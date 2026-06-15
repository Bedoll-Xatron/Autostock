"""수정사항 검증 테스트 — P0/P1/P2 변경 내역 커버."""
import sys
import os
from datetime import date
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "marketflow", "engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ─────────────────────────────────────────────────────────────────────────────
# [P0-2] 손절 비율 8% 확인
# ─────────────────────────────────────────────────────────────────────────────
class TestStopLossPct:
    def test_marketflow_config_stop_loss_is_8pct(self):
        from marketflow.engine.config import SignalConfig
        cfg = SignalConfig()
        assert cfg.stop_loss_pct == 0.08, f"손절 비율이 8%여야 함, 실제: {cfg.stop_loss_pct}"

    def test_autostock_config_stop_loss_is_8pct(self):
        from autostock import config
        assert config.STOP_LOSS_PCT == 0.08, f"STOP_LOSS_PCT가 8%여야 함, 실제: {config.STOP_LOSS_PCT}"


# ─────────────────────────────────────────────────────────────────────────────
# [P0-1] R_RATIO 통일 확인
# ─────────────────────────────────────────────────────────────────────────────
class TestRRatio:
    def test_autostock_config_r_ratio_is_0005(self):
        from autostock import config
        assert config.R_RATIO == 0.005

    def test_marketflow_config_r_ratio_is_0005(self):
        from marketflow.engine.config import SignalConfig
        cfg = SignalConfig()
        assert cfg.r_ratio == 0.005

    def test_executor_uses_config_r_ratio_not_hardcoded(self):
        """executor.py에 _R_RATIO = 0.01 하드코딩이 없어야 함."""
        import inspect
        from autostock.trading import executor
        source = inspect.getsource(executor)
        assert "_R_RATIO = 0.01" not in source, "executor.py에 0.01 하드코딩이 남아 있음"

    def test_calc_order_qty_uses_atr_and_r_ratio(self):
        """calc_order_qty가 R_RATIO(0.5%)와 ATR 손절폭으로 수량 계산 (LLM 신뢰도 미반영)."""
        from unittest.mock import patch
        from autostock import config
        from autostock.trading import executor
        from autostock.trading.executor import calc_order_qty, SINGLE_POSITION_CAP_PCT
        from autostock.models import FinalDecision

        decision = FinalDecision(
            ticker="005930", action="BUY", confidence=9.0, final_reason="테스트",
            price_reference=50000.0, stop_loss_price=46000.0, bull_summary="", bear_summary="",
        )
        available_cash = 10_000_000.0

        with patch.object(executor, "compute_stop_pct", return_value=4.0):
            qty = calc_order_qty(decision, available_cash)

        risk_amount = available_cash * config.R_RATIO  # 신뢰도 배수 없음
        position_size = min(risk_amount / 0.04, available_cash * SINGLE_POSITION_CAP_PCT)
        expected_qty = int(position_size / 50000)
        assert qty == expected_qty, f"예상 {expected_qty}, 실제 {qty}"

    def test_calc_order_qty_ignores_llm_confidence(self):
        """confidence가 달라도 수량이 동일 — 사이징이 LLM에서 분리됨."""
        from unittest.mock import patch
        from autostock.trading import executor
        from autostock.trading.executor import calc_order_qty
        from autostock.models import FinalDecision

        def _mk(conf):
            return FinalDecision(
                ticker="005930", action="BUY", confidence=conf, final_reason="t",
                price_reference=50000.0, stop_loss_price=46000.0, bull_summary="", bear_summary="",
            )
        with patch.object(executor, "compute_stop_pct", return_value=4.0):
            q_hi = calc_order_qty(_mk(9.0), 10_000_000.0)
            q_lo = calc_order_qty(_mk(5.0), 10_000_000.0)
        assert q_hi == q_lo

    def test_calc_order_qty_fallback_uses_config_stop_loss(self):
        """compute_stop_pct 실패(0) 시 config.STOP_LOSS_PCT 폴백."""
        from unittest.mock import patch
        from autostock import config
        from autostock.trading import executor
        from autostock.trading.executor import calc_order_qty, SINGLE_POSITION_CAP_PCT
        from autostock.models import FinalDecision

        decision = FinalDecision(
            ticker="005930", action="BUY", confidence=7.0, final_reason="테스트",
            price_reference=50000.0, stop_loss_price=0.0, bull_summary="", bear_summary="",
        )
        available_cash = 10_000_000.0
        with patch.object(executor, "compute_stop_pct", return_value=0.0):
            qty = calc_order_qty(decision, available_cash)

        risk_amount = available_cash * config.R_RATIO
        position_size = min(risk_amount / config.STOP_LOSS_PCT, available_cash * SINGLE_POSITION_CAP_PCT)
        expected_qty = int(position_size / 50000)
        assert qty == expected_qty


# ─────────────────────────────────────────────────────────────────────────────
# [P1-2] RVOL 함수 분리 확인
# ─────────────────────────────────────────────────────────────────────────────
class TestRvolIndicator:
    def _make_df(self, today_vol: int, avg_vol: int = 100) -> pd.DataFrame:
        data = {
            "volume": [avg_vol] * 20 + [today_vol],
            "close": [100] * 21,
            "open": [99] * 21,
            "high": [101] * 21,
            "low": [98] * 21,
        }
        return pd.DataFrame(data)

    def test_rvol_ratio_exists(self):
        from indicators import rvol_ratio
        assert callable(rvol_ratio)

    def test_rvol_high_volume_returns_high_ratio(self):
        from indicators import rvol_ratio
        df = self._make_df(today_vol=300, avg_vol=100)
        assert rvol_ratio(df) == pytest.approx(3.0, rel=0.01)

    def test_rvol_low_volume_returns_low_ratio(self):
        from indicators import rvol_ratio
        df = self._make_df(today_vol=50, avg_vol=100)
        assert rvol_ratio(df) < 1.0

    def test_vdu_and_rvol_are_separate_functions(self):
        """vdu_ratio와 rvol_ratio가 별도 함수로 존재해야 함."""
        import indicators
        assert hasattr(indicators, "vdu_ratio")
        assert hasattr(indicators, "rvol_ratio")
        assert indicators.vdu_ratio is not indicators.rvol_ratio

    def test_scorer_rvol_uses_rvol_ratio_not_vdu(self):
        """scorer.py의 _score_rvol이 _rvol_ratio를 사용하는지 소스 검증."""
        import inspect
        import scorer
        source = inspect.getsource(scorer.Scorer._score_rvol)
        assert "_rvol_ratio" in source, "_score_rvol이 _rvol_ratio를 사용해야 함"
        # _vdu_ratio 직접 호출이 없어야 함 (import alias는 괜찮음)
        assert "= _vdu_ratio(" not in source, "_score_rvol 내부에 _vdu_ratio 직접 호출이 남아 있음"


# ─────────────────────────────────────────────────────────────────────────────
# [P1-1] MA 중장기 정배열 점수 확인
# ─────────────────────────────────────────────────────────────────────────────
class TestMaAlignedLong:
    def _make_charts(self, n: int, close_fn=None):
        from models import ChartData
        charts = []
        for i in range(n):
            price = close_fn(i) if close_fn else 100 + i * 0.1
            charts.append(ChartData(
                code="000000",
                date=date(2025, 1, 1),
                open=int(price * 0.99),
                high=int(price * 1.01),
                low=int(price * 0.98),
                close=int(price),
                volume=100_000,
                ma5=price,
                ma10=price * 0.99,
                ma20=price * 0.98,
            ))
        return charts

    def test_ma_aligned_long_field_exists_in_checklist(self):
        from models import ChecklistDetail
        c = ChecklistDetail()
        assert hasattr(c, "ma_aligned_long")
        assert c.ma_aligned_long is False

    def test_ma_aligned_long_in_to_dict(self):
        from models import ChecklistDetail
        c = ChecklistDetail(ma_aligned_long=True)
        d = c.to_dict()
        assert d["optional"]["ma_aligned_long"] is True

    def test_score_chart_gives_extra_point_for_long_ma_alignment(self):
        """130일 데이터에서 MA20>MA60>MA120 정배열이면 추가 1점."""
        from scorer import Scorer
        from models import StockData

        scorer = Scorer()

        # 우상향 차트 (120일 이상 — 중장기 정배열 조건 충족)
        charts = self._make_charts(130, close_fn=lambda i: 100 + i * 0.5)

        stock = StockData(
            code="000000", name="테스트", market="KOSPI",
            open=163, high=165, low=161, close=164,
            volume=500_000, trading_value=80_000_000_000,
            market_cap=500_000_000_000,
            change_pct=6.0, high_52w=170, low_52w=100,
        )

        # _score_chart는 (pts, flags_dict)를 반환
        pts, flags = scorer._score_chart(stock, charts)
        assert flags["ma_aligned_long"] is True, "우상향 130일 차트에서 중장기 정배열 감지 실패"

    def test_score_chart_no_long_alignment_when_insufficient_data(self):
        """60일 데이터에서는 MA120 계산 불가 — ma_aligned_long=False."""
        from scorer import Scorer
        from models import StockData

        scorer = Scorer()
        charts = self._make_charts(60, close_fn=lambda i: 100 + i * 0.5)

        stock = StockData(
            code="000000", name="테스트", market="KOSPI",
            open=128, high=130, low=127, close=129,
            volume=500_000, trading_value=80_000_000_000,
            market_cap=500_000_000_000,
            change_pct=6.0, high_52w=130, low_52w=100,
        )

        # _score_chart는 (pts, flags_dict)를 반환
        _, flags = scorer._score_chart(stock, charts)
        assert flags["ma_aligned_long"] is False


# ─────────────────────────────────────────────────────────────────────────────
# [P2-1] 폰트 OS 분기 확인
# ─────────────────────────────────────────────────────────────────────────────
class TestFontOsBranch:
    def test_windows_font_is_malgun(self):
        """Windows에서 폰트가 Malgun Gothic으로 설정되어야 함."""
        import platform
        if platform.system() == "Windows":
            import collectors  # noqa: F401 — collectors import 시 matplotlib rcParams 설정됨
            import matplotlib
            family = matplotlib.rcParams["font.family"]
            # list 또는 string 양쪽 모두 허용
            if isinstance(family, list):
                assert "Malgun Gothic" in family, f"font.family에 Malgun Gothic 없음: {family}"
            else:
                assert family == "Malgun Gothic", f"font.family가 Malgun Gothic이 아님: {family}"

    def test_collectors_imports_platform(self):
        """collectors.py가 platform 모듈을 import하는지 소스 확인."""
        import inspect
        import collectors
        source = inspect.getsource(collectors)
        assert "import platform" in source


# ─────────────────────────────────────────────────────────────────────────────
# [P2-2] 52주 고가/저가 보호 확인
# ─────────────────────────────────────────────────────────────────────────────
class TestHigh52wProtection:
    def test_high_52w_not_overwritten_when_nonzero(self):
        """generator.py: high_52w가 0이 아니면 차트 데이터로 덮어쓰지 않음."""
        import inspect
        import sys, os
        gen_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "marketflow", "engine", "generator.py"
        )
        source = open(gen_path, encoding="utf-8").read()
        assert "if stock.high_52w == 0:" in source, "52주 고가 0 여부 체크 누락"
        assert "if stock.low_52w == 0:" in source, "52주 저가 0 여부 체크 누락"
