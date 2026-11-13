import os
from flask import Flask, request, abort
from dotenv import load_dotenv
from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
)

# .envファイルから環境変数を読み込む
load_dotenv('LINE_CHANNEL.env')

app = Flask(__name__)

# 環境変数からトークンとシークレットを取得
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

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
    
    # ユーザーが送ってきた言葉をそのままオウム返しするテスト
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"「{received_text}」を受け取りました！")
    )

if __name__ == "__main__":
    app.run(port=5000)
