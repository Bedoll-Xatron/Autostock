"""KODEX200 MA50/MA200 기반 시장 국면 감지."""
from datetime import date, timedelta
from enum import Enum

from autostock.logger import get_logger

log = get_logger(__name__)

KODEX200_CODE = "069500"

_SCALE_MAP = {"BULL": 1.0, "CAUTION": 0.7, "BEAR": 0.0}


class RegimeLevel(str, Enum):
    BULL = "BULL"
    CAUTION = "CAUTION"
    BEAR = "BEAR"


def detect_regime(target_date: date | None = None) -> tuple[RegimeLevel, float]:
    """
    KODEX200 종가 기반 MA50/MA200 스코어링으로 시장 국면 판단.

    score 4점 만점:
      current > MA200 → +2
      MA50 > MA200    → +1
      current > MA50  → +1

    Returns:
        (RegimeLevel, position_scale) — BULL=1.0, CAUTION=0.7, BEAR=0.0
    """
    try:
        from pykrx import stock as pykrx_stock
    except ImportError:
        log.warning("pykrx 미설치 — 시장 국면 BULL(기본값) 적용")
        return RegimeLevel.BULL, 1.0

    ref = target_date or date.today()
    end_str = ref.strftime("%Y%m%d")
    start_str = (ref - timedelta(days=320)).strftime("%Y%m%d")

    try:
        df = pykrx_stock.get_market_ohlcv_by_date(start_str, end_str, KODEX200_CODE)
    except Exception as e:
        log.warning("KODEX200 조회 실패 (%s) — BULL(기본값) 적용", e)
        return RegimeLevel.BULL, 1.0

    if df is None or len(df) < 50:
        count = len(df) if df is not None else 0
        log.warning("KODEX200 데이터 부족 (%d행) — BULL(기본값) 적용", count)
        return RegimeLevel.BULL, 1.0

    closes = df["종가"].values.astype(float)
    current = closes[-1]
    ma50 = closes[-50:].mean()
    ma200 = closes[-min(200, len(closes)):].mean()

    score = 0
    if current > ma200:
        score += 2
    if ma50 > ma200:
        score += 1
    if current > ma50:
        score += 1

    if score >= 3:
        level = RegimeLevel.BULL
    elif score >= 2:
        level = RegimeLevel.CAUTION
    else:
        level = RegimeLevel.BEAR

    scale = _SCALE_MAP[level.value]
    log.info(
        "시장 국면: %s (score=%d, current=%.0f, MA50=%.0f, MA200=%.0f, scale=%.1f)",
        level.value, score, current, ma50, ma200, scale,
    )
    return level, scale
