"""종가베팅 V2 엔진 실행 스크립트"""

import argparse
import asyncio
import logging
import time
from collections import Counter
from datetime import date

from config import SignalConfig

log = logging.getLogger(__name__)
from generator import SignalGenerator
from models import Grade, ScreenerResult
from notifier import notify_signal_results
from persistence import save_result_to_json


def fmt_value(tv):
    if tv >= 1_000_000_000_000:
        return f"{tv / 1_000_000_000_000:.1f}조"
    return f"{tv / 100_000_000:,.0f}억"


def print_result(result: ScreenerResult):
    """스크리너 결과를 터미널에 예쁘게 출력한다."""
    today = result.date.isoformat()

    print()
    print("=" * 70)
    print(f"  종가베팅 V2 스크리닝 결과 - {today}")
    print("=" * 70)

    # 요약
    a_cnt = result.by_grade.get("A", 0)
    b_cnt = result.by_grade.get("B", 0)
    elapsed = result.processing_time_ms / 1000

    print(f"\n  후보: {result.total_candidates}개 → 시그널: {result.filtered_count}개"
          f"  (A: {a_cnt} / B: {b_cnt})  |  {elapsed:.1f}초")

    if not result.signals:
        print(f"\n  시그널 없음 - 조건을 충족하는 종목이 없습니다.")
        print("=" * 70)
        return

    # 시그널 테이블
    print(f"\n  {'#':>2}  {'등급':>2}  {'종목명':<12} {'코드':<8} "
          f"{'총점':>5} {'품질':>5}  {'진입가':>8} {'손절가':>8} {'목표가':>8}  "
          f"{'수량':>5} {'포지션':>10}")
    print("-" * 70)

    for i, s in enumerate(result.signals, 1):
        print(f"  {i:>2}   {s.grade.value}   {s.stock_name:<12} {s.stock_code:<8} "
              f"{s.score.total:>3}pt {s.quality:>5.1f}  "
              f"{s.entry_price:>8,} {s.stop_price:>8,} {s.target_price:>8,}  "
              f"{s.quantity:>5} {s.position_size:>10,}")

    # 종목별 상세
    for i, s in enumerate(result.signals, 1):
        print(f"\n{'─' * 70}")
        print(f"  {i}. {s.stock_name} ({s.stock_code})  |  "
              f"{s.market}  |  {s.change_pct:+.1f}%  |  등급 {s.grade.value}")
        print(f"{'─' * 70}")

        # 점수
        sc = s.score
        print(f"  [점수] {sc.total}pt  -  "
              f"뉴스 {sc.news}/3  거래대금 {sc.volume}/3  차트 {sc.chart}/3  "
              f"캔들 {sc.candle}/1  조정 {sc.consolidation}/1  "
              f"수급 {sc.supply}/2  회복 {sc.retracement}/1  지지 {sc.pullback_support}/1  "
              f"RVOL {sc.rvol_score}/2")

        # 가격
        print(f"  [가격] 현재 {s.current_price:,}원  →  "
              f"진입 {s.entry_price:,}  |  손절 {s.stop_price:,} (-5%)  |  "
              f"목표 {s.target_price:,} (+15%)")

        # 포지션
        print(f"  [포지션] {s.quantity}주 × {s.entry_price:,}원 = "
              f"{s.position_size:,}원  |  R값 {s.r_value:,.0f}  |  "
              f"R배수 {s.r_multiplier}")

        # 수급
        print(f"  [수급] 외국인 5일 {s.foreign_5d:+,}  |  "
              f"기관 5일 {s.inst_5d:+,}  |  "
              f"거래대금 {fmt_value(s.trading_value)}")

        # 품질
        print(f"  [품질] {s.quality:.1f}/100")

        # 테마
        if s.themes:
            print(f"  [테마] {', '.join(s.themes)}")

        # LLM 이유
        if sc.llm_reason:
            print(f"  [LLM] {sc.llm_reason}")

    # 시장별 분포
    print(f"\n{'=' * 70}")
    market_str = "  ".join(f"{k}: {v}개" for k, v in result.by_market.items())
    print(f"  시장별: {market_str}")

    # 테마 집계
    all_themes = []
    for s in result.signals:
        all_themes.extend(s.themes)
    if all_themes:
        theme_counts = Counter(all_themes).most_common(5)
        theme_str = "  ".join(f"{t}({c})" for t, c in theme_counts)
        print(f"  테마:   {theme_str}")

    print(f"  소요:   {elapsed:.1f}초")
    print("=" * 70)


async def main(no_telegram: bool = False):
    config = SignalConfig()
    generator = SignalGenerator(config=config, capital=10_000_000)

    start = time.time()

    # 1. 시그널 생성
    signals = await generator.generate(top_n=25)

    elapsed_ms = (time.time() - start) * 1000

    # 2. ScreenerResult 구성
    today = date.today()

    by_grade = {}
    by_market = {}
    for s in signals:
        g = s.grade.value
        by_grade[g] = by_grade.get(g, 0) + 1
        by_market[s.market] = by_market.get(s.market, 0) + 1

    result = ScreenerResult(
        date=today,
        total_candidates=30,  # generator 내부에서 최대 top_n * 2
        filtered_count=len(signals),
        signals=signals,
        by_grade=by_grade,
        by_market=by_market,
        processing_time_ms=elapsed_ms,
    )

    # 3. JSON 저장
    log.info("[저장] JSON 파일 저장 중...")
    save_result_to_json(result)

    # 4. 결과 출력
    print_result(result)

    # 5. 텔레그램 전송
    if no_telegram:
        log.info("텔레그램 전송 건너뜀 (--no-telegram)")
    else:
        ok = notify_signal_results()
        if ok:
            log.info("텔레그램 전송 완료")
        else:
            log.info("텔레그램 미설정")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="종가베팅 V2 엔진")
    parser.add_argument("--no-telegram", action="store_true", help="텔레그램 전송 건너뜀")
    args = parser.parse_args()
    asyncio.run(main(no_telegram=args.no_telegram))
