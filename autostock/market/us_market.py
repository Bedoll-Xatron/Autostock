"""미국 시장 지표 실시간 수집 및 시황 브리핑 생성 모듈 (n8n 대체)"""
import os
import httpx
import logging
from datetime import date
from autostock import config
from autostock.db import supabase as db
from autostock.hitl import telegram_bot as bot_ui
from autostock.utils.retry import sync_retry
from langchain_core.messages import SystemMessage, HumanMessage

log = logging.getLogger(__name__)

@sync_retry(max_retries=3)
def fetch_fear_and_greed() -> dict:
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("fear_and_greed", {})
        return {
            "fear_greed_score": round(data.get("score", 50)),
            "fear_greed_rating": data.get("rating", "Neutral")
        }
    except Exception as e:
        log.error("CNN 공포탐욕지수 조회 실패: %s", e)
        return {"fear_greed_score": 50, "fear_greed_rating": "Neutral"}

import yfinance as yf
import FinanceDataReader as fdr

@sync_retry(max_retries=3)
def fetch_fdr_index(symbol: str) -> float:
    """FinanceDataReader를 이용해 지수/환율 종가 조회"""
    df = fdr.DataReader(symbol)
    if df.empty:
        return 0.0
    return float(df['Close'].iloc[-1])

@sync_retry(max_retries=3)
def fetch_yf_price(symbol: str) -> dict:
    """yfinance를 이용해 실시간 가격 및 변동성 조회"""
    ticker = yf.Ticker(symbol)
    # fast_info 또는 history 사용
    hist = ticker.history(period="2d")
    if hist.empty:
        return {"price": 0.0, "movement": "Stable"}
    
    current_price = hist['Close'].iloc[-1]
    prev_price = hist['Close'].iloc[-2] if len(hist) > 1 else current_price
    
    change = current_price - prev_price
    if change > 0:
        mov = "Up"
    elif change < 0:
        mov = "Down"
    else:
        mov = "Stable"
        
    return {"price": current_price, "movement": mov}

def get_market_data() -> dict:
    fg = fetch_fear_and_greed()

    vix, vix_mov = 20.0, "Stable"
    sp500, sp500_mov = 0.0, "Stable"
    krw, krw_mov = 1350.0, "Stable"
    kospi, kospi_mov = 0.0, "Stable"

    # VIX & S&P Futures (yfinance)
    try:
        # VIX
        v_data = fetch_yf_price("^VIX")
        vix = v_data["price"]
        vix_mov = v_data["movement"]
            
        # S&P Futures
        s_data = fetch_yf_price("ES=F")
        sp500 = s_data["price"]
        sp500_mov = s_data["movement"]
    except Exception as e:
        log.error("VIX/S&P 조회 에러: %s", e)

    # USD-KRW (FinanceDataReader)
    try:
        krw = fetch_fdr_index("USD/KRW")
        # FDR은 일봉 기준이라 변동성 계산은 생략하거나 간단히 처리
        krw_mov = "Stable"
    except Exception as e:
        log.error("환율 조회 에러: %s", e)

    # KOSPI (FinanceDataReader)
    try:
        kospi = fetch_fdr_index("KS11") # 코스피 지수
        kospi_mov = "Stable"
    except Exception as e:
        log.error("코스피 조회 에러: %s", e)

    fg_score = fg["fear_greed_score"]
    
    # n8n 조건 로직 완벽 복제
    if fg_score < 25 or fg_score > 74 or vix >= 30:
        cond = "DANGER"
    elif fg_score < 50 or vix >= 20:
        cond = "CAUTION"
    else:
        cond = "NORMAL"

    return {
        "date": date.today().isoformat(),
        "fear_greed_score": fg_score,
        "fear_greed_rating": fg["fear_greed_rating"],
        "vix": vix,
        "vix_movement": vix_mov,
        "sp500_futures": sp500,
        "sp500_futures_movement": sp500_mov,
        "usd_krw": krw,
        "kospi": kospi,
        "kospi_movement": kospi_mov,
        "condition": cond,
    }

def generate_morning_briefing(data: dict) -> str:
    msg_body = f"""
date: {data['date']}
fear_greed_score: {data['fear_greed_score']} ({data['fear_greed_rating']})
vix: {data['vix']} ({data['vix_movement']})
sp500_futures: {data['sp500_futures']} ({data['sp500_futures_movement']})
usd_krw: {data['usd_krw']} 
kospi: {data['kospi']} ({data['kospi_movement']})
condition: {data['condition']}
"""
    sys_prompt = '''You are a quantitative market analyst. Analyze the provided market data and write a concise Korean morning briefing for a Telegram alert.

## Output Format (strictly follow this structure)

📊 **오늘의 시황 브리핑** · {date}

**📈 시장 지표**
• 공탐지수: {fear_greed_score} ({fear_greed_rating})
• VIX: {vix} ({vix_movement})
• S&P500 선물: {sp500_futures} ({sp500_futures_movement})
• 원/달러: {usd_krw}
• 코스피: {kospi} ({kospi_movement})

**🧠 시황 해석**
(2~3줄. 지표 간 상관관계 기반 핵심만. 수치 반복 금지.)

**⚡ 매매 판단**
{condition} — (판단 근거 1줄)

## Rules
- 총 응답은 15줄 이내
- 시황 해석은 데이터를 단순 나열하지 말고 지표 간 관계를 해석할 것
- condition이 DANGER일 때는 위험/기회 양면을 모두 언급할 것
- 숫자 포맷: 소수점 2자리, 환율은 정수
- Telegram 렌더링 기준으로 bold/bullet 사용, 코드블록 사용 금지
- 한국어로만 작성'''

    try:
        from autostock.research.llm_factory import get_basic_llm
        llm = get_basic_llm()
        res = llm.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=msg_body)])
        return res.content
    except Exception as e:
        log.error("브리핑 LLM 생성 실패: %s", e)
        return f"📊 **오늘의 시황 브리핑**\n시황 생성 중 오류가 발생했습니다. (데이터 정상 기록)"

def run_us_market_update() -> dict:
    """잡(Job) 스케줄러가 파이프라인 직전(08:40)에 호출."""
    log.info("미국 시황 데이터(Market Daily) 스크래핑 시작...")
    data = get_market_data()
    
    # Supabase에 덮어쓰기 저장 (키: date)
    db.upsert_market_data(data)
    log.info("Market Daily 저장 완료: %s", data["date"])
    
    # 디스코드에 브리핑 발송
    briefing = generate_morning_briefing(data)
    try:
        bot_ui.schedule_message(briefing)
    except Exception as e:
        log.error("브리핑 전송 실패 (텔레그램 봇 미실행 등): %s", e)
    
    return data

if __name__ == "__main__":
    # 단독 실행 모드
    print("단독 스크래핑 테스트 진입...")
    res = run_us_market_update()
    print("수집완료:", res)
