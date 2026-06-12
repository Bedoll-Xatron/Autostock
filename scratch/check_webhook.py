import asyncio
import os
import httpx
from dotenv import load_dotenv

async def main():
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        print(resp.json())

if __name__ == "__main__":
    asyncio.run(main())
