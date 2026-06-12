import sys
import os
sys.path.append(os.getcwd())

import asyncio
import httpx
from autostock.db.supabase import fetch_watchlist
from autostock.research.agents import screening_agent
from autostock.market.fetcher import get_large_cap_tickers
from autostock.models import TradingState
from autostock import config

async def send_direct_telegram(text):
    """봇 루프 없이 직접 텔레그램 메시지 전송"""
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        print("텔레그램 설정이 없습니다.")
        return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                print("✅ 텔레그램 테스트 메시지 발송 성공!")
            else:
                print(f"❌ 텔레그램 발송 실패: {resp.text}")
        except Exception as e:
            print(f"❌ 텔레그램 에러: {e}")

async def sample_test():
    print("=== Gem Hunter 로직 및 텔레그램 테스트 ===")
    
    # 1. 텔레그램 테스트
    await send_direct_telegram("🔔 <b>Gem Hunter 연결 테스트</b>\n신규 봇 토큰 및 08:30/15:00 스케줄 설정이 적용되었습니다.")
    
    # 2. 워치리스트 로드
    watchlist = fetch_watchlist()
    print(f"전체 워치리스트 종목 수: {len(watchlist)}")
    
    # 3. 대형주 필터링
    large_caps = get_large_cap_tickers()
    filtered_watchlist = [w for w in watchlist if w["ticker"] not in large_caps]
    print(f"대형주 제외 후 남은 종목: {len(filtered_watchlist)}개")
    
    # 4. AI 스크리닝 실행
    state = {
        "watchlist": watchlist,
        "market_data": {"condition": "NORMAL"},
        "selected_tickers": [],
        "technical_reports": {},
        "fundamental_reports": {},
        "sentiment_reports": {},
        "final_decisions": [],
        "retry_count": 0
    }
    
    print("\nAI 스크리닝 실행 중...")
    result = screening_agent(state)
    
    tickers = result['selected_tickers']
    reason = result['screening_reason']

    print("\n--- [최종 스크리닝 결과] ---")
    print(f"선정된 종목: {tickers}")
    print(f"선정 사유: {reason}")

    # 5. 결과 텔레그램 전송
    msg = f"🔍 <b>Gem Hunter 분석 결과</b>\n\n"
    msg += f"✅ <b>선정 종목</b>: {', '.join(tickers)}\n\n"
    msg += f"💬 <b>선정 이유</b>: {reason}"
    await send_direct_telegram(msg)

if __name__ == "__main__":
    asyncio.run(sample_test())
