import os
from flask import Flask, request, abort
from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, FlexSendMessage,
    BubbleContainer, BoxComponent, TextComponent, ButtonComponent, PostbackAction
)
from dotenv import load_dotenv

# .envファイル（LINE_CHANNEL.env）から認証情報を読み込む
load_dotenv('LINE_CHANNEL.env')

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

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

def create_category_flex_message():
    """ 8つのカテゴリーを選択できるFlex Message（バブル）を生成する関数 """
    buttons = []
    for cat in CATEGORIES:
        buttons.append(
            ButtonComponent(
                style="secondary",
                height="sm",
                action=PostbackAction(
                    label=cat,
                    data=f"action=select_category&category={cat}"
                )
            )
        )
        
    # タイトルとボタン群をまとめるボックス
    box_contents = [
        TextComponent(
            text="受け取りたいお得情報のカテゴリーを選んでね！", 
            weight="bold", 
            size="sm", 
            wrap=True, 
            margin="md"
        )
    ]
    # ボタンを順番に追加
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
    """ LINEからのWebhookを受け取るエンドポイント """
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
    """ テキストメッセージを受信したときの処理 """
    received_text = event.message.text
    
    # 「カテゴリー」や「設定」と送信されたらFlex Messageを表示する
    if "カテゴリー" in received_text or "設定" in received_text or "メニュー" in received_text:
        flex_msg = create_category_flex_message()
        line_bot_api.reply_message(event.reply_token, flex_msg)
    else:
        # 通常のテキストの場合は案内を返す
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"「{received_text}」を受け取りました！\n\n「カテゴリー設定」と送信すると、お好みのジャンルを選べます。")
        )


@handler.add(PostbackEvent)
def handle_postback(event):
    """ Flex Messageのボタンなどが押された（ポストバック）ときの処理 """
    data = event.postback.data
    
    # クエリパラメータ形式のデータを分解する (例: action=select_category&category=お米)
    params = dict(item.split('=') for item in data.split('&'))
    
    if params.get('action') == 'select_category':
        selected_category = params.get('category')
        user_id = event.source.user_id  # ユーザーのLINE ID
        
        # 💡 [データベース保存処理のイメージ]
        # ここで DB（SQLiteやMySQLなど）に 「user_id」と「selected_category」の紐付けを保存・更新します。
        # 例: save_user_category(user_id, selected_category)
        
        # ユーザーへ完了の返信
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"「{selected_category}」カテゴリーの登録を受け付けました！\n今後このジャンルのお得情報をお届けします。")
        )


if __name__ == "__main__":
    app.run(port=5000)