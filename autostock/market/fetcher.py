"""OHLCV, 기초데이터 수집 — FinanceDataReader + NAVER Finance API."""
from datetime import datetime, timedelta
import urllib.request
import json
import re
import pandas as pd
import FinanceDataReader as fdr

from autostock.logger import get_logger

log = get_logger(__name__)

_NAVER_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _last_trading_day() -> str:
    """오늘 기준 가장 최근 거래일 반환 (월요일→금요일, 주말→금요일)."""
    today = datetime.today()
    wd = today.weekday()  # 0=월, 5=토, 6=일
    if wd == 0:
        return (today - timedelta(days=3)).strftime("%Y%m%d")
    elif wd == 5:
        return (today - timedelta(days=1)).strftime("%Y%m%d")
    elif wd == 6:
        return (today - timedelta(days=2)).strftime("%Y%m%d")
    return today.strftime("%Y%m%d")


def fetch_ohlcv(ticker: str, days: int = 60) -> pd.DataFrame:
    """일봉 OHLCV 조회. 실패 시 빈 DataFrame 반환."""
    end = datetime.today()
    start = end - timedelta(days=days)
    try:
        df = fdr.DataReader(ticker, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        return df
    except Exception as e:
        log.warning("fetch_ohlcv %s failed: %s", ticker, e)
        return pd.DataFrame()


def _naver_parse_number(value: str) -> float:
    """'30.66배', '-6,564원', 'N/A' 등에서 숫자 추출. 음수 보존."""
    if not value or value.strip().upper() in ("N/A", "-", ""):
        return 0.0
    negative = value.lstrip().startswith("-")
    cleaned = re.sub(r"[^\d.]", "", value.replace(",", ""))
    try:
        result = float(cleaned)
        return -result if negative else result
    except ValueError:
        return 0.0


def fetch_fundamental(ticker: str) -> dict:
    """PER, PBR, EPS 조회 (NAVER Finance 모바일 API). 실패 시 기본값 반환."""
    try:
        url = f"https://m.stock.naver.com/api/stock/{ticker}/integration"
        req = urllib.request.Request(url, headers=_NAVER_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())

        info_map = {item["code"]: item["value"] for item in data.get("totalInfos", [])}
        per = _naver_parse_number(info_map.get("per", ""))
        pbr = _naver_parse_number(info_map.get("pbr", ""))
        eps = _naver_parse_number(info_map.get("eps", ""))
        return {"per": per, "pbr": pbr, "eps": eps}
    except Exception as e:
        log.warning("fetch_fundamental %s failed: %s", ticker, e)
        return {"per": 0.0, "pbr": 0.0, "eps": 0.0}


def fetch_roe(ticker: str) -> float:
    """ROE 조회. NAVER Finance에서 EPS/BPS 비율로 추정. 실패 시 0 반환."""
    try:
        url = f"https://m.stock.naver.com/api/stock/{ticker}/integration"
        req = urllib.request.Request(url, headers=_NAVER_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())

        info_map = {item["code"]: item["value"] for item in data.get("totalInfos", [])}
        eps = _naver_parse_number(info_map.get("eps", ""))
        bps = _naver_parse_number(info_map.get("bps", ""))
        if bps > 0:
            return round(eps / bps * 100, 2)
        return 0.0
    except Exception as e:
        log.warning("fetch_roe %s failed: %s", ticker, e)
        return 0.0


def fetch_investor_trend(ticker: str, days: int = 5) -> dict:
    """최근 N일 외국인/기관 순매수 조회 (NAVER Finance 모바일 API)."""
    try:
        url = f"https://m.stock.naver.com/api/stock/{ticker}/integration"
        req = urllib.request.Request(url, headers=_NAVER_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())

        trends = data.get("dealTrendInfos", [])[:days]
        if not trends:
            raise ValueError("no dealTrendInfos")

        foreign_net = sum(
            int(t.get("foreignerPureBuyQuant", "0").replace(",", "").replace("+", ""))
            for t in trends
        )
        inst_net = sum(
            int(t.get("organPureBuyQuant", "0").replace(",", "").replace("+", ""))
            for t in trends
        )
        return {"foreign_net": foreign_net, "inst_net": inst_net}
    except Exception as e:
        log.warning("fetch_investor_trend %s failed: %s", ticker, e)
        return {"foreign_net": 0, "inst_net": 0}


_LARGE_CAP_FALLBACK = {
    "005930", "000660", "035420", "005380", "000270",  # 삼성전자, SK하이닉스, 네이버, 현대차, 기아
    "005490", "005495", "010130", "012330", "033780",  # POSCO홀딩스, 현대모비스, KT&G 등
    "055550", "105560", "086790", "316140",              # 신한, KB, 하나, 우리금융
    "003550", "051910", "006400", "207940",              # LG, LG화학, 삼성SDI, 삼성바이오
    "028260", "066570", "096770", "017670",              # 삼성물산, LG전자, SK이노, SKT
    "030200", "032830", "009150", "015760",              # KT, 삼성생명, 삼성전기, 한국전력
    "034730", "011200", "068270", "000810",              # SK, HMM, 셀트리온, 삼성화재
    "034020", "003670", "010950", "011070", "000100",  # 두산에너빌리티, 포스코퓨처엠, S-Oil, LG이노텍, 유한양행
}


def get_large_cap_tickers() -> set[str]:
    """KOSPI 200 구성 종목 코드 반환 (BoB 제외용 — 대형주 필터).

    pykrx 실패 시 상위 대형주 하드코딩 폴백으로 전환.
    """
    try:
        from pykrx import stock
        import datetime

        today = datetime.datetime.now().strftime("%Y%m%d")
        tickers = stock.get_index_portfolio_deposit_file(today, "1028")  # KOSPI 200
        if tickers:
            log.info("get_large_cap_tickers: KOSPI 200 구성 %d종목", len(tickers))
            return set(tickers)
        raise ValueError("empty result")
    except Exception as e:
        log.warning("get_large_cap_tickers pykrx 실패, 하드코딩 폴백 적용: %s", e)
        return _LARGE_CAP_FALLBACK
