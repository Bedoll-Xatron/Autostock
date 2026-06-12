"""체결 직전 안전 게이트 단위 테스트."""
import pandas as pd
import pytest

from autostock.trading.entry_gates import (
    EXTENDED_CHANGE_PCT,
    MA20_PROXIMITY_PCT,
    RVOL_MIN_RATIO,
    check_entry_gates,
)


def _make_df(close_prices: list[float], volumes: list[int]) -> pd.DataFrame:
    return pd.DataFrame({"Close": close_prices, "Volume": volumes})


def _pass_df() -> pd.DataFrame:
    """모든 게이트를 통과하는 정상 데이터 (22일치)."""
    return _make_df(
        close_prices=[10000] * 22,
        volumes=[1_000_000] * 21 + [1_500_000],  # RVOL = 1.5
    )


# ── EXTENDED 차단 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_extended_blocks(monkeypatch):
    monkeypatch.setattr("autostock.trading.entry_gates.fetch_ohlcv", lambda t, days: _pass_df())
    assert check_entry_gates("000000", EXTENDED_CHANGE_PCT, 10000) == "BLOCK_EXTENDED"


@pytest.mark.unit
def test_extended_blocks_above_threshold(monkeypatch):
    monkeypatch.setattr("autostock.trading.entry_gates.fetch_ohlcv", lambda t, days: _pass_df())
    assert check_entry_gates("000000", EXTENDED_CHANGE_PCT + 1.0, 10000) == "BLOCK_EXTENDED"


@pytest.mark.unit
def test_extended_passes_below_threshold(monkeypatch):
    monkeypatch.setattr("autostock.trading.entry_gates.fetch_ohlcv", lambda t, days: _pass_df())
    assert check_entry_gates("000000", EXTENDED_CHANGE_PCT - 0.1, 10000) is None


# ── RVOL 차단 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_low_rvol_blocks(monkeypatch):
    # RVOL = 900_000 / 1_000_000 = 0.9 → 차단
    df = _make_df(
        close_prices=[10000] * 22,
        volumes=[1_000_000] * 21 + [900_000],
    )
    monkeypatch.setattr("autostock.trading.entry_gates.fetch_ohlcv", lambda t, days: df)
    assert check_entry_gates("000000", 3.0, 10000) == "BLOCK_RVOL"


@pytest.mark.unit
def test_rvol_exactly_at_min_passes(monkeypatch):
    # RVOL = 1.3 → 통과 (경계값: < 1.3만 차단)
    df = _make_df(
        close_prices=[10000] * 22,
        volumes=[1_000_000] * 21 + [1_300_000],
    )
    monkeypatch.setattr("autostock.trading.entry_gates.fetch_ohlcv", lambda t, days: df)
    assert check_entry_gates("000000", 3.0, 10000) is None


@pytest.mark.unit
def test_rvol_zero_volume_passes(monkeypatch):
    # 평균 거래량 0 → ratio 계산 불가 → 보수적 통과
    df = _make_df(
        close_prices=[10000] * 22,
        volumes=[0] * 22,
    )
    monkeypatch.setattr("autostock.trading.entry_gates.fetch_ohlcv", lambda t, days: df)
    assert check_entry_gates("000000", 3.0, 10000) is None


# ── MA20 차단 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_ma20_break_blocks(monkeypatch):
    # MA20 ≈ 10000, 현재 9700 → < -2.0% → 차단
    df = _make_df(
        close_prices=[10000] * 20 + [9700] + [9700],  # 22행 (volumes 22행과 일치)
        volumes=[1_000_000] * 21 + [1_500_000],
    )
    monkeypatch.setattr("autostock.trading.entry_gates.fetch_ohlcv", lambda t, days: df)
    assert check_entry_gates("000000", 0.5, 9700) == "BLOCK_MA20"


@pytest.mark.unit
def test_ma20_at_threshold_passes(monkeypatch):
    # MA20 ≈ 9980, 현재 9800 → -1.8% → 경계값 통과 (≤ -2% 만 차단)
    df = _make_df(
        close_prices=[10000] * 20 + [9800] + [9800],
        volumes=[1_000_000] * 21 + [1_500_000],
    )
    monkeypatch.setattr("autostock.trading.entry_gates.fetch_ohlcv", lambda t, days: df)
    assert check_entry_gates("000000", 0.5, 9800) is None


# ── OHLCV 실패 패스 ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_ohlcv_failure_passes(monkeypatch):
    monkeypatch.setattr(
        "autostock.trading.entry_gates.fetch_ohlcv",
        lambda t, days: pd.DataFrame(),
    )
    assert check_entry_gates("000000", 3.0, 10000) is None


@pytest.mark.unit
def test_insufficient_rows_passes(monkeypatch):
    # 데이터 10일치만 → RVOL/MA20 계산 불가 → 통과
    df = _make_df(close_prices=[10000] * 10, volumes=[1_000_000] * 10)
    monkeypatch.setattr("autostock.trading.entry_gates.fetch_ohlcv", lambda t, days: df)
    assert check_entry_gates("000000", 3.0, 10000) is None
