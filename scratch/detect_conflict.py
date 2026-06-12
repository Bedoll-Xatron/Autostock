import asyncio
import os
from telegram import Bot
from dotenv import load_dotenv
import time

async def main():
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    bot = Bot(token=token)
    print("Testing for remote conflict (30 seconds)...")
    for i in range(5):
        try:
            # getUpdates will fail if another instance is polling
            updates = await bot.get_updates(timeout=5)
            print(f"[{i}] Success! No conflict at this moment.")
        except Exception as e:
            print(f"[{i}] Conflict/Error detected: {e}")
        await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
