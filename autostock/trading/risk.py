"""변동성 기반 손절폭 계산."""
import pandas as pd
from autostock.market.fetcher import fetch_ohlcv
from autostock.logger import get_logger

log = get_logger(__name__)

ATR_PERIOD = 14
ATR_MULTIPLIER = 1.5
STOP_PCT_FLOOR = 2.5
STOP_PCT_CEIL  = 6.0


def compute_atrp(ticker: str) -> float:
    """ATRP(%) = ATR(14) / 종가 × 100. 실패 시 0 반환."""
    try:
        df = fetch_ohlcv(ticker, days=30)
        if df is None or len(df) < ATR_PERIOD + 1:
            return 0.0
        h, l, c = df["High"], df["Low"], df["Close"]
        prev_c = c.shift(1)
        tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        atr = tr.rolling(ATR_PERIOD).mean().iloc[-1]
        close = c.iloc[-1]
        if close <= 0:
            return 0.0
        return float(atr / close * 100)
    except Exception as e:
        log.warning("[%s] ATRP 산출 실패: %s — 기본값 사용", ticker, e)
        return 0.0


def compute_stop_pct(ticker: str) -> float:
    """ATRP × 1.5, [2.5, 6.0] 클램프. 산출 실패 시 3.0 폴백."""
    atrp = compute_atrp(ticker)
    if atrp <= 0:
        return 3.0
    return max(STOP_PCT_FLOOR, min(STOP_PCT_CEIL, round(atrp * ATR_MULTIPLIER, 2)))
