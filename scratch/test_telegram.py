import asyncio
import os
from telegram import Bot
from dotenv import load_dotenv

async def main():
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    print(f"Token: {token[:10]}...")
    print(f"Chat ID: {chat_id}")
    
    bot = Bot(token=token)
    try:
        await bot.send_message(chat_id=chat_id, text="🚀 Telegram 연결 테스트 중입니다.")
        print("Success!")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
