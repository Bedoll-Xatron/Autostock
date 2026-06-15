"""눌림 대기 진입 단위 테스트."""
from datetime import time as dtime
from unittest.mock import AsyncMock, patch

import pytest

from autostock.trading.limit_order import (
    PULLBACK_TIMEOUT,
    PULLBACK_WAIT_START,
    PULLBACK_TOLERANCE_PCT,
    _wait_for_pullback,
    limit_buy_with_pullback,
)


def _bars(close: float, count: int = 5) -> list[dict]:
    return [{"close": close}] * count


@pytest.mark.unit
@pytest.mark.anyio
async def test_pullback_timeout_returns_false():
    """09:45 이후에는 눌림 대기 없이 즉시 포기."""
    past_timeout = dtime(9, 46)
    with patch("autostock.trading.limit_order._get_now_kst", return_value=past_timeout):
        success, target = await _wait_for_pullback("000000")
    assert success is False
    assert target == 0


@pytest.mark.unit
@pytest.mark.anyio
async def test_pullback_before_window_sleeps_then_gives_up(monkeypatch):
    """09:15 이전 → 대기 후 09:45 초과 → 포기."""
    call_count = 0

    def mock_time():
        nonlocal call_count
        call_count += 1
        # 첫 호출은 09:10 (대기 구간 이전), 두 번째는 09:46 (타임아웃)
        return dtime(9, 10) if call_count == 1 else dtime(9, 46)

    with (
        patch("autostock.trading.limit_order._get_now_kst", side_effect=mock_time),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        success, target = await _wait_for_pullback("000000")
    assert success is False


@pytest.mark.unit
@pytest.mark.anyio
async def test_pullback_success_within_tolerance():
    """5MA=10000, 현재가=10030 → 0.3% 차이 → 눌림 도달."""
    ma_price = 10000.0
    cur_price = 10030.0  # 0.3% 차이 — PULLBACK_TOLERANCE_PCT(0.5%) 이내

    in_window = dtime(9, 20)
    with (
        patch("autostock.trading.limit_order._get_now_kst", return_value=in_window),
        patch("autostock.trading.limit_order.get_intraday_5min", return_value=_bars(ma_price)),
        patch("autostock.trading.limit_order.get_current_price", return_value=cur_price),
    ):
        success, target = await _wait_for_pullback("000000")
    assert success is True
    assert target > 0


@pytest.mark.unit
@pytest.mark.anyio
async def test_pullback_outside_tolerance_waits():
    """5MA=10000, 현재가=10200 → 2% 차이 → 도달 안 함, 09:45 이후 포기."""
    ma_price  = 10000.0
    cur_price = 10200.0  # 2% 차이 — 허용 범위 초과

    call_count = 0
    def mock_time():
        nonlocal call_count
        call_count += 1
        return dtime(9, 20) if call_count <= 2 else dtime(9, 46)

    with (
        patch("autostock.trading.limit_order._get_now_kst", side_effect=mock_time),
        patch("autostock.trading.limit_order.get_intraday_5min", return_value=_bars(ma_price)),
        patch("autostock.trading.limit_order.get_current_price", return_value=cur_price),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        success, target = await _wait_for_pullback("000000")
    assert success is False


@pytest.mark.unit
@pytest.mark.anyio
async def test_limit_buy_with_pullback_no_pullback_returns_false():
    """눌림 미발생(포기) 시 limit_buy_with_pullback은 (0, 0) 반환."""
    past_timeout = dtime(9, 46)
    with patch("autostock.trading.limit_order._get_now_kst", return_value=past_timeout):
        filled_qty, price = await limit_buy_with_pullback("000000", qty=10)
    assert filled_qty == 0
    assert price == 0


@pytest.mark.unit
@pytest.mark.anyio
async def test_limit_buy_with_pullback_fills_on_success():
    """눌림 도달 → 전량 체결 시 (qty, target) 반환."""
    ma_price = 10000.0
    cur_price = 10030.0
    in_window = dtime(9, 20)

    with (
        patch("autostock.trading.limit_order._get_now_kst", return_value=in_window),
        patch("autostock.trading.limit_order.get_intraday_5min", return_value=_bars(ma_price)),
        patch("autostock.trading.limit_order.get_current_price", return_value=cur_price),
        patch("autostock.trading.limit_order.limit_buy",
              return_value={"output": {"ODNO": "ORD001"}}),
        patch("autostock.trading.limit_order.get_order_fill_qty", return_value=10),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        filled_qty, fill_price = await limit_buy_with_pullback("000000", qty=10)
    assert filled_qty == 10
    assert fill_price > 0


@pytest.mark.unit
@pytest.mark.anyio
async def test_limit_buy_with_pullback_partial_fill_returns_partial():
    """부분 체결(타임아웃) 시 체결분만 반환하고 잔량 취소."""
    ma_price = 10000.0
    cur_price = 10030.0
    in_window = dtime(9, 20)

    with (
        patch("autostock.trading.limit_order._get_now_kst", return_value=in_window),
        patch("autostock.trading.limit_order.get_intraday_5min", return_value=_bars(ma_price)),
        patch("autostock.trading.limit_order.get_current_price", return_value=cur_price),
        patch("autostock.trading.limit_order.limit_buy",
              return_value={"output": {"ODNO": "ORD001"}}),
        patch("autostock.trading.limit_order.get_order_fill_qty", return_value=4),  # 10주 중 4주만
        patch("autostock.trading.limit_order.cancel_order", return_value=True) as cancel,
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        filled_qty, fill_price = await limit_buy_with_pullback("000000", qty=10)
    assert filled_qty == 4
    assert fill_price > 0
    cancel.assert_called_once()
