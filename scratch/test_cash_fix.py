import asyncio
import os
from autostock.trading.kis_client import get_available_cash
from dotenv import load_dotenv

def main():
    load_dotenv()
    try:
        cash = get_available_cash()
        print(f"Available Cash: {cash:,.0f}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
