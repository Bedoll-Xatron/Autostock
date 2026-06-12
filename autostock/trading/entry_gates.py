"""체결 직전 안전 게이트 — 점수 임계값 변경 없이 약신호 차단."""
from typing import Optional

import pandas as pd

from autostock import config
from autostock.market.fetcher import fetch_ohlcv
from autostock.logger import get_logger

log = get_logger(__name__)

# ── 게이트 임계값 ──────────────────────────────────────────────────────────
# 실거래 전환 시 자동 강화 (09_점수임계값_검토_가이드 §6.2)
EXTENDED_CHANGE_PCT  = 5.0 if not config.KIS_SIMULATED_MODE else 7.0
RVOL_MIN_RATIO       = 1.5 if not config.KIS_SIMULATED_MODE else 1.3
MA20_PROXIMITY_PCT   = -2.0  # MA20 대비 -2% 이하: 추세 훼손 차단


def _vol_ratio(df: pd.DataFrame) -> float:
    """오늘 거래량 / 최근 20일 평균 거래량."""
    if len(df) < 21:
        return 0.0
    vol_20avg = df["Volume"].iloc[-21:-1].mean()
    if vol_20avg <= 0:
        return 0.0
    return float(df["Volume"].iloc[-1] / vol_20avg)


def _ma20_distance_pct(df: pd.DataFrame) -> float:
    """현재가 기준 MA20 대비 거리 (%)."""
    if len(df) < 20:
        return 0.0
    ma20 = df["Close"].iloc[-20:].mean()
    cur = float(df["Close"].iloc[-1])
    if ma20 <= 0:
        return 0.0
    return (cur / ma20 - 1) * 100


def check_entry_gates(
    ticker: str,
    change_pct: float,
    cur_price: float,
) -> Optional[str]:
    """체결 직전 게이트 검사.

    Returns:
        None                 모든 게이트 통과
        "BLOCK_EXTENDED"     당일 등락률 과대 (추격매수)
        "BLOCK_RVOL"         거래량 폭발 부재
        "BLOCK_MA20"         MA20 추세 훼손
    """
    # 1. 추격매수 차단 (OHLCV 조회 불필요)
    if change_pct >= EXTENDED_CHANGE_PCT:
        log.warning(
            "[%s] 게이트 차단 EXTENDED — change_pct=%+.1f%% >= %.1f%%",
            ticker, change_pct, EXTENDED_CHANGE_PCT,
        )
        return "BLOCK_EXTENDED"

    df = fetch_ohlcv(ticker, days=30)
    if df is None or df.empty:
        log.warning("[%s] OHLCV 조회 실패 — 게이트 통과 (보수적 패스)", ticker)
        return None

    # 2. 거래량 폭발 확인
    rvol = _vol_ratio(df)
    if 0 < rvol < RVOL_MIN_RATIO:
        log.warning(
            "[%s] 게이트 차단 RVOL — ratio=%.2f < %.2f",
            ticker, rvol, RVOL_MIN_RATIO,
        )
        return "BLOCK_RVOL"

    # 3. MA20 추세 훼손 확인
    ma20_dist = _ma20_distance_pct(df)
    if ma20_dist <= MA20_PROXIMITY_PCT:
        log.warning(
            "[%s] 게이트 차단 MA20 — 거리 %+.2f%% <= %.1f%%",
            ticker, ma20_dist, MA20_PROXIMITY_PCT,
        )
        return "BLOCK_MA20"

    log.info(
        "[%s] 게이트 통과: change=%+.1f%% rvol=%.2f ma20_dist=%+.2f%%",
        ticker, change_pct, rvol, ma20_dist,
    )
    return None
