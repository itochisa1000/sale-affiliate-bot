import os
import hmac
import secrets
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import parse_qs, urlencode

from flask import (
    Flask, request, abort, render_template, Response,
    redirect, url_for, flash, session
)
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, FollowEvent, UnfollowEvent, FlexSendMessage,
    BubbleContainer, BoxComponent, TextComponent, ButtonComponent,
    PostbackAction
)
from dotenv import load_dotenv

# ---------------------------------------------------------------
# 設定
# ---------------------------------------------------------------
load_dotenv('LINE_CHANNEL.env')

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise RuntimeError('LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET が未設定です')

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(24)

logging.basicConfig(level=logging.INFO)
logger = app.logger

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

DB_PATH = 'otoku.db'
MULTICAST_LIMIT = 500        # LINE multicastの1回あたり上限
LINE_TEXT_MAX = 5000         # LINEテキストメッセージの最大文字数
JST = timezone(timedelta(hours=9))
ALL_CONFIRM_WORD = "全員"     # 全員配信時に入力させる確認ワード

CATEGORIES = [
    "お米", "飲料", "グルメ", "日用品", "キッチン用品",
    "ベビー・キッズ", "美容・コスメ", "健康食品", "お酒"
]

CATEGORY_EMOJIS = {
    "お米": "🌾お米",
    "飲料": "🥤飲料",
    "グルメ": "🥩グルメ🍰",
    "日用品": "🧻日用品",
    "キッチン用品": "🍳キッチン用品",
    "ベビー・キッズ": "👶ベビー・キッズ",
    "美容・コスメ": "💄美容・コスメ",
    "健康食品": "💊健康食品",
    "お酒": "🍺お酒"
}

SITES = ["Amazon", "楽天市場", "Yahoo!ショッピング"]


# ---------------------------------------------------------------
# データベース
# ---------------------------------------------------------------
@contextmanager
def get_conn():
    """ 自動でcommit/rollback/closeするDB接続 """
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_categories (
                user_id TEXT,
                category TEXT,
                PRIMARY KEY (user_id, category)
            )
        ''')
        # 友だち全員のテーブル（カテゴリー未選択の人も含む）
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')
        # 配信履歴
        conn.execute('''
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at TEXT DEFAULT (datetime('now', 'localtime')),
                kind TEXT,
                target TEXT,
                site_name TEXT,
                title TEXT,
                body TEXT,
                url TEXT,
                success_count INTEGER,
                failed_count INTEGER
            )
        ''')
        # 既存のカテゴリー登録者をusersへ移行
        conn.execute(
            'INSERT OR IGNORE INTO users (user_id) '
            'SELECT DISTINCT user_id FROM user_categories'
        )


init_db()


def ensure_user(user_id):
    """ 何かイベントが来た時点でusersに登録（既存の友だちの取りこぼし対策） """
    if not user_id:
        return
    with get_conn() as conn:
        conn.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))


def delete_user(user_id):
    with get_conn() as conn:
        conn.execute('DELETE FROM user_categories WHERE user_id = ?', (user_id,))
        conn.execute('DELETE FROM users WHERE user_id = ?', (user_id,))


def count_users():
    with get_conn() as conn:
        return conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]


def get_user_categories(user_id):
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT category FROM user_categories WHERE user_id = ?', (user_id,)
        ).fetchall()
    registered = {r[0] for r in rows}
    return [c for c in CATEGORIES if c in registered]


def get_users_for_categories(categories):
    """ 指定カテゴリーのいずれかを登録しているユーザー（重複なし） """
    if not categories:
        return []
    placeholders = ",".join("?" * len(categories))
    with get_conn() as conn:
        rows = conn.execute(
            f'SELECT DISTINCT user_id FROM user_categories WHERE category IN ({placeholders})',
            categories
        ).fetchall()
    return [r[0] for r in rows]


def toggle_category(user_id, category):
    """ 登録なら解除、未登録なら登録。登録後の状態(True=登録)を返す """
    with get_conn() as conn:
        conn.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
        exists = conn.execute(
            'SELECT 1 FROM user_categories WHERE user_id = ? AND category = ?',
            (user_id, category)
        ).fetchone()
        if exists:
            conn.execute(
                'DELETE FROM user_categories WHERE user_id = ? AND category = ?',
                (user_id, category)
            )
            return False
        conn.execute(
            'INSERT INTO user_categories (user_id, category) VALUES (?, ?)',
            (user_id, category)
        )
        return True


def save_delivery(kind, target, site_name, title, body, url, success, failed):
    with get_conn() as conn:
        conn.execute('''
            INSERT INTO deliveries
                (kind, target, site_name, title, body, url, success_count, failed_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (kind, target, site_name, title, body, url, success, failed))


def get_recent_deliveries(limit=10):
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT * FROM deliveries ORDER BY id DESC LIMIT ?', (limit,)
        ).fetchall()
    return rows


# ---------------------------------------------------------------
# LINE API：通数・友だち数
# ---------------------------------------------------------------
def get_quota_info():
    """ 今月の上限・送信済み・残り通数をLINEのAPIから取得 """
    try:
        quota = line_bot_api.get_message_quota()
        consumption = line_bot_api.get_message_quota_consumption()
        used = consumption.total_usage
        if quota.type == 'limited':
            limit = quota.value
            return {'ok': True, 'unlimited': False, 'limit': limit,
                    'used': used, 'remaining': max(limit - used, 0)}
        return {'ok': True, 'unlimited': True, 'limit': None,
                'used': used, 'remaining': None}
    except Exception as e:
        logger.error("通数の取得に失敗: %s", e)
        return {'ok': False}


def estimate_all_count():
    """ 全員配信の見積人数。usersの人数と友だち数API（前日分）の大きいほうを採用 """
    db_count = count_users()
    api_count = None
    try:
        yesterday = (datetime.now(JST) - timedelta(days=1)).strftime('%Y%m%d')
        res = line_bot_api.get_insight_followers(yesterday)
        if res.status == 'ready' and res.followers is not None:
            api_count = res.followers
    except Exception as e:
        logger.warning("友だち数APIの取得に失敗: %s", e)
    return max(db_count, api_count or 0), db_count, api_count


# ---------------------------------------------------------------
# Flex Message
# ---------------------------------------------------------------
def create_category_flex_message(user_id):
    current = get_user_categories(user_id)

    contents = [
        TextComponent(
            text="受け取りたいお得情報のカテゴリーを選んでね！（タップで登録/解除）",
            weight="bold", size="xs", wrap=True, margin="md"
        )
    ]
    for cat in CATEGORIES:
        disp = CATEGORY_EMOJIS.get(cat, cat)
        label = f"✅ {disp}" if cat in current else disp
        contents.append(
            ButtonComponent(
                style="secondary",
                height="sm",
                action=PostbackAction(
                    label=label,
                    data=urlencode({"action": "toggle_category", "category": cat})
                )
            )
        )

    bubble = BubbleContainer(
        body=BoxComponent(layout="vertical", contents=contents, spacing="sm")
    )
    return FlexSendMessage(alt_text="カテゴリー選択メニュー", contents=bubble)


# ---------------------------------------------------------------
# LINE Webhook
# ---------------------------------------------------------------
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    logger.info("Webhook received")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text
    user_id = event.source.user_id
    ensure_user(user_id)

    if "カテゴリー" in text or "設定" in text or "メニュー" in text:
        line_bot_api.reply_message(event.reply_token, create_category_flex_message(user_id))

    elif "確認" in text:
        current = get_user_categories(user_id)
        if current:
            list_text = "\n".join(f"・{c}" for c in current)
            reply = f"📋 現在登録中のカテゴリー：\n\n{list_text}\n\n変更したい場合は「設定」と送ってね！"
        else:
            reply = "現在登録されているカテゴリーはありません。\n下のメニューから「配信カテゴリー選択・変更」を押して登録してね！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    elif "ヘルプ" in text or "使い方" in text or "質問" in text:
        reply = (
            "💡【お得速報の使い方】\n\n"
            "1️⃣ 下のメニュー「配信カテゴリー選択・変更」から、欲しいセール情報のジャンルを選びます。\n"
            "2️⃣ 選んだジャンルのセールが開催されたときだけ、LINEで通知が届きます！\n"
            "3️⃣ もう一度同じボタンをタップすると解除できます。"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"「{text}」を受け取りました！\n\n下のメニューから「配信カテゴリー選択・変更」を押すと、お好みのジャンルを選べます。"
            )
        )


@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    ensure_user(user_id)
    welcome = TextSendMessage(
        text=(
            "友だち追加ありがとうございます！🎉\n\n"
            "このアカウントでは、あなたが選んだジャンルのお得なセール情報だけを厳選してお届けします。\n\n"
            "まずは下のボタンから、受け取りたいカテゴリーを選んでね！"
        )
    )
    line_bot_api.reply_message(
        event.reply_token, [welcome, create_category_flex_message(user_id)]
    )


@handler.add(UnfollowEvent)
def handle_unfollow(event):
    """ ブロック・友だち解除されたらユーザーデータを削除 """
    delete_user(event.source.user_id)


@handler.add(PostbackEvent)
def handle_postback(event):
    params = {k: v[0] for k, v in parse_qs(event.postback.data).items()}

    if params.get('action') != 'toggle_category':
        return

    category = params.get('category')
    if category not in CATEGORIES:
        return

    user_id = event.source.user_id
    registered = toggle_category(user_id, category)
    if registered:
        status = f"✅「{category}」を登録しました！"
    else:
        status = f"🗑️「{category}」の登録を解除しました！"

    current = get_user_categories(user_id)
    list_text = "、".join(current) if current else "なし（未選択）"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"{status}\n\n📋 現在登録中：{list_text}")
    )


# ---------------------------------------------------------------
# 配信ロジック
# ---------------------------------------------------------------
def chunked(items, n=MULTICAST_LIMIT):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def send_to_users(user_ids, text):
    """ 500人ずつ分割してmulticast。(成功数, 失敗数) を返す """
    success = failed = 0
    for chunk in chunked(user_ids):
        try:
            line_bot_api.multicast(chunk, TextSendMessage(text=text))
            success += len(chunk)
        except LineBotApiError as e:
            failed += len(chunk)
            logger.error("multicast失敗: status=%s message=%s", e.status_code, e.message)
    return success, failed


def build_deal_text(category, title, price_info, url, site_name=""):
    link_label = f"👇 {site_name}で詳細・購入する" if site_name else "👇 詳細・購入はこちら"
    return (
        f"【PR】🔥【{category}が安い！】\n\n"
        f"📦 {title}\n"
        f"💰 {price_info}\n"
        f"※価格は配信時点のものです。\n\n"
        f"{link_label}\n{url}"
    )


def deliver(plan):
    """ 確認済みの配信プランを実行し、履歴を保存。(成功数 or None, 失敗数) """
    meta = plan['meta']
    if plan['user_ids'] is None:   # 全員配信
        try:
            line_bot_api.broadcast(TextSendMessage(text=plan['text']))
            success, failed = None, 0
        except LineBotApiError as e:
            logger.error("broadcast失敗: status=%s message=%s", e.status_code, e.message)
            success, failed = 0, 1
    else:
        success, failed = send_to_users(plan['user_ids'], plan['text'])

    save_delivery(plan['kind'], plan['target_label'], meta.get('site_name', ''),
                  meta.get('title', ''), meta.get('body', ''), meta.get('url', ''),
                  success, failed)
    return success, failed


# ---------------------------------------------------------------
# 管理画面
# ---------------------------------------------------------------
def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not ADMIN_PASSWORD:
            return Response('ADMIN_PASSWORD が設定されていません', 503)
        auth = request.authorization
        ok = (
            auth is not None
            and hmac.compare_digest(auth.username or '', ADMIN_USERNAME)
            and hmac.compare_digest(auth.password or '', ADMIN_PASSWORD)
        )
        if not ok:
            return Response('認証が必要です', 401,
                            {'WWW-Authenticate': 'Basic realm="Admin"'})
        return f(*args, **kwargs)
    return wrapper


def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_hex(16)
    return session['csrf']


app.jinja_env.globals['csrf_token'] = csrf_token


def check_csrf():
    sent = request.form.get('csrf_token', '')
    if not sent or not hmac.compare_digest(sent, session.get('csrf', '')):
        abort(400)


def build_plan(form):
    """ フォーム内容から配信プランを作る。(エラー文 or None, プラン or None) """
    kind = form.get('form_type')

    if kind == 'deal':
        category = form.get('category', '')
        site_name = form.get('site_name', '')
        title = form.get('title', '').strip()
        price_info = form.get('price_info', '').strip()
        url = form.get('url', '').strip()

        if category not in CATEGORIES:
            return "カテゴリーが正しくありません", None
        if site_name and site_name not in SITES:
            return "購入サイトが正しくありません", None
        if not (title and price_info and url.startswith(("http://", "https://"))):
            return "入力内容を確認してください（URLはhttp(s)://から）", None

        user_ids = get_users_for_categories([category])
        if not user_ids:
            return f"「{category}」の登録者がいません", None

        text = build_deal_text(category, title, price_info, url, site_name)
        return None, {
            'kind': 'deal', 'is_all': False,
            'target_label': category,
            'user_ids': user_ids, 'count': len(user_ids), 'count_note': '',
            'text': text,
            'meta': {'site_name': site_name, 'title': title,
                     'body': price_info, 'url': url},
        }

    if kind == 'free':
        mode = form.get('target_mode', '')
        body = form.get('message_body', '').strip()
        if not body:
            return "メッセージ本文を入力してください", None
        if len(body) > LINE_TEXT_MAX:
            return f"本文が長すぎます（最大{LINE_TEXT_MAX}文字）", None

        if mode == 'all':
            count, db_count, api_count = estimate_all_count()
            note = f"目安（登録済み {db_count} 名"
            note += f"／友だち数API {api_count} 名）" if api_count is not None else "／友だち数APIは取得不可）"
            return None, {
                'kind': 'free', 'is_all': True,
                'target_label': '全員',
                'user_ids': None, 'count': count, 'count_note': note,
                'text': body, 'meta': {'body': body},
            }

        if mode == 'categories':
            selected = []
            for c in form.getlist('target_categories'):
                if c in CATEGORIES and c not in selected:
                    selected.append(c)
            if not selected:
                return "カテゴリーを1つ以上選んでください", None
            user_ids = get_users_for_categories(selected)
            if not user_ids:
                return "選んだカテゴリーの登録者がいません", None
            return None, {
                'kind': 'free', 'is_all': False,
                'target_label': "、".join(selected),
                'user_ids': user_ids, 'count': len(user_ids),
                'count_note': '（複数カテゴリー登録者の重複は除外済み）' if len(selected) > 1 else '',
                'text': body, 'meta': {'body': body},
            }

        return "配信対象を選んでください", None

    return "不正なリクエストです", None


def format_result(plan, success, failed):
    count = "友だち全員" if success is None else f"{success} 名"
    msg = f"✅「{plan['target_label']}」の対象者（{count}）に配信しました！"
    if failed:
        msg += f"　⚠️ 失敗: {failed} 件（通数上限などの可能性。ログを確認してください）"
    return msg


@app.route("/otoku-haishin-821", methods=['GET', 'POST'])
@require_auth
def admin_panel():
    if request.method == 'POST':
        check_csrf()
        step = request.form.get('step', 'preview')

        error, plan = build_plan(request.form)
        if error:
            flash(f"⚠️ {error}", "error")
            return redirect(url_for('admin_panel'))

        fields = [(k, v) for k, v in request.form.items(multi=True)
                  if k not in ('step', 'csrf_token', 'confirm_text')]

        # ステップ1：確認画面（対象人数・残り通数を表示）
        if step == 'preview':
            quota = get_quota_info()
            after = None
            short = False
            if quota['ok'] and not quota['unlimited']:
                after = quota['remaining'] - plan['count']
                short = after < 0
            return render_template(
                'admin_confirm.html',
                plan=plan, fields=fields, quota=quota,
                after=after, short=short, confirm_word=ALL_CONFIRM_WORD
            )

        # ステップ2：送信確定
        if step == 'send':
            if plan['is_all'] and request.form.get('confirm_text', '').strip() != ALL_CONFIRM_WORD:
                flash(f"⚠️ 全員配信は確認ワード「{ALL_CONFIRM_WORD}」の入力が必要です。送信していません。", "error")
                return redirect(url_for('admin_panel'))
            success, failed = deliver(plan)
            flash(format_result(plan, success, failed), "error" if failed else "ok")
            return redirect(url_for('admin_panel'))

        abort(400)

    return render_template(
        'admin.html',
        categories=CATEGORIES,
        sites=SITES,
        quota=get_quota_info(),
        user_count=count_users(),
        deliveries=get_recent_deliveries()
    )


if __name__ == "__main__":
    # 本番は gunicorn 等を使用: gunicorn -w 2 -b 127.0.0.1:5000 app:app
    app.run(host="127.0.0.1", port=5000, debug=False)