import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def fractal_swings(df: pd.DataFrame, k: int = 3) -> list[dict]:
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    raw: list[dict] = []

    for i in range(k, n - k):
        if all(highs[i] > highs[i - j] and highs[i] > highs[i + j] for j in range(1, k + 1)):
            raw.append({"i": i, "type": "H", "price": float(highs[i])})
        if all(lows[i] < lows[i - j] and lows[i] < lows[i + j] for j in range(1, k + 1)):
            raw.append({"i": i, "type": "L", "price": float(lows[i])})

    raw.sort(key=lambda x: x["i"])

    # 연속 같은 타입 → 더 극단적인 것만 남기기
    result: list[dict] = []
    for swing in raw:
        if result and result[-1]["type"] == swing["type"]:
            prev = result[-1]
            if swing["type"] == "H" and swing["price"] > prev["price"]:
                result[-1] = swing
            elif swing["type"] == "L" and swing["price"] < prev["price"]:
                result[-1] = swing
        else:
            result.append(swing)

    return result


def tightness_score(df: pd.DataFrame, period: int = 5) -> float:
    """최근 N일간의 가격 밀집도(Tightness)를 계산한다. (낮을수록 밀집)
    (MaxHigh - MinLow) / AvgClose * 100
    """
    if len(df) < period:
        return 999.0
    tail = df.iloc[-period:]
    high = tail["high"].max()
    low = tail["low"].min()
    avg_close = tail["close"].mean()
    if avg_close == 0:
        return 999.0
    return (high - low) / avg_close * 100


def vdu_ratio(df: pd.DataFrame, avg_period: int = 20) -> float:
    """오늘의 거래량이 과거 평균 대비 어느 정도인지 계산한다. (Volume Dry-up)
    Today Volume / Avg Volume — 낮을수록(< 0.6) 고갈 신호
    """
    if len(df) < avg_period + 1:
        return 1.0
    today_vol = df["volume"].iloc[-1]
    avg_vol = df["volume"].iloc[-(avg_period + 1) : -1].mean()
    if avg_vol == 0:
        return 1.0
    return today_vol / avg_vol


def rvol_ratio(df: pd.DataFrame, avg_period: int = 20) -> float:
    """상대거래량(RVOL): 오늘 거래량 / 최근 N일 평균 거래량.
    높을수록(>= 2.5) 폭발적 매집 신호 — vdu_ratio와 계산식은 동일하나 해석 방향이 반대.
    """
    if len(df) < avg_period + 1:
        return 1.0
    today_vol = df["volume"].iloc[-1]
    avg_vol = df["volume"].iloc[-(avg_period + 1) : -1].mean()
    if avg_vol == 0:
        return 1.0
    return today_vol / avg_vol
