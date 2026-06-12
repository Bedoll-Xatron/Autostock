import asyncio
import os
from autostock.trading.kis_client import get_balance
from dotenv import load_dotenv
import json

def main():
    load_dotenv()
    try:
        balance = get_balance()
        print(json.dumps(balance, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
