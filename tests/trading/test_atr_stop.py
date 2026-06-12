"""ATR 기반 손절폭(compute_stop_pct) 단위 테스트."""
import pandas as pd
import pytest
from unittest.mock import patch

from autostock.trading.risk import (
    compute_atrp,
    compute_stop_pct,
    ATR_PERIOD,
    ATR_MULTIPLIER,
    STOP_PCT_FLOOR,
    STOP_PCT_CEIL,
)


def _make_ohlcv(n: int = 20, atr_val: float = 500.0, close_val: float = 10_000.0) -> pd.DataFrame:
    """ATR이 atr_val에 수렴하는 고정 OHLCV DataFrame 생성."""
    rows = []
    for i in range(n):
        rows.append({
            "High":  close_val + atr_val,
            "Low":   close_val - atr_val,
            "Close": close_val,
        })
    return pd.DataFrame(rows)


class TestComputeAtrp:
    def test_returns_correct_atrp(self):
        """ATRP = ATR / Close × 100 — 정상 데이터에서 올바른 값을 반환."""
        df = _make_ohlcv(n=20, atr_val=500, close_val=10_000)
        with patch("autostock.trading.risk.fetch_ohlcv", return_value=df):
            atrp = compute_atrp("005930")
        # TR = High - Low = 1000, ATR = 1000, ATRP = 1000/10000 * 100 = 10.0
        assert abs(atrp - 10.0) < 0.01

    def test_returns_zero_on_none(self):
        """fetch_ohlcv가 None을 반환하면 0.0 반환."""
        with patch("autostock.trading.risk.fetch_ohlcv", return_value=None):
            assert compute_atrp("005930") == 0.0

    def test_returns_zero_on_insufficient_data(self):
        """데이터 부족(ATR_PERIOD 미만)이면 0.0 반환."""
        df = _make_ohlcv(n=ATR_PERIOD - 1)
        with patch("autostock.trading.risk.fetch_ohlcv", return_value=df):
            assert compute_atrp("005930") == 0.0

    def test_returns_zero_on_exception(self):
        """fetch_ohlcv 예외 발생 시 0.0 반환."""
        with patch("autostock.trading.risk.fetch_ohlcv", side_effect=RuntimeError("timeout")):
            assert compute_atrp("005930") == 0.0

    def test_returns_zero_when_close_zero(self):
        """종가가 0이면 0.0 반환 (ZeroDivision 방지)."""
        df = _make_ohlcv(n=20, close_val=0)
        with patch("autostock.trading.risk.fetch_ohlcv", return_value=df):
            assert compute_atrp("005930") == 0.0


class TestComputeStopPct:
    def test_fallback_when_atrp_zero(self):
        """ATRP 산출 실패 시 3.0% 폴백."""
        with patch("autostock.trading.risk.compute_atrp", return_value=0.0):
            assert compute_stop_pct("005930") == 3.0

    def test_applies_multiplier(self):
        """ATRP × ATR_MULTIPLIER가 적용된다."""
        atrp = 3.0  # 3.0 × 1.5 = 4.5 → [2.5, 6.0] 범위 내
        with patch("autostock.trading.risk.compute_atrp", return_value=atrp):
            result = compute_stop_pct("005930")
        expected = round(atrp * ATR_MULTIPLIER, 2)
        assert result == pytest.approx(expected, abs=0.01)

    def test_clamps_at_floor(self):
        """ATRP × multiplier < STOP_PCT_FLOOR 이면 FLOOR로 클램프."""
        with patch("autostock.trading.risk.compute_atrp", return_value=0.5):
            assert compute_stop_pct("005930") == STOP_PCT_FLOOR

    def test_clamps_at_ceil(self):
        """ATRP × multiplier > STOP_PCT_CEIL 이면 CEIL로 클램프."""
        with patch("autostock.trading.risk.compute_atrp", return_value=10.0):
            assert compute_stop_pct("005930") == STOP_PCT_CEIL

    @pytest.mark.parametrize("atrp,expected", [
        (1.0, STOP_PCT_FLOOR),          # 1.0 × 1.5 = 1.5 → floor
        (1.67, STOP_PCT_FLOOR),         # 1.67 × 1.5 = 2.505 → barely above floor
        (3.0, 4.5),                     # 3.0 × 1.5 = 4.5 → mid range
        (4.0, STOP_PCT_CEIL),           # 4.0 × 1.5 = 6.0 → exactly ceil
        (5.0, STOP_PCT_CEIL),           # 5.0 × 1.5 = 7.5 → ceil
    ])
    def test_boundary_cases(self, atrp: float, expected: float):
        """경계값 파라미터 테스트."""
        with patch("autostock.trading.risk.compute_atrp", return_value=atrp):
            result = compute_stop_pct("005930")
        assert result == pytest.approx(expected, abs=0.01)
