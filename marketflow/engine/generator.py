"""시그널 생성기 — 수집 → LLM 분석 → 스코어링 → 포지션 사이징 → Signal 생성"""

import asyncio
import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import List, Optional

from config import SignalConfig
from collectors import (
    get_top_gainers, get_volume_leaders, get_moderate_movers,
    get_52w_near_high, get_supply_leaders,
    get_kospi_return_20d, get_chart_data, get_supply_data, get_stock_news,
)
from llm_analyzer import GeminiAnalyzer
from models import Grade, Signal, StockData
from position_sizer import PositionSizer
from scorer import Scorer

WATCHLIST_PATH = Path(__file__).parent / "watchlist.json"

log = logging.getLogger(__name__)


def _load_watchlist() -> List[StockData]:
    """전날 W등급으로 저장된 종목을 로드한다. close=0 플레이스홀더로 반환."""
    if not WATCHLIST_PATH.exists():
        return []
    try:
        items = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
        return [
            StockData(
                code=item["code"], name=item["name"], market=item["market"],
                open=0, high=0, low=0, close=0,
                volume=0, trading_value=0, market_cap=0,
                change_pct=0.0, high_52w=0, low_52w=0,
            )
            for item in items
        ]
    except Exception as e:
        log.warning("워치리스트 로드 실패: %s", e)
        return []


def _save_watchlist(signals: List[Signal]) -> None:
    """W등급 시그널을 watchlist.json에 저장한다."""
    items = [
        {"code": s.stock_code, "name": s.stock_name, "market": s.market}
        for s in signals if s.grade == Grade.W
    ]
    try:
        WATCHLIST_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("워치리스트 저장: %d개 → %s", len(items), WATCHLIST_PATH)
    except Exception as e:
        log.warning("워치리스트 저장 실패: %s", e)


class SignalGenerator:
    """매매 시그널 생성기"""

    def __init__(self, config: SignalConfig = None, capital: int = 10_000_000):
        self.config = config or SignalConfig()
        self.capital = capital
        self.scorer = Scorer(self.config)
        self.position_sizer = PositionSizer(capital, self.config)
        self.llm_analyzer = GeminiAnalyzer()

    async def generate(self, top_n: int = 15) -> List[Signal]:
        """메인 파이프라인: 수집 → 분석 → 필터링 → 정렬"""
        today = date.today()

        log.info("=== 시그널 생성 시작 | %s ===", today)

        # 1. KOSPI 20일 수익률 (RS 계산 기준선)
        log.info("[수집] KOSPI 지수 20일 수익률...")
        kospi_return_20d = get_kospi_return_20d()
        if kospi_return_20d is not None:
            log.info("  KOSPI 20d 수익률: %.2f%%", kospi_return_20d)
        else:
            log.warning("  KOSPI 20d 수익률 수집 실패 → RS 점수 비활성")

        # 2. 상승 종목 수집
        log.info("[수집] KOSPI 상승 종목...")
        kospi = get_top_gainers("KOSPI", self.config)
        log.info("[수집] KOSDAQ 상승 종목...")
        kosdaq = get_top_gainers("KOSDAQ", self.config)

        kospi = sorted(kospi, key=lambda x: x.change_pct, reverse=True)[:top_n]
        kosdaq = sorted(kosdaq, key=lambda x: x.change_pct, reverse=True)[:top_n]

        # 3. 거래대금 상위 종목 추가 (VCP 사전 신호 포착)
        log.info("[수집] KOSPI 거래대금 상위...")
        vol_kospi = get_volume_leaders("KOSPI", self.config, top_n=50)
        log.info("[수집] KOSDAQ 거래대금 상위...")
        vol_kosdaq = get_volume_leaders("KOSDAQ", self.config, top_n=50)

        # 4. 완만 상승 종목 추가 (2~5%, 초기 모멘텀)
        log.info("[수집] KOSPI 완만상승(2~5%)...")
        mod_kospi = get_moderate_movers("KOSPI", self.config, top_n=50)
        log.info("[수집] KOSDAQ 완만상승(2~5%)...")
        mod_kosdaq = get_moderate_movers("KOSDAQ", self.config, top_n=50)

        # 5. 52주 신고가 종목 추가 (신고가 돌파 시도)
        log.info("[수집] KOSPI 52주 신고가...")
        high_kospi = get_52w_near_high("KOSPI", self.config, top_n=30)
        log.info("[수집] KOSDAQ 52주 신고가...")
        high_kosdaq = get_52w_near_high("KOSDAQ", self.config, top_n=30)

        # 6. 외국인/기관 순매수 상위 (수급 강도 높은 종목)
        log.info("[수집] KOSPI 수급순매수...")
        supply_kospi = get_supply_leaders("KOSPI", self.config, top_n=30)
        log.info("[수집] KOSDAQ 수급순매수...")
        supply_kosdaq = get_supply_leaders("KOSDAQ", self.config, top_n=30)

        # 7. 전날 W등급 워치리스트 재분석
        log.info("[수집] W등급 워치리스트 로드...")
        watchlist_stocks = _load_watchlist()
        log.info("  워치리스트: %d개", len(watchlist_stocks))

        # 8. 유니버스 합치기 (코드 중복 제거, 실제 데이터 우선)
        all_stocks_dict: dict = {}
        for s in (kospi + kosdaq + vol_kospi + vol_kosdaq + mod_kospi + mod_kosdaq
                  + high_kospi + high_kosdaq + supply_kospi + supply_kosdaq):
            all_stocks_dict[s.code] = s
        # 워치리스트는 실제 데이터 없을 때만 추가 (close=0 플레이스홀더)
        for s in watchlist_stocks:
            if s.code not in all_stocks_dict:
                all_stocks_dict[s.code] = s

        all_stocks = list(all_stocks_dict.values())
        total = len(all_stocks)

        log.info(
            "  총 분석 대상: %d개 (상승 %d, 거래대금 %d, 완만 %d, 신고가 %d, 수급 %d, 워치리스트 %d)",
            total,
            len(kospi) + len(kosdaq),
            len(vol_kospi) + len(vol_kosdaq),
            len(mod_kospi) + len(mod_kosdaq),
            len(high_kospi) + len(high_kosdaq),
            len(supply_kospi) + len(supply_kosdaq),
            len(watchlist_stocks),
        )

        # 9. 각 종목에 대해 _analyze_stock() 병렬 호출 (최대 10종목 동시)
        _sem = asyncio.Semaphore(10)

        async def _analyze_with_sem(stock: StockData, idx: int) -> Optional[Signal]:
            async with _sem:
                log.info("[%d/%d] %s(%s) 분석 중...", idx, total, stock.name, stock.code)
                return await self._analyze_stock(stock, today, kospi_return_20d)

        tasks = [_analyze_with_sem(s, i) for i, s in enumerate(all_stocks, 1)]
        results = await asyncio.gather(*tasks)
        signals: List[Signal] = [r for r in results if r is not None]

        # 10. C등급 제외 (이미 _analyze_stock에서 None 반환)
        # 11. 등급순 정렬 (A > B > W), 동일 등급 내 총점 내림차순
        grade_order = {Grade.A: 0, Grade.B: 1, Grade.W: 2}
        signals.sort(key=lambda s: (grade_order.get(s.grade, 99), -s.score.total))

        # 12. W등급 워치리스트 저장 (다음 실행 때 재분석)
        _save_watchlist(signals)

        # 13. 결과 요약
        a_cnt = sum(1 for s in signals if s.grade == Grade.A)
        b_cnt = sum(1 for s in signals if s.grade == Grade.B)
        w_cnt = sum(1 for s in signals if s.grade == Grade.W)

        log.info("=== 시그널 생성 완료: %d개 (A: %d, B: %d, W: %d) ===", len(signals), a_cnt, b_cnt, w_cnt)
        for s in signals:
            log.info("  [%s] %-12s %2d/20  품질 %5.1f  %d주 %12,d원",
                     s.grade.value, s.stock_name, s.score.total,
                     s.quality, s.quantity, s.position_size)

        return signals

    async def _analyze_stock(
        self, stock: StockData, target_date: date, kospi_return_20d: Optional[float] = None
    ) -> Optional[Signal]:
        """개별 종목 분석 → Signal 생성"""
        name = stock.name
        code = stock.code

        try:
            # 1. 차트 데이터 170일 수집 (6개월 모멘텀 126+스킵21=147 + MA120 여유분)
            log.debug("  차트 수집...")
            charts = get_chart_data(code, days=170)

            # StockData OHLC/52주 보완 (API 원본에 없는 필드, 워치리스트 플레이스홀더 포함)
            if charts:
                latest = charts[-1]
                if stock.close == 0:
                    stock.close = latest.close
                if stock.open == 0:
                    stock.open = latest.open
                if stock.high == 0:
                    stock.high = latest.high
                if stock.low == 0:
                    stock.low = latest.low
                if stock.high_52w == 0:
                    stock.high_52w = max(c.high for c in charts)
                if stock.low_52w == 0:
                    stock.low_52w = min(c.low for c in charts)

            # 2. 뉴스 3건 수집
            log.debug("  뉴스 수집...")
            news_list = get_stock_news(code, name, limit=3)
            news_items = [{"title": n.title, "summary": n.summary} for n in news_list]

            # 3. LLM 뉴스 분석
            log.debug("  LLM 분석...")
            llm_result = await self.llm_analyzer.analyze_news(name, news_items)
            llm_score = llm_result.get("score", 0)
            llm_source = llm_result.get("source", "?")
            log.debug("  → LLM: %d/3 (%s)", llm_score, llm_source)

            # 4. 수급 데이터 수집
            log.debug("  수급 수집...")
            supply = get_supply_data(code)

            # 5. scorer.calculate() → 점수 계산
            score, checklist = self.scorer.calculate(
                stock, charts, news_list, supply, llm_result, kospi_return_20d
            )
            log.debug("  점수: %d/20", score.total)

            # 6. scorer.determine_grade() → 등급 결정
            grade_raw = self.scorer.determine_grade(stock, score)
            grade = Grade(grade_raw.value)
            log.debug("  등급: %s", grade.value)

            # 7. C등급이면 None 반환; W등급은 게이트 생략
            if grade == Grade.C:
                log.debug("  → C등급 제외")
                return None

            if grade == Grade.W:
                quality = 0.0
                log.debug("  → W등급(VCP 워치리스트) 포지션 없음")
            else:
                # 8. 품질 게이트 3중 필터 (A/B 등급별 차등 적용)
                if grade == Grade.A:
                    supply_min = self.config.min_supply_score
                    total_min = self.config.min_total_score
                    quality_min = self.config.min_quality
                else:  # Grade.B
                    supply_min = self.config.min_supply_score_b
                    total_min = self.config.min_total_score_b
                    quality_min = self.config.min_quality_b

                # Gate 1: 수급 점수
                if score.supply < supply_min:
                    log.info("  → 수급부족 %s: supply=%d (필요: %d, %s등급)",
                             name, score.supply, supply_min, grade.value)
                    return None

                # Gate 2: 총점
                if score.total < total_min:
                    log.info("  → 점수부족 %s: total=%d (필요: %d, %s등급)",
                             name, score.total, total_min, grade.value)
                    return None

                # Gate 3: 품질 점수
                quality = self.scorer.calculate_quality(stock, charts, score)
                log.debug("  품질: %s", quality)

                if quality < quality_min:
                    log.info("  → 품질부족 %s: quality=%s (필요: %.0f, %s등급)",
                             name, quality, quality_min, grade.value)
                    return None

            # 9. position_sizer.calculate() → 포지션 계산 (W등급은 0)
            if grade == Grade.W:
                entry_price = stock.close
                stop_price = int(stock.close * (1 - self.config.stop_loss_pct))
                target_price = int(stock.close * (1 + self.config.take_profit_pct))
                r_value = 0
                position_size = 0
                quantity = 0
                r_multiplier = 0.0
            else:
                pos = self.position_sizer.calculate(stock.close, grade)
                log.debug("  포지션: %d주 / %,.0f원 (자본대비 %s%%)",
                          pos.quantity, pos.position_size, pos.position_pct)
                entry_price = pos.entry_price
                stop_price = pos.stop_price
                target_price = pos.target_price
                r_value = pos.r_value
                position_size = int(pos.position_size)
                quantity = pos.quantity
                r_multiplier = pos.r_multiplier

            # 10. Signal 객체 생성 및 반환
            return Signal(
                stock_code=code,
                stock_name=name,
                market=stock.market,
                signal_date=target_date,
                grade=grade,
                score=score,
                checklist=checklist,
                current_price=stock.close,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                r_value=r_value,
                position_size=position_size,
                quantity=quantity,
                r_multiplier=r_multiplier,
                trading_value=stock.trading_value,
                change_pct=stock.change_pct,
                foreign_5d=supply.foreign_net_5d,
                inst_5d=supply.inst_net_5d,
                quality=quality,
                news_items=news_items,
                themes=llm_result.get("themes", []),
            )

        except Exception as e:
            log.error("  [에러] %s(%s): %s", name, code, e)
            return None
