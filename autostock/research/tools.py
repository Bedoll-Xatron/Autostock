"""LangChain @tool 정의 — 기술/기본/감성 지표 조회."""
from langchain_core.tools import tool
from autostock import config
from autostock.market.fetcher import (
    fetch_ohlcv,
    fetch_fundamental,
    fetch_roe,
    fetch_investor_trend,
)
from autostock.market.analyzer import calc_rsi, calc_macd, calc_stop_loss, determine_trend


@tool
def get_technical_indicators(ticker: str) -> dict:
    """주어진 ticker의 기술적 지표(추세, RSI, MACD, 손절가)를 반환한다."""
    df = fetch_ohlcv(ticker, days=60)
    if df.empty:
        return {"error": f"OHLCV 데이터를 가져올 수 없습니다: {ticker}"}

    trend = determine_trend(df)
    rsi = calc_rsi(df)
    macd_data = calc_macd(df)
    stop_loss = calc_stop_loss(df)

    return {
        "ticker": ticker,
        "trend": trend,
        "rsi": rsi,
        "macd": macd_data["description"],
        "macd_value": macd_data["macd"],
        "signal_value": macd_data["signal"],
        "stop_loss_price": stop_loss,
    }


@tool
def get_fundamental_indicators(ticker: str) -> dict:
    """주어진 ticker의 기본 지표(PER, PBR, ROE)를 반환한다."""
    fund = fetch_fundamental(ticker)
    roe = fetch_roe(ticker)

    return {
        "ticker": ticker,
        "per": fund["per"],
        "pbr": fund["pbr"],
        "eps": fund["eps"],
        "roe": roe,
    }


@tool
def get_sentiment_indicators(ticker: str, ticker_name: str) -> dict:
    """주어진 ticker의 감성 지표(뉴스, 외국인/기관 수급)를 반환한다."""
    # 뉴스 검색 (Tavily 사용)
    news_summary = "뉴스 없음"
    if config.TAVILY_API_KEY:
        try:
            from tavily import TavilyClient
            tavily = TavilyClient(api_key=config.TAVILY_API_KEY)
            # 주식 관련 뉴스 검색
            search_query = f"{ticker_name} 주식 최신 뉴스 및 전망"
            response = tavily.search(query=search_query, search_depth="advanced", max_results=5)
            
            results = response.get("results", [])
            if results:
                headlines = [r.get("title", "") for r in results]
                news_summary = " | ".join(headlines)
        except Exception as e:
            news_summary = f"뉴스 검색 에러: {str(e)}"
    else:
        news_summary = "TAVILY_API_KEY 미설정으로 검색 생략"

    # 수급 조회
    investor = fetch_investor_trend(ticker)
    foreign_label = "순매수" if investor["foreign_net"] > 0 else "순매도"
    inst_label = "순매수" if investor["inst_net"] > 0 else "순매도"

    return {
        "ticker": ticker,
        "news_summary": news_summary,
        "foreign_net": f"{investor['foreign_net']:,}주 {foreign_label}",
        "inst_net": f"{investor['inst_net']:,}주 {inst_label}",
    }
