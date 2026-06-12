"""기술지표 계산 — RSI, MACD, 손절가."""
import pandas as pd


def calc_rsi(df: pd.DataFrame, period: int = 14) -> float:
    """RSI 계산. 데이터 부족 시 50 반환."""
    try:
        close = df["Close"].dropna()
        if len(close) < period + 1:
            return 50.0
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=period - 1, min_periods=period).mean().iloc[-1]
        avg_loss = loss.ewm(com=period - 1, min_periods=period).mean().iloc[-1]
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)
    except Exception:
        return 50.0


def calc_macd(df: pd.DataFrame) -> dict:
    """MACD 계산. 실패 시 기본값 반환."""
    try:
        close = df["Close"].dropna()
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        macd_val = round(float(macd_line.iloc[-1]), 2)
        signal_val = round(float(signal_line.iloc[-1]), 2)
        return {
            "macd": macd_val,
            "signal": signal_val,
            "histogram": round(macd_val - signal_val, 2),
            "description": "골든크로스" if macd_val > signal_val else "데드크로스",
        }
    except Exception:
        return {"macd": 0.0, "signal": 0.0, "histogram": 0.0, "description": "계산불가"}


def calc_stop_loss(df: pd.DataFrame, multiplier: float = 1.5) -> float:
    """ATR 기반 손절가 계산."""
    try:
        high = df["High"]
        low = df["Low"]
        close = df["Close"]
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        current_price = float(close.iloc[-1])
        return round(current_price - multiplier * atr, 0)
    except Exception:
        try:
            return round(float(df["Close"].iloc[-1]) * 0.9, 0)
        except Exception:
            return 0.0


def determine_trend(df: pd.DataFrame) -> str:
    """20일 이동평균 기준 추세 판단."""
    try:
        close = df["Close"]
        ma20 = close.rolling(20).mean().iloc[-1]
        current = float(close.iloc[-1])
        if current > ma20 * 1.02:
            return "상승"
        if current < ma20 * 0.98:
            return "하락"
        return "횡보"
    except Exception:
        return "횡보"
