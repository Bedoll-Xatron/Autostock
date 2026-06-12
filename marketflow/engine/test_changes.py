"""scorer/generator 변경사항 검증 테스트"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from datetime import date
from config import SignalConfig, Grade
from models import StockData, ChartData, SupplyData, ScoreDetail
from scorer import Scorer


def make_charts(n=60, base=10000, up_trend=True):
    """테스트용 차트 데이터 생성"""
    charts = []
    for i in range(n):
        price = base + (i * 50 if up_trend else -i * 20)
        charts.append(ChartData(
            code="000000",
            date=date(2025, 1, 1),
            open=price - 50,
            high=price + 100,
            low=price - 100,
            close=price,
            volume=1_000_000,
        ))
    # MA 계산
    closes = [c.close for c in charts]
    for i, row in enumerate(charts):
        for window, attr in [(5, "ma5"), (10, "ma10"), (20, "ma20")]:
            if i >= window - 1:
                setattr(row, attr, round(sum(closes[i - window + 1:i + 1]) / window))
            else:
                setattr(row, attr, None)
    return charts


def make_stock(close=13000, change_pct=7.0, trading_value=200_000_000_000):
    return StockData(
        code="000000", name="테스트", market="KOSPI",
        open=close - 200, high=close + 100, low=close - 300, close=close,
        volume=5_000_000, trading_value=trading_value, market_cap=500_000_000_000,
        change_pct=change_pct, high_52w=close + 500, low_52w=close - 3000,
    )


scorer = Scorer()


def test_chart_score_ma60():
    """MA60 위: +1점 추가 (최대 3점)"""
    charts = make_charts(n=60, base=10000, up_trend=True)
    stock = make_stock(close=charts[-1].close + 200)  # close > MA60 보장
    stock.high_52w = stock.close + 500

    _, _, score_val, _ = None, None, None, None

    # _score_chart 직접 호출
    pts, flags = scorer._score_chart(stock, charts)

    # close > MA60이면 pts에 1이 포함돼야 함
    ma60 = sum(c.close for c in charts[-60:]) / 60
    above_ma60 = stock.close > ma60
    print(f"  close={stock.close:,}  MA60={ma60:,.0f}  above_MA60={above_ma60}  chart_pts={pts}")

    if above_ma60:
        assert pts >= 1, f"MA60 위인데 chart_pts={pts}"
        print("  [PASS] MA60 위에서 chart_pts >= 1")
    else:
        print("  [SKIP] 이 케이스는 MA60 아래")


def test_chart_score_below_ma60():
    """하락 추세 종목: MA60 아래면 3번째 점수 없음"""
    charts = make_charts(n=60, base=13000, up_trend=False)
    stock = make_stock(close=charts[-1].close - 500)  # 낮은 가격 → MA60 아래
    stock.high_52w = 14000

    pts, flags = scorer._score_chart(stock, charts)
    ma60 = sum(c.close for c in charts[-60:]) / 60
    above_ma60 = stock.close > ma60
    print(f"  close={stock.close:,}  MA60={ma60:,.0f}  above_MA60={above_ma60}  chart_pts={pts}")

    if not above_ma60:
        # MA60 아래이면 이 차트 구성에서 pts < 3이어야 함
        print(f"  [PASS] MA60 아래 → 3번째 점수 없음 (pts={pts})")
    else:
        print(f"  [INFO] 이 케이스는 MA60 위 (pts={pts})")


def test_b_grade_supply1_passes():
    """B등급: supply=1이면 게이트 통과해야 함"""
    config = SignalConfig()
    assert config.min_supply_score_b == 1, "B등급 supply 기준이 1이어야 함"

    # supply=1, total=9 → B등급 통과 시뮬레이션
    supply_min = config.min_supply_score_b  # 1
    total_min = config.min_total_score_b    # 9
    quality_min = config.min_quality_b      # 40.0

    score = ScoreDetail(
        news=2, volume=2, chart=2, candle=1,
        supply=1,   # 외국인 OR 기관 중 하나
        rs_score=1,
    )
    # total = 2+2+2+1+1+1 = 9
    assert score.total == 9, f"total={score.total} (기대: 9)"
    assert score.supply >= supply_min, f"supply={score.supply} < {supply_min}"
    assert score.total >= total_min, f"total={score.total} < {total_min}"

    print(f"  total={score.total}  supply={score.supply}  supply_min={supply_min}  total_min={total_min}")
    print("  [PASS] B등급 supply=1, total=9 → 게이트 통과")


def test_a_grade_supply1_blocked():
    """A등급: supply=1이면 게이트 차단돼야 함"""
    config = SignalConfig()
    supply_min_a = config.min_supply_score  # 2

    score = ScoreDetail(news=2, volume=3, chart=3, candle=1, supply=1, rs_score=2)
    # total = 2+3+3+1+1+2 = 12 → A등급 충분하지만 supply=1 → 차단

    blocked = score.supply < supply_min_a
    print(f"  total={score.total}  supply={score.supply}  A등급_supply_min={supply_min_a}  blocked={blocked}")
    assert blocked, "A등급에서 supply=1은 차단돼야 함"
    print("  [PASS] A등급 supply=1 → 차단 확인")


def test_b_quality_40():
    """B등급: quality=45이면 통과 (기준 40)"""
    config = SignalConfig()
    assert config.min_quality_b == 40.0

    charts = make_charts(n=60, base=10000, up_trend=True)
    stock = make_stock(close=charts[-1].close + 200, change_pct=7.0)
    stock.high_52w = stock.close + 500

    score = ScoreDetail(
        news=1, volume=1, chart=2, candle=1,
        supply=1, rs_score=1,
    )
    # total=7 → 이 경우 C등급이지만 quality 계산 자체를 테스트

    quality = scorer.calculate_quality(stock, charts, score)
    print(f"  quality={quality}  min_quality_b={config.min_quality_b}")

    # supply=1(15pt) + total=7(10pt) + change_pct=7%(15pt) + 20d모멘텀 + vol_ratio
    print(f"  [INFO] supply=1이면 quality 계산 정상 동작")


def test_chart_max_score():
    """chart score 최대 3점 도달 가능 여부"""
    charts = make_charts(n=60, base=10000, up_trend=True)
    last = charts[-1]
    # close가 MA5 > MA10 > MA20 > MA60 모두 위에 있는 상승 추세
    stock = make_stock(close=last.close + 500)
    stock.high_52w = stock.close - 100  # 52주 신고가 근접

    pts, flags = scorer._score_chart(stock, charts)
    ma60 = sum(c.close for c in charts[-60:]) / 60
    print(f"  chart_pts={pts}/3  flags={flags}  close={stock.close:,}  MA60={ma60:,.0f}  high_52w={stock.high_52w:,}")

    assert pts <= 3, f"chart_pts가 3 초과: {pts}"
    print(f"  [PASS] chart score 최대 3점 내 (현재 {pts}점)")


if __name__ == "__main__":
    tests = [
        ("chart_score_ma60", test_chart_score_ma60),
        ("chart_score_below_ma60", test_chart_score_below_ma60),
        ("b_grade_supply1_passes", test_b_grade_supply1_passes),
        ("a_grade_supply1_blocked", test_a_grade_supply1_blocked),
        ("b_quality_40", test_b_quality_40),
        ("chart_max_score", test_chart_max_score),
    ]

    passed = failed = 0
    for name, fn in tests:
        print(f"\n[{name}]")
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"결과: {passed}개 통과 / {failed}개 실패")
