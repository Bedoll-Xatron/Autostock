"""
역발상 반전 엔진 (Contrarian Reversal Engine)
=============================================
RSI 과매도 + 지지선 근접 종목을 탐지합니다.

파이프라인:
  1. 유니버스: KOSPI + KOSDAQ 하락률 상위 (down) + 소폭 하락종목
  2. 차트 60일 수집
  3. RSI(14) 계산 (Wilder 스무딩)
  4. oversold_score : RSI < 40이면 (40 - RSI) × 2.5  → 0-100
  5. support_level  : 최근 20일 저점
  6. reversal_probability:
       · RSI 기반 기초 확률
       · 지지선 근접 보너스 (+0.10 if price < support × 1.03)
       · 거래량 감소 보너스 (+0.05 if vol_ratio < 0.7)
       · 0.95 상한
  7. 필터: RSI < 40 AND reversal_probability >= 0.40
  8. 저장: marketflow/data/contrarian_latest.json
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Optional

import requests

# ── 경로 설정 ──────────────────────────────────────────────────────
_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR   = os.path.normpath(os.path.join(_ENGINE_DIR, '..', 'data'))
sys.path.insert(0, _ENGINE_DIR)

from collectors import get_chart_data  # type: ignore


# ── 상수 ───────────────────────────────────────────────────────────

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
}
_MARKETS = ('KOSPI', 'KOSDAQ')
_EXCLUDE_KW = ('ETF', 'ETN', '스팩', '리츠', '인버스', '레버리지',
               'KODEX', 'TIGER', 'KBSTAR', 'ARIRANG', 'ACE ', 'HANARO')
_RSI_PERIOD  = 14
_RSI_OVERSOLD = 40   # 이하면 과매도 후보


# ── 데이터 클래스 ──────────────────────────────────────────────────

@dataclass
class ContrarianCandidate:
    code: str
    name: str
    market: str
    price: float
    change_pct: float


@dataclass
class ContrarianSignal:
    ticker: str
    name: str
    market: str
    score: int                  # reversal_probability × 10 (0-10, 정수)
    oversold_score: float       # 0-100
    reversal_probability: float # 0.0-1.0
    support_level: float
    signal_date: str
    rsi: float
    price: float
    change_pct: float


# ── 유틸리티 ──────────────────────────────────────────────────────

def _to_int(val) -> int:
    try:
        return int(str(val).replace(',', ''))
    except (ValueError, TypeError):
        return 0

def _to_float(val) -> float:
    try:
        return float(str(val).replace(',', ''))
    except (ValueError, TypeError):
        return 0.0

def _is_excluded(name: str) -> bool:
    if any(kw in name for kw in _EXCLUDE_KW):
        return True
    if name.endswith('우') or name.endswith('우B'):
        return True
    return False


# ── 유니버스 수집 ──────────────────────────────────────────────────

def _fetch_down_stocks(market: str, page_size: int = 60) -> list[ContrarianCandidate]:
    """하락 종목 상위 수집."""
    url = (
        f'https://m.stock.naver.com/api/stocks/down/{market}'
        f'?page=1&pageSize={page_size}'
    )
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f'    [경고] down/{market} 수집 실패: {e}')
        return []

    result: list[ContrarianCandidate] = []
    for s in data.get('stocks', []):
        name = s.get('stockName', '')
        if _is_excluded(name):
            continue
        price = _to_int(s.get('closePrice', 0))
        if not (1_000 <= price <= 300_000):
            continue
        chg = _to_float(s.get('fluctuationsRatio', 0))
        # 너무 급락(-20% 이상)은 추세 하락일 가능성 → 제외
        if chg < -20:
            continue
        result.append(ContrarianCandidate(
            code=s['itemCode'],
            name=name,
            market=market.upper(),
            price=float(price),
            change_pct=chg,
        ))
    return result


def build_universe(top_n: int = 50) -> list[ContrarianCandidate]:
    seen: set[str] = set()
    all_cands: list[ContrarianCandidate] = []

    for market in _MARKETS:
        for c in _fetch_down_stocks(market, top_n):
            if c.code not in seen:
                seen.add(c.code)
                all_cands.append(c)
        time.sleep(0.2)

    print(f'  [유니버스] {len(all_cands)}개 하락 종목 후보')
    return all_cands


# ── 기술 지표 ──────────────────────────────────────────────────────

def _calc_rsi(charts: list, period: int = _RSI_PERIOD) -> float:
    """RSI(period) — Wilder 스무딩."""
    closes = [c.close for c in charts]
    if len(closes) < period + 2:
        return 50.0

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    # 초기 평균
    avg_gain = sum(max(d, 0) for d in deltas[:period]) / period
    avg_loss = sum(max(-d, 0) for d in deltas[:period]) / period

    # Wilder 스무딩
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def _support_level(charts: list, lookback: int = 20) -> float:
    """최근 lookback일 저점."""
    recent = charts[-lookback:]
    if not recent:
        return 0.0
    return float(min(c.low for c in recent))


def _avg_volume(charts: list, period: int = 20) -> float:
    recent = charts[-period:]
    if not recent:
        return 0.0
    return sum(c.volume for c in recent) / len(recent)


def _calc_oversold_score(rsi: float) -> float:
    """RSI → 과매도 점수 (0-100). RSI가 낮을수록 높음."""
    if rsi >= _RSI_OVERSOLD:
        return 0.0
    return round(((_RSI_OVERSOLD - rsi) / _RSI_OVERSOLD) * 100, 1)


def _calc_reversal_prob(
    rsi: float,
    price: float,
    support: float,
    vol_today: int,
    avg_vol: float,
) -> float:
    """
    반전 확률 계산 (0.0-0.95).
    RSI 기반 기초 확률 + 지지선/거래량 보너스.
    """
    # RSI 기반 기초 확률
    if rsi <= 15:
        prob = 0.90
    elif rsi <= 20:
        prob = 0.82
    elif rsi <= 25:
        prob = 0.74
    elif rsi <= 30:
        prob = 0.66
    elif rsi <= 35:
        prob = 0.58
    elif rsi <= 40:
        prob = 0.48
    else:
        return 0.0

    # 지지선 근접 보너스: 가격이 지지선 3% 이내
    if support > 0 and price <= support * 1.03:
        prob += 0.10

    # 거래량 감소 보너스: 하락 중 거래량 축소 → 매도 소진 가능
    if avg_vol > 0 and vol_today < avg_vol * 0.7:
        prob += 0.05

    return round(min(prob, 0.95), 2)


# ── 메인 파이프라인 ────────────────────────────────────────────────

def run(top_n: int = 50, min_rsi_threshold: int = _RSI_OVERSOLD) -> dict:
    """
    역발상 반전 엔진 메인 파이프라인.

    Args:
        top_n             : 마켓별 하락 종목 수집 수
        min_rsi_threshold : 포함 최대 RSI (이하인 종목만)
    """
    today_str = date.today().isoformat()
    t0 = time.time()

    print('=' * 62)
    print(f'  역발상 반전 엔진  |  {today_str}')
    print('=' * 62)

    # 1. 유니버스
    print('\n[1] 하락 종목 유니버스 수집 중...')
    universe = build_universe(top_n)

    # 2. 개별 분석
    print(f'\n[2] {len(universe)}개 종목 RSI 분석 중...\n')
    signals: list[ContrarianSignal] = []

    for i, cand in enumerate(universe, 1):
        print(f'  [{i:>3}/{len(universe)}] {cand.name}({cand.code}) ... ', end='')

        try:
            charts = get_chart_data(cand.code, days=60)
            if not charts or len(charts) < 20:
                print('데이터 부족')
                continue

            latest    = charts[-1]
            price     = float(latest.close)
            rsi       = _calc_rsi(charts)
            support   = _support_level(charts)
            avg_vol   = _avg_volume(charts)
            vol_today = latest.volume

            print(f'RSI={rsi:.1f} ', end='')

            if rsi > min_rsi_threshold:
                print('→ 과매도 아님')
                time.sleep(0.2)
                continue

            oversold  = _calc_oversold_score(rsi)
            rev_prob  = _calc_reversal_prob(rsi, price, support, vol_today, avg_vol)
            score     = int(round(rev_prob * 10))

            signals.append(ContrarianSignal(
                ticker=cand.code,
                name=cand.name,
                market=cand.market,
                score=score,
                oversold_score=oversold,
                reversal_probability=rev_prob,
                support_level=round(support, 0),
                signal_date=today_str,
                rsi=rsi,
                price=price,
                change_pct=cand.change_pct,
            ))
            print(f'과매도={oversold:.0f} 확률={rev_prob:.2f} ✓')

        except Exception as e:
            print(f'에러: {e}')

        time.sleep(0.3)

    # 3. 정렬 (반전확률 내림차순)
    signals.sort(key=lambda s: (-s.reversal_probability, -s.oversold_score))

    # 4. 통계
    cnt         = len(signals)
    avg_s       = round(sum(s.score for s in signals) / cnt, 1) if signals else 0.0
    high_prob   = sum(1 for s in signals if s.reversal_probability > 0.7)
    avg_oversold = round(sum(s.oversold_score for s in signals) / cnt, 1) if signals else 0.0
    elapsed     = round(time.time() - t0, 1)

    print(f'\n{"=" * 62}')
    print(f'  완료: {cnt}개  고확률(>0.7): {high_prob}개  평균과매도: {avg_oversold}  {elapsed}s')
    print(f'{"=" * 62}')
    print(f'\n  {"#":>3}  {"종목명":<14}  {"RSI":>6}  {"과매도":>6}  {"확률":>6}  {"지지선":>10}')
    print(f'  {"─" * 55}')
    for rank, s in enumerate(signals[:10], 1):
        print(
            f'  {rank:>3}  {s.name:<14}  {s.rsi:>6.1f}  '
            f'{s.oversold_score:>6.1f}  {s.reversal_probability:>6.2f}  '
            f'₩{s.support_level:>9,.0f}'
        )

    return {
        'date': today_str,
        'signals': [asdict(s) for s in signals],
        'stats': {
            'total':        cnt,
            'avg_score':    avg_s,
            'high_prob':    high_prob,
            'avg_oversold': avg_oversold,
        },
        'processing_time_s': elapsed,
        'updated_at': datetime.now().isoformat(),
    }


def save_results(result: dict) -> str:
    os.makedirs(_DATA_DIR, exist_ok=True)
    path = os.path.join(_DATA_DIR, 'contrarian_latest.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    date_str = result.get('date', '').replace('-', '')
    if len(date_str) == 8:
        dated_path = os.path.join(_DATA_DIR, f'contrarian_{date_str}.json')
        with open(dated_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'\n  => {path} 저장 완료 ({result["stats"]["total"]}개 시그널)')
    return path
