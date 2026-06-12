from dataclasses import dataclass, field
from typing import Optional
import pandas as pd

from config import VCPConfig, VCPGradeParams
from indicators import ema, fractal_swings, tightness_score, vdu_ratio


@dataclass
class VCPResult:
    detected: bool
    grade: str = ""
    pivot_high: float = 0
    c1: float = 0
    c2: float = 0
    c3: float = 0
    r12: float = 0
    r23: float = 0
    score: int = 0
    swing_points: list = field(default_factory=list)
    l3_proxy: bool = False  # L3가 프록시(최근 저가)로 대체되었는지
    tightness: float = 0.0  # 최근 5일 변동폭 (%)
    vdu_ratio: float = 0.0  # 거래량 마름 비율 (0~1)
    is_surge_candidate: bool = False  # 급성장 후보 여부 (S등급)


def detect_vcp(df: pd.DataFrame, config: Optional[VCPConfig] = None) -> VCPResult:
    if config is None:
        config = VCPConfig()

    if len(df) < config.lookback:
        return VCPResult(detected=False)

    close = df["close"]
    e20 = ema(close, 20)
    e60 = ema(close, 60)

    for params, grade_name in config.all_grades():
        if not _check_trend(close, e20, e60, params.trend_mode):
            continue

        result = _extract_vcp_from_swings(
            df, config.swing_k, config.lookback, params,
        )
        if result is not None:
            result.grade = grade_name
            
            # --- 에너지 응축(Tightness & VDU) 분석 추가 ---
            result.tightness = tightness_score(df, period=5)
            result.vdu_ratio = vdu_ratio(df, avg_period=20)
            
            # S등급 판정 기준: 타이트함 < 4% AND 거래량 마름 < 0.4 AND 기존 등급 A or B
            if result.tightness < 4.5 and result.vdu_ratio < 0.45 and result.grade in ["A", "B"]:
                result.is_surge_candidate = True
                
            return result

    return VCPResult(detected=False)


def _check_trend(close: pd.Series, e20: pd.Series, e60: pd.Series, mode: str) -> bool:
    c = close.iloc[-1]
    m20 = e20.iloc[-1]
    m60 = e60.iloc[-1]

    if mode == "STRICT":
        return c > m20 > m60 and e60.iloc[-1] > e60.iloc[-6]
    elif mode == "ABOVE_MA20":
        return c > m20
    elif mode == "ABOVE_MA60":
        return c > m60
    else:  # ANY
        return True


def _extract_vcp_from_swings(
    df: pd.DataFrame,
    k: int,
    lookback: int,
    params: VCPGradeParams,
) -> Optional[VCPResult]:
    tail = df.iloc[-lookback:].reset_index(drop=True)
    swings = fractal_swings(tail, k=k)

    swing_highs = [s for s in swings if s["type"] == "H"]
    swing_lows = [s for s in swings if s["type"] == "L"]

    if len(swing_highs) < 3 or len(swing_lows) < 2:
        return None

    # 최근 3개 고점
    h1, h2, h3 = swing_highs[-3], swing_highs[-2], swing_highs[-1]

    # H1-H2 사이 최저점
    lows_between_h1_h2 = [s for s in swing_lows if h1["i"] < s["i"] < h2["i"]]
    if not lows_between_h1_h2:
        return None
    l1 = min(lows_between_h1_h2, key=lambda s: s["price"])

    # H2-H3 사이 최저점
    lows_between_h2_h3 = [s for s in swing_lows if h2["i"] < s["i"] < h3["i"]]
    if not lows_between_h2_h3:
        return None
    l2 = min(lows_between_h2_h3, key=lambda s: s["price"])

    # H3 이후 저점 (없으면 최근 저가로 대체)
    l3_proxy = False
    lows_after_h3 = [s for s in swing_lows if s["i"] > h3["i"]]
    if lows_after_h3:
        l3 = min(lows_after_h3, key=lambda s: s["price"])
    else:
        recent_low = tail["low"].iloc[h3["i"]:].min()
        l3 = {"i": len(tail) - 1, "type": "L", "price": float(recent_low)}
        l3_proxy = True

    # 수축률 계산
    c1 = (h1["price"] - l1["price"]) / h1["price"] * 100
    c2 = (h2["price"] - l2["price"]) / h2["price"] * 100
    c3 = (h3["price"] - l3["price"]) / h3["price"] * 100

    # C1 > C2 > C3 > 0 확인
    if not (c1 > c2 > c3 > 0):
        return None

    # 수축 비율 확인
    r12 = c1 / c2
    r23 = c2 / c3
    if r12 < params.min_r12 or r23 < params.min_r23:
        return None

    # 고점 하강 확인
    if params.require_descending_highs:
        if not (h1["price"] > h2["price"] > h3["price"]):
            return None

    # 저점 상승 확인
    if params.require_ascending_lows:
        if not (l1["price"] < l2["price"] < l3["price"]):
            return None

    return VCPResult(
        detected=True,
        pivot_high=h3["price"],
        c1=round(c1, 2),
        c2=round(c2, 2),
        c3=round(c3, 2),
        r12=round(r12, 2),
        r23=round(r23, 2),
        swing_points=swings,
        l3_proxy=l3_proxy,
    )


def score_vcp(result: VCPResult, atrp: float, config: Optional[VCPConfig] = None) -> int:
    if not result.detected:
        return 0

    if config is None:
        config = VCPConfig()

    # --- 1. Decay quality (50점) ---

    # r12 점수 (0~20): 1.0 ~ 1.2 선형, 1.2 이상 만점
    r12_score = min((result.r12 - 1.0) / 0.2, 1.0) * 20

    # r23 점수 (0~15): 1.0 ~ 1.15 선형, 1.15 이상 만점
    r23_score = min((result.r23 - 1.0) / 0.15, 1.0) * 15

    # C3 타이트함 (0~15): C3가 낮을수록 좋음
    c3_range = config.c3_hi - config.c3_lo
    c3_clamped = max(config.c3_lo, min(result.c3, config.c3_hi))
    c3_score = (1.0 - (c3_clamped - config.c3_lo) / c3_range) * 15

    decay_score = r12_score + r23_score + c3_score

    # --- 2. ATR quality (25점) ---
    atrp_range = config.atrp_hi - config.atrp_lo
    atrp_clamped = max(config.atrp_lo, min(atrp, config.atrp_hi))
    atr_score = (1.0 - (atrp_clamped - config.atrp_lo) / atrp_range) * 25

    # --- 3. Structure quality (25점) ---
    structure_score = 0.0

    # 고점 하강 보너스 (12.5점)
    highs = [s for s in result.swing_points if s["type"] == "H"]
    if len(highs) >= 3:
        h1, h2, h3 = highs[-3], highs[-2], highs[-1]
        if h1["price"] > h2["price"] > h3["price"]:
            structure_score += 12.5

    # 저점 상승 보너스 (12.5점)
    lows = [s for s in result.swing_points if s["type"] == "L"]
    if len(lows) >= 2:
        l1, l2 = lows[-2], lows[-1]
        if l1["price"] < l2["price"]:
            structure_score += 12.5

    # --- 페널티 ---
    penalty = 0
    if result.l3_proxy:
        penalty = 15

    total = decay_score + atr_score + structure_score - penalty
    return int(max(0, min(100, round(total))))
