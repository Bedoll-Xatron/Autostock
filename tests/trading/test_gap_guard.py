"""갭업 진입 차단 로직 단위 테스트."""
import os
import pytest

GAP_UP_BLOCK_PCT = 3.0


def _calc_gap(prev_close: float, open_price: float) -> float:
    if prev_close > 0 and open_price > 0:
        return (open_price / prev_close - 1) * 100
    return 0.0


def _should_block(prev_close: float, open_price: float) -> bool:
    gap = _calc_gap(prev_close, open_price)
    return gap >= GAP_UP_BLOCK_PCT


class TestGapGuard:
    def test_gap_up_blocks_buy(self):
        """갭업 3% 초과 시 진입 차단."""
        assert _should_block(prev_close=10_000, open_price=10_350) is True

    def test_gap_exactly_at_threshold_blocks(self):
        """갭업 정확히 3.0% 이상이면 차단."""
        assert _should_block(prev_close=10_000, open_price=10_300) is True

    def test_gap_below_threshold_allows(self):
        """갭업 2.9%이면 허용."""
        assert _should_block(prev_close=10_000, open_price=10_290) is False

    def test_gap_neutral_allows_buy(self):
        """갭없음(0%)이면 허용."""
        assert _should_block(prev_close=10_000, open_price=10_000) is False

    def test_gap_down_allows_buy(self):
        """갭다운이면 허용."""
        assert _should_block(prev_close=10_000, open_price=9_800) is False

    def test_missing_prev_close_allows_buy(self):
        """전일 종가 없으면 차단하지 않음 (안전 fallback)."""
        assert _should_block(prev_close=0, open_price=10_300) is False

    def test_missing_open_price_allows_buy(self):
        """현재가 조회 실패(0)이면 차단하지 않음 (안전 fallback)."""
        assert _should_block(prev_close=10_000, open_price=0) is False

    @pytest.mark.parametrize("gap_pct,expected_block", [
        (2.9, False),
        (3.0, True),
        (5.0, True),
        (10.0, True),
        (-1.0, False),
    ])
    def test_gap_boundary_cases(self, gap_pct: float, expected_block: bool):
        """경계값 파라미터 테스트."""
        open_price = 10_000 * (1 + gap_pct / 100)
        assert _should_block(10_000, open_price) is expected_block
