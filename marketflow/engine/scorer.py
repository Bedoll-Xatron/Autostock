from typing import List, Optional, Tuple

import pandas as pd

from config import Grade, SignalConfig, VCPConfig
from indicators import vdu_ratio as _vdu_ratio, rvol_ratio as _rvol_ratio
from models import (
    ChecklistDetail,
    ChartData,
    NewsData,
    ScoreDetail,
    StockData,
    SupplyData,
)
from vcp_detector import detect_vcp


class Scorer:
    def __init__(self, config: SignalConfig = None):
        self.config = config or SignalConfig()

    def calculate(
        self,
        stock: StockData,
        charts: List[ChartData],
        news_list: List[NewsData],
        supply: Optional[SupplyData],
        llm_result: Optional[dict] = None,
        kospi_return_20d: Optional[float] = None,
        short_balance_pct: float = 0.0,
    ) -> Tuple[ScoreDetail, ChecklistDetail]:
        score = ScoreDetail()
        checklist = ChecklistDetail()

        # 1. 뉴스/재료 점수 (0~3)
        score.news, news_flags = self._score_news(news_list, llm_result)
        checklist.has_news = news_flags["has_news"]
        checklist.news_sources = news_flags["sources"]
        score.llm_reason = news_flags["reason"]

        # 2. 거래대금 점수 (0~3)
        score.volume, checklist.volume_sufficient = self._score_volume(stock)

        # 3. 차트패턴 점수 (0~4)
        score.chart, chart_flags = self._score_chart(stock, charts)
        checklist.is_new_high = chart_flags["new_high"]
        checklist.is_breakout = chart_flags["breakout"]
        checklist.ma_aligned = chart_flags["ma_aligned"]
        checklist.ma_aligned_long = chart_flags["ma_aligned_long"]

        # 4. 캔들형태 점수 (0~1)
        score.candle, candle_flags = self._score_candle(stock, charts)
        checklist.good_candle = candle_flags["good"]
        checklist.upper_wick_long = candle_flags["upper_wick_long"]

        # 5. 기간조정 점수 (0~1)
        score.consolidation, checklist.has_consolidation = \
            self._score_consolidation(charts)

        # 6. 수급 점수 (0~2)
        score.supply, checklist.supply_positive = self._score_supply(supply)

        # 7. 조정폭 회복 점수 (0~1)
        score.retracement, checklist.retracement_recovery = \
            self._score_retracement_recovery(charts)

        # 8. 되돌림 지지 점수 (0~1)
        score.pullback_support, checklist.pullback_support_confirmed = \
            self._score_pullback_support(charts)

        # 9. 거래량 고갈 점수 (0~1)
        score.vdu_score = self._score_vdu(charts)

        # 10. 상대강도 점수 (0~2)
        score.rs_score = self._score_rs(charts, kospi_return_20d)

        # 11. VCP 패턴 점수 (0~2)
        score.vcp_score = self._score_vcp(charts)

        # 12. 공매도 잔고 역이용 점수 (0~2)
        score.short_score = self._score_short_squeeze(short_balance_pct, score.supply)

        # 13. 상대거래량 조기감지 (RVOL) 점수 (0~2)
        score.rvol_score = self._score_rvol(charts)

        return score, checklist

    def _score_news(
        self, news_list: List[NewsData], llm_result: Optional[dict]
    ) -> Tuple[int, dict]:
        flags = {"has_news": False, "sources": [], "reason": ""}

        # LLM 분석 결과가 있으면 우선 사용
        if llm_result and isinstance(llm_result.get("score"), int):
            pts = max(0, min(3, llm_result["score"]))
            flags["reason"] = llm_result.get("reason", "")
            flags["sources"] = llm_result.get("themes", [])
            if pts >= 1:
                flags["has_news"] = True
            return pts, flags

        # LLM 없으면 뉴스 존재 여부로 최소 판단
        if news_list:
            flags["has_news"] = True
            flags["sources"] = [n.title for n in news_list[:3]]
            flags["reason"] = news_list[0].title
            return 1, flags

        return 0, flags

    def _score_volume(self, stock: StockData) -> Tuple[int, bool]:
        tv = stock.trading_value
        if tv >= 1_000_000_000_000:
            pts = 3
        elif tv >= 500_000_000_000:
            pts = 2
        elif tv >= 100_000_000_000:
            pts = 1
        else:
            pts = 0
        sufficient = tv >= 50_000_000_000
        return pts, sufficient

    def _score_chart(
        self, stock: StockData, charts: List[ChartData]
    ) -> Tuple[int, dict]:
        flags = {"new_high": False, "breakout": False, "ma_aligned": False, "ma_aligned_long": False}

        if len(charts) < 20:
            return 0, flags

        pts = 0
        last = charts[-1]

        # 1a) 단기 정배열 (MA5 > MA10 > MA20)
        if last.ma5 is not None and last.ma10 is not None and last.ma20 is not None:
            if stock.close > last.ma5 > last.ma10 > last.ma20:
                flags["ma_aligned"] = True
                pts += 1

        # 1b) 중장기 정배열 (MA20 > MA60 > MA120) — 백테스트 슈퍼 젬 조건
        if len(charts) >= 120:
            ma20 = sum(c.close for c in charts[-20:]) / 20
            ma60 = sum(c.close for c in charts[-60:]) / 60
            ma120 = sum(c.close for c in charts[-120:]) / 120
            if ma20 > ma60 > ma120:
                flags["ma_aligned_long"] = True
                pts += 1

        # 2) 52주 신고가 근접 또는 60일 고가 돌파
        if stock.high_52w > 0 and stock.close >= stock.high_52w * 0.95:
            flags["new_high"] = True
            pts += 1
        elif len(charts) >= 60:
            high_60d = max(c.high for c in charts[-60:])
            if stock.close > high_60d:
                flags["breakout"] = True
                pts += 1

        # 3) 중기 추세: close > MA60 (60일 이평선 위 = 중기 상승 추세)
        if len(charts) >= 60:
            ma60 = sum(c.close for c in charts[-60:]) / 60
            if stock.close > ma60:
                pts += 1

        return pts, flags

    def _score_candle(
        self, stock: StockData, charts: List[ChartData]
    ) -> Tuple[int, dict]:
        o, h, l, c = stock.open, stock.high, stock.low, stock.close
        flags = {"good": False, "upper_wick_long": False, "body_ratio": 0.0}

        if o == 0 or h == l:
            return 0, flags
        if c <= o:
            return 0, flags

        body = c - o
        total_range = h - l
        body_ratio = body / total_range
        upper_wick = h - c
        upper_wick_ratio = upper_wick / body if body > 0 else 999

        flags["body_ratio"] = round(body_ratio, 4)

        if upper_wick_ratio > 0.5:
            flags["upper_wick_long"] = True

        if (body_ratio >= 0.6 and upper_wick_ratio <= 0.3) or \
           (body_ratio >= 0.5 and upper_wick_ratio <= 0.5):
            flags["good"] = True
            return 1, flags

        return 0, flags

    def _score_supply(self, supply: Optional[SupplyData]) -> Tuple[int, bool]:
        if supply is None:
            return 0, False

        f = supply.foreign_net_5d
        i = supply.inst_net_5d

        if f > 0 and i > 0:
            pts = 2
        elif f > 0 or i > 0:
            pts = 1
        else:
            pts = 0

        return pts, pts >= 1

    def _score_retracement_recovery(
        self, charts: List[ChartData]
    ) -> Tuple[int, bool]:
        if len(charts) < 10:
            return 0, False

        recent = charts[-10:]
        high_idx = max(range(len(recent)), key=lambda i: recent[i].high)

        # 고점 이후 최소 2일은 지나야 함
        if high_idx >= len(recent) - 2:
            return 0, False

        high_val = recent[high_idx].high
        after_high = recent[high_idx + 1:]
        low_after = min(c.low for c in after_high)

        decline = high_val - low_after
        if high_val <= 0 or decline <= 0 or decline / high_val < 0.03:
            return 0, False

        recovery = recent[-1].close - low_after
        if recovery >= decline * 0.5:
            return 1, True

        return 0, False

    def _score_pullback_support(
        self, charts: List[ChartData]
    ) -> Tuple[int, bool]:
        if len(charts) < 25:
            return 0, False

        past_resistance = max(c.high for c in charts[-25:-5])
        recent_5 = charts[-5:]

        # 최근 5일 중 오늘 제외, 종가가 저항선을 넘은 날이 있는지
        breakout = any(c.close > past_resistance for c in recent_5[:-1])
        if not breakout:
            return 0, False

        today = charts[-1]
        if today.low <= past_resistance * 1.02 and today.close > past_resistance:
            return 1, True

        return 0, False

    def _score_consolidation(
        self, charts: List[ChartData]
    ) -> Tuple[int, bool]:
        if len(charts) < 20:
            return 0, False

        recent_20 = charts[-20:]
        recent_5 = charts[-5:]

        high_20 = max(c.high for c in recent_20)
        low_20 = min(c.low for c in recent_20)
        high_5 = max(c.high for c in recent_5)
        low_5 = min(c.low for c in recent_5)

        if low_20 <= 0 or low_5 <= 0:
            return 0, False

        range_20 = (high_20 - low_20) / low_20
        range_5 = (high_5 - low_5) / low_5

        volatility_contracted = range_5 < range_20 * 0.5
        sideways = range_20 <= 0.15
        breakout = charts[-1].close > high_20

        if (sideways or volatility_contracted) and breakout:
            return 1, True

        return 0, False

    def _score_vdu(self, charts: List[ChartData]) -> int:
        if len(charts) < 21:
            return 0
        df = pd.DataFrame([{
            "close": c.close, "high": c.high, "low": c.low,
            "open": c.open, "volume": c.volume,
        } for c in charts])
        ratio = _vdu_ratio(df, avg_period=20)
        return 1 if ratio < 0.6 else 0

    def _score_rs(self, charts: List[ChartData], kospi_return_20d: Optional[float]) -> int:
        if len(charts) < 20 or kospi_return_20d is None:
            return 0
        price_now = charts[-1].close
        price_20ago = charts[-20].close
        if price_20ago <= 0:
            return 0
        stock_return = (price_now - price_20ago) / price_20ago * 100
        excess = stock_return - kospi_return_20d
        if excess >= 20:
            return 2
        if excess >= 10:
            return 1
        return 0

    def _score_vcp(self, charts: List[ChartData]) -> int:
        if len(charts) < 20:
            return 0
        df = pd.DataFrame([{
            "close": c.close, "high": c.high, "low": c.low,
            "open": c.open, "volume": c.volume,
        } for c in charts])
        try:
            result = detect_vcp(df, VCPConfig())
        except Exception:
            return 0
        if not result.detected:
            return 0
        if result.grade in ("A", "B"):
            return 2
        return 1

    def _score_rvol(self, charts: List[ChartData]) -> int:
        """상대거래량(RVOL) 조기 급등 감지. 오늘 거래량 / 20일 평균 거래량.

        상승 캔들 + RVOL 급등 = 기관/외국인 매집 조기 포착 신호.
        2점: RVOL >= 2.5 (강한 폭발)
        1점: RVOL >= 1.5 (의미있는 증가)
        0점: 이외 또는 하락 캔들
        """
        if len(charts) < 22:
            return 0
        df = pd.DataFrame([{
            "close": c.close, "high": c.high, "low": c.low,
            "open": c.open, "volume": c.volume,
        } for c in charts])
        # 상승 캔들(종가 > 시가)만 유효
        if df["close"].iloc[-1] <= df["open"].iloc[-1]:
            return 0
        ratio = _rvol_ratio(df, avg_period=20)
        if ratio >= 2.5:
            return 2
        if ratio >= 1.5:
            return 1
        return 0

    def _score_short_squeeze(self, short_balance_pct: float, supply_score: int) -> int:
        """
        공매도 잔고 비율 + 수급으로 숏 스퀴즈 가능성 평가.

        2점: 잔고비율 ≥ 5% AND supply_score ≥ 2 (외국인+기관 동시 매수 시 압박 극대)
        1점: 잔고비율 ≥ 2% AND supply_score ≥ 1
        0점: 이외
        """
        if short_balance_pct >= 5.0 and supply_score >= 2:
            return 2
        if short_balance_pct >= 2.0 and supply_score >= 1:
            return 1
        return 0

    def determine_grade(self, stock: StockData, score: ScoreDetail) -> Grade:
        if not score.mandatory_passed:
            if score.vcp_score > 0:
                return Grade.W
            return Grade.C
        if score.total >= 11:
            return Grade.A
        if score.total >= 9:
            return Grade.B
        if score.vcp_score > 0:
            return Grade.W
        return Grade.C

    def calculate_quality(
        self, stock: StockData, charts: List[ChartData], score: ScoreDetail
    ) -> float:
        q = 0.0

        # 1. 수급 (최대 30점)
        if score.supply >= 2:
            q += 30
        elif score.supply == 1:
            q += 15

        # 2. 총점 (최대 25점)
        if score.total >= 10:
            q += 25
        elif score.total >= 9:
            q += 20
        elif score.total >= 8:
            q += 15
        elif score.total >= 7:
            q += 10

        # 3. 당일 상승률 (최대 20점)
        chg = abs(stock.change_pct)
        if chg <= 5:
            q += 20
        elif chg <= 10:
            q += 15
        elif chg <= 15:
            q += 10
        elif chg <= 20:
            q += 5

        # 4. 20일 모멘텀 (최대 15점)
        if len(charts) >= 20:
            price_20ago = charts[-20].close
            if price_20ago > 0:
                m20 = (stock.close - price_20ago) / price_20ago * 100
                if m20 <= 20:
                    q += 15
                elif m20 <= 40:
                    q += 10
                elif m20 <= 60:
                    q += 5

        # 5. 거래량 비율 (최대 10점)
        if len(charts) >= 20:
            vol_20avg = sum(c.volume for c in charts[-20:]) / 20
            if vol_20avg > 0:
                vol_ratio = stock.volume / vol_20avg
                if 4 <= vol_ratio <= 6:
                    q += 10
                elif 2 <= vol_ratio <= 8:
                    q += 5

        return round(q, 1)
