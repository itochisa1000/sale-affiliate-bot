import os
import sqlite3
from flask import Flask, request, abort, render_template_string
from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, FollowEvent, FlexSendMessage,
    BubbleContainer, BoxComponent, TextComponent, ButtonComponent, PostbackAction
)
from dotenv import load_dotenv

# .envファイル（LINE_CHANNEL.env）から認証情報を読み込む
load_dotenv('LINE_CHANNEL.env')

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
ADMIN_USER_ID = os.environ.get('ADMIN_USER_ID')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 8つのカテゴリー定義
CATEGORIES = [
    "お米", 
    "飲料", 
    "ベビー・キッズ", 
    "お酒", 
    "日用品", 
    "美容・コスメ", 
    "グルメ", 
    "キッチン用品"
]

def init_db():
    """ 起動時にデータベースとテーブルを自動作成する関数 """
    conn = sqlite3.connect('otoku.db')
    cursor = conn.cursor()
    # ユーザーIDとカテゴリーを保存するテーブル
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_categories (
            user_id TEXT,
            category TEXT,
            PRIMARY KEY (user_id, category)
        )
    ''')
    conn.commit()
    conn.close()

# アプリ起動時にDBを初期化
init_db()

def get_user_categories(user_id):
    """ 指定したユーザーが現在登録しているカテゴリーのリストを取得する関数 """
    conn = sqlite3.connect('otoku.db')
    cursor = conn.cursor()
    cursor.execute('SELECT category FROM user_categories WHERE user_id = ?', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

def create_category_flex_message(user_id):
    """ 現在の登録状態を反映したカテゴリー選択Flex Messageを生成する関数 """
    current_categories = get_user_categories(user_id)
    
    buttons = []
    for cat in CATEGORIES:
        # すでに登録されているカテゴリーはラベルに「✅」をつけるなどの工夫も可能
        is_registered = cat in current_categories
        label_text = f"✅ {cat}" if is_registered else cat
        
        buttons.append(
            ButtonComponent(
                style="secondary",
                height="sm",
                action=PostbackAction(
                    label=label_text,
                    data=f"action=toggle_category&category={cat}"
                )
            )
        )
        
    box_contents = [
        TextComponent(
            text="受け取りたいお得情報のカテゴリーを選んでね！（タップで登録/解除）", 
            weight="bold", 
            size="xs", 
            wrap=True, 
            margin="md"
        )
    ]
    for btn in buttons:
        box_contents.append(btn)

    bubble = BubbleContainer(
        body=BoxComponent(
            layout="vertical",
            contents=box_contents,
            spacing="sm"
        )
    )
    return FlexSendMessage(alt_text="カテゴリー選択メニュー", contents=bubble)


@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    received_text = event.message.text
    user_id = event.source.user_id
    
     # 管理者からの「配信」コマンド処理
    if user_id == ADMIN_USER_ID and received_text.startswith("配信"):
        lines = [line.strip() for line in received_text.splitlines() if line.strip()]
        if len(lines) >= 5:
            category = lines[1]
            title = lines[2]
            price_info = lines[3]
            url = lines[4]
            if category in CATEGORIES:
                sent_count = broadcast_deal_to_category(category, title, price_info, url)
                reply_text = f"📢【配信完了】\n「{category}」の登録者 {sent_count} 名に一斉送信しました！"
            else:
                reply_text = f"⚠️ カテゴリー名「{category}」が正しくありません。\n以下から選んでください：\n" + "、".join(CATEGORIES)
        else:
            reply_text = (
                "⚠️ 入力形式が足りません。以下の5行で送ってください：\n\n"
                "配信\nカテゴリー名\n商品タイトル\n価格・割引情報\n商品URL"
            )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return


    if "カテゴリー" in received_text or "設定" in received_text or "メニュー" in received_text:
        flex_msg = create_category_flex_message(user_id)
        line_bot_api.reply_message(event.reply_token, flex_msg)
    elif "確認" in received_text:
        current_list = get_user_categories(user_id)
        if current_list:
            list_text = "\n".join([f"・{cat}" for cat in current_list])
            reply_text = f"📋 現在登録中のカテゴリー：\n\n{list_text}\n\n変更したい場合は「設定」と送ってね！"
        else:
            reply_text = "現在登録されているカテゴリーはありません。\n下のメニューから「配信カテゴリー選択・変更」を押して登録してね！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    elif "ヘルプ" in received_text or "使い方" in received_text or "質問" in received_text:
        reply_text = (
            "💡【お得速報の使い方】\n\n"
            "1️⃣ 下のメニュー「配信カテゴリー選択・変更」から、欲しいセール情報のジャンルを選びます。\n"
            "2️⃣ 選んだジャンルのセールが開催されたときだけ、LINEで通知が届きます！\n"
            "3️⃣ もう一度同じボタンをタップすると解除できます。"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"「{received_text}」を受け取りました！\n\n下のメニューから「配信カテゴリー選択・変更」を押すと、お好みのジャンルを選べます。")
        )


@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    welcome_text = TextSendMessage(
        text="友だち追加ありがとうございます！🎉\n\nこのアカウントでは、あなたが選んだジャンルのお得なセール情報だけを厳選してお届けします。\n\nまずは下のボタンから、受け取りたいカテゴリーを選んでね！"
    )
    flex_msg = create_category_flex_message(user_id)
    line_bot_api.reply_message(event.reply_token, [welcome_text, flex_msg])


@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data
    params = dict(item.split('=') for item in data.split('&'))
    
    if params.get('action') == 'toggle_category':
        selected_category = params.get('category')
        user_id = event.source.user_id
        
        conn = sqlite3.connect('otoku.db')
        cursor = conn.cursor()
        
        # すでに登録されているかチェック
        cursor.execute('SELECT 1 FROM user_categories WHERE user_id = ? AND category = ?', (user_id, selected_category))
        exists = cursor.fetchone()
        
        if exists:
            # 登録済みなら削除（解除）
            cursor.execute('DELETE FROM user_categories WHERE user_id = ? AND category = ?', (user_id, selected_category))
            msg_status = f"🗑️「{selected_category}」の登録を解除しました！"
        else:
            # 未登録なら追加（登録）
            cursor.execute('INSERT INTO user_categories (user_id, category) VALUES (?, ?)', (user_id, selected_category))
            msg_status = f"✅「{selected_category}」を登録しました！"
            
        conn.commit()
        conn.close()
        
        # 現在の登録中リストを取得
        current_list = get_user_categories(user_id)
        if current_list:
            list_text = "、".join(current_list)
        else:
            list_text = "なし（未選択）"
            
        reply_text = f"{msg_status}\n\n📋 現在の登録中：{list_text}"
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
def broadcast_deal_to_category(category, title, price_info, url):
    """ 指定カテゴリーを登録している全ユーザーにセール情報を一斉送信する関数 """
    conn = sqlite3.connect('otoku.db')
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT user_id FROM user_categories WHERE category = ?', (category,))
    target_users = [row[0] for row in cursor.fetchall()]
    conn.close()

    if not target_users:
        return 0

    message_text = (
        f"🔥【{category}の超お得情報！】\n\n"
        f"📦 {title}\n"
        f"💰 {price_info}\n\n"
        f"👇 詳細・購入はこちら\n{url}"
    )

    # LINEのmulticast機能で対象者全員に一斉プッシュ送信
    line_bot_api.multicast(target_users, TextSendMessage(text=message_text))
    return len(target_users)

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>お得速報 管理者配信パネル</title>
    <style>
        body { font-family: sans-serif; max-width: 500px; margin: 40px auto; padding: 20px; background: #f9f9f9; }
        .card { background: white; padding: 25px; border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        h2 { color: #333; margin-top: 0; }
        label { display: block; margin-top: 15px; font-weight: bold; color: #555; }
        select, input { width: 100%; padding: 10px; margin-top: 5px; border: 1px solid #ccc; border-radius: 5px; box-sizing: border-box; font-size: 16px; }
        button { width: 100%; padding: 12px; margin-top: 25px; background: #06C755; color: white; border: none; border-radius: 5px; font-size: 16px; font-weight: bold; cursor: pointer; }
        button:hover { background: #05b34c; }
        .msg { padding: 12px; background: #e8f8ee; color: #058a3a; border-radius: 5px; margin-bottom: 15px; font-weight: bold; }
    </style>
</head>
<body>
    <div class="card">
        <h2>📢 お得速報 配信パネル</h2>
        {% if result_msg %}
            <div class="msg">{{ result_msg }}</div>
        {% endif %}
        <form method="POST">
            <label>① 配信するカテゴリー</label>
            <select name="category" required>
                {% for cat in categories %}
                    <option value="{{ cat }}">{{ cat }}</option>
                {% endfor %}
            </select>
            <label>② 商品タイトル</label>
            <input type="text" name="title" placeholder="例：無洗米 あきたこまち 5kg" required>
            <label>③ セール価格・割引情報</label>
            <input type="text" name="price_info" placeholder="例：通常4,500円 → 2,980円（33%OFF!）" required>
            <label>④ アフィリエイトリンク（URL）</label>
            <input type="url" name="url" placeholder="https://..." required>
            <button type="submit">このカテゴリーの登録者に一斉配信する</button>
        </form>
    </div>
</body>
</html>
"""

@app.route("/admin", methods=['GET', 'POST'])
def admin_panel():
    result_msg = None
    if request.method == 'POST':
        category = request.form.get('category')
        title = request.form.get('title')
        price_info = request.form.get('price_info')
        url = request.form.get('url')
        
        sent_count = broadcast_deal_to_category(category, title, price_info, url)
        result_msg = f"✅「{category}」を登録している {sent_count} 名に配信しました！"
        
    return render_template_string(ADMIN_HTML, categories=CATEGORIES, result_msg=result_msg)

if __name__ == "__main__":
    app.run(port=5000)
