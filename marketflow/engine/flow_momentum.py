"""
수급 모멘텀 엔진 (Flow Momentum Engine)
========================================
외국인/기관 5일 누적 순매수가 강한 종목을 발굴합니다.

파이프라인:
  1. 유니버스 구성
       · Naver 모바일 API  거래대금 상위 (KOSPI + KOSDAQ)
       · Naver 모바일 API  상승률  상위 (KOSPI + KOSDAQ)
       · 최근 10개 jongga 시그널 파일 종목 (이미 수급이 검증됨)
  2. 개별 종목 분석
       · get_chart_data(60일)  — 추세 / 거래량 비율
       · get_supply_data()     — 외국인/기관 5일 순매수 주수
       · 억원 환산 = 주수 × 현재가 / 1e8
  3. 스코어링 (10점 만점)
       · flow_score  (0-5): 수급 강도  — 쌍매수 + 규모 기준
       · trend_score (0-3): 이동평균 정배열 (MA20/MA60)
       · vol_score   (0-2): 거래대금 급증 비율 (vs 20일 평균)
  4. 시그널 분류
       · strong:   flow >= 3 AND total >= 7
       · moderate: flow >= 2 AND total >= 4
       · weak:     나머지
  5. 저장: marketflow/data/flow_momentum_latest.json
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
from bs4 import BeautifulSoup

# ── 경로 설정 ─────────────────────────────────────────────────────
_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR   = os.path.normpath(os.path.join(_ENGINE_DIR, '..', 'data'))
sys.path.insert(0, _ENGINE_DIR)

from collectors import get_chart_data, get_supply_data  # type: ignore

# ── 상수 ──────────────────────────────────────────────────────────
_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Referer': 'https://finance.naver.com/',
}

_MARKETS = ('KOSPI', 'KOSDAQ')

_EXCLUDE_KW = (
    'ETF', 'ETN', '스팩', '리츠', '인버스', '레버리지',
    'KODEX', 'TIGER', 'KBSTAR', 'ARIRANG', 'ACE ', 'HANARO',
)

_MIN_PRICE          = 1_000
_MAX_PRICE          = 500_000
_MIN_TRADING_VALUE  = 5_000_000_000   # 50억
_MIN_FLOW_SCORE     = 1               # 기본 필터: 최소 flow_score


# ── 데이터 클래스 ─────────────────────────────────────────────────

@dataclass
class _Candidate:
    code: str
    name: str
    market: str
    price: float
    change_pct: float
    trading_value: float  # 원 단위


@dataclass
class FlowSignal:
    ticker: str
    name: str
    market: str
    # 점수
    score: int            # 0-10 (total)
    flow_score: int       # 0-5
    trend_score: int      # 0-3
    vol_score: int        # 0-2
    # 수급
    foreign_flow: float   # 억원 (5d, 양수=순매수)
    institution_flow: float  # 억원 (5d)
    # 거래량
    volume_ratio: float   # 오늘 거래대금 / 20일 평균
    # 분류
    signal_strength: str  # 'strong' | 'moderate' | 'weak'
    signal_date: str
    # 보조 정보
    price: float
    change_pct: float
    ma20: Optional[float]
    ma60: Optional[float]
    trend: str            # 'above_both' | 'above_ma60' | 'above_ma20' | 'below'


# ── 유틸리티 ─────────────────────────────────────────────────────

def _to_int(val: object) -> int:
    try:
        return int(str(val).replace(',', ''))
    except (ValueError, TypeError):
        return 0


def _to_float(val: object) -> float:
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


# ── 유니버스 수집 ─────────────────────────────────────────────────

def _fetch_naver_stocks(endpoint: str, market: str, page_size: int = 60) -> list[_Candidate]:
    """
    네이버 모바일 API에서 종목 리스트를 가져온다.
    endpoint: 'up' | 'amount' | 'volume'
    """
    url = (
        f'https://m.stock.naver.com/api/stocks/{endpoint}/{market}'
        f'?page=1&pageSize={page_size}'
    )
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f'    [경고] Naver API {endpoint}/{market} 실패: {e}')
        return []

    candidates: list[_Candidate] = []
    for s in data.get('stocks', []):
        name = s.get('stockName', '')
        if _is_excluded(name):
            continue
        price = _to_int(s.get('closePrice', 0))
        if not (_MIN_PRICE <= price <= _MAX_PRICE):
            continue
        # accumulatedTradingValue 단위: 만원 → 원으로 환산
        tv = _to_int(s.get('accumulatedTradingValue', 0)) * 1_000_000
        if tv < _MIN_TRADING_VALUE:
            continue
        candidates.append(_Candidate(
            code=s['itemCode'],
            name=name,
            market=market.upper(),
            price=float(price),
            change_pct=_to_float(s.get('fluctuationsRatio', 0)),
            trading_value=float(tv),
        ))
    return candidates


def _load_jongga_codes() -> list[_Candidate]:
    """최근 10개 jongga 시그널 파일에서 종목 추출."""
    result: list[_Candidate] = []
    seen: set[str] = set()
    files = sorted(
        glob.glob(os.path.join(_DATA_DIR, 'jongga_v2_results_*.json')),
        reverse=True,
    )[:10]

    for fp in files:
        try:
            d = json.load(open(fp, encoding='utf-8'))
            for s in d.get('signals', []):
                code = s.get('stock_code', '')
                if not code or code in seen:
                    continue
                seen.add(code)
                result.append(_Candidate(
                    code=code,
                    name=s.get('stock_name', ''),
                    market=s.get('market', 'KOSPI'),
                    price=float(s.get('entry_price') or 0),
                    change_pct=float(s.get('change_pct') or 0),
                    trading_value=float(s.get('trading_value') or 0),
                ))
        except Exception:
            pass
    return result


def build_universe(top_n: int = 40) -> list[_Candidate]:
    """
    KOSPI + KOSDAQ 거래대금/상승률 상위 + jongga 역사 종목을 합쳐
    후보 유니버스를 구성한다.
    """
    seen: set[str] = set()
    all_cands: list[_Candidate] = []

    for market in _MARKETS:
        # 거래대금 상위 — 없으면 상승률로 대체
        amount = _fetch_naver_stocks('amount', market, top_n)
        if not amount:
            print(f'    [{market}] amount 엔드포인트 없음 → up 으로 대체')
            amount = _fetch_naver_stocks('up', market, top_n)

        # 상승률 상위 (별도 추가)
        gainers = _fetch_naver_stocks('up', market, top_n)

        for c in amount + gainers:
            if c.code not in seen:
                seen.add(c.code)
                all_cands.append(c)

        time.sleep(0.2)

    # jongga 역사 종목
    for c in _load_jongga_codes():
        if c.code not in seen:
            seen.add(c.code)
            all_cands.append(c)

    print(f'  [유니버스] {len(all_cands)}개 후보 종목')
    return all_cands


# ── 차트 보조 계산 ────────────────────────────────────────────────

def _compute_ma60(charts: list) -> Optional[float]:
    """
    60일 데이터로 MA60 계산.
    데이터가 60개 미만이면 가용 데이터 평균을 사용.
    """
    closes = [c.close for c in charts]
    if not closes:
        return None
    return round(sum(closes[-60:]) / min(len(closes), 60), 0)


def _avg_trading_value_20d(charts: list) -> float:
    """
    최근 20일 평균 거래대금 추정.
    거래대금 = 종가 × 거래량 (근사값)
    """
    recent = charts[-20:]
    if not recent:
        return 0.0
    vals = [c.close * c.volume for c in recent]
    return sum(vals) / len(vals)


# ── 스코어링 ──────────────────────────────────────────────────────

def _calc_flow_score(foreign_flow: float, inst_flow: float) -> int:
    """
    외국인/기관 5일 누적 순매수 강도 점수 (0-5).

    기준 (억원):
      5점: 쌍매수 + 합산 200억+
      4점: 쌍매수 + 합산 100억+
      3점: 쌍매수 + 합산 30억+
      2점: 쌍매수(소규모) 또는 단독 50억+
      1점: 단독 매수
      0점: 쌍매도
    """
    combined = foreign_flow + inst_flow
    both_pos = foreign_flow > 0 and inst_flow > 0
    one_pos  = foreign_flow > 0 or inst_flow > 0
    one_max  = max(abs(foreign_flow), abs(inst_flow))

    if both_pos:
        if combined >= 200:
            return 5
        if combined >= 100:
            return 4
        if combined >= 30:
            return 3
        return 2
    if one_pos:
        if one_max >= 50:
            return 2
        return 1
    return 0


def _calc_trend_score(
    price: float,
    ma20: Optional[float],
    ma60: Optional[float],
) -> tuple[int, str]:
    """
    이동평균 정배열 기반 추세 점수 (0-3).

    3점: 가격 > MA20 > MA60
    2점: 가격 > MA20 and 가격 > MA60 (but MA20 <= MA60)
    1점: 가격 > MA60 또는 가격 > MA20 (단일)
    0점: 모두 하방
    """
    if not price:
        return 0, 'unknown'

    above_ma20 = ma20 is not None and ma20 > 0 and price > ma20
    above_ma60 = ma60 is not None and ma60 > 0 and price > ma60
    ma20_gt_ma60 = (
        ma20 is not None and ma60 is not None
        and ma20 > 0 and ma60 > 0
        and ma20 > ma60
    )

    if above_ma20 and above_ma60:
        if ma20_gt_ma60:
            return 3, 'above_both'
        return 2, 'above_both'
    if above_ma60:
        return 1, 'above_ma60'
    if above_ma20:
        return 1, 'above_ma20'
    return 0, 'below'


def _calc_vol_score(current_tv: float, avg_tv: float) -> tuple[int, float]:
    """
    거래대금 급증 점수 (0-2) + ratio 반환.

    2점: 현재 >= 3배 평균
    1점: 현재 >= 1.5배 평균
    0점: 평균 이하
    """
    if avg_tv <= 0:
        return 0, 0.0
    ratio = round(current_tv / avg_tv, 2)
    if ratio >= 3.0:
        return 2, ratio
    if ratio >= 1.5:
        return 1, ratio
    return 0, ratio


def _classify_strength(flow_s: int, total: int) -> str:
    """시그널 강도 분류."""
    if flow_s >= 3 and total >= 7:
        return 'strong'
    if flow_s >= 2 and total >= 4:
        return 'moderate'
    return 'weak'


# ── 개별 종목 분석 ────────────────────────────────────────────────

def analyze_stock(cand: _Candidate, today_str: str) -> Optional[FlowSignal]:
    """
    1개 종목에 대해 차트 + 수급 수집 → 스코어링 → FlowSignal 반환.

    수급이 쌍매도(양쪽 모두 0 이하)이면 None 반환.
    """
    code, name, market = cand.code, cand.name, cand.market

    try:
        # ── 차트 (60일) ──────────────────────────────────────────
        charts = get_chart_data(code, days=60)
        if not charts:
            return None

        latest = charts[-1]
        price  = float(latest.close)
        if price <= 0:
            return None

        ma20   = latest.ma20
        ma60   = _compute_ma60(charts)
        avg_tv = _avg_trading_value_20d(charts)

        # ── 수급 (5일) ───────────────────────────────────────────
        supply = get_supply_data(code)
        f5d = supply.foreign_net_5d  # 순매수 주수
        i5d = supply.inst_net_5d

        # 억원 환산 (주수 × 현재가 / 1억)
        foreign_flow = round(f5d * price / 1e8, 1)
        inst_flow    = round(i5d * price / 1e8, 1)

        # 쌍매도면 건너뜀
        if foreign_flow <= 0 and inst_flow <= 0:
            return None

        # ── 거래대금 ─────────────────────────────────────────────
        # candidate 값이 있으면 사용, 없으면 차트에서 추정
        cur_tv = (
            cand.trading_value
            if cand.trading_value > 0
            else price * latest.volume
        )

        # ── 스코어 계산 ──────────────────────────────────────────
        f_score              = _calc_flow_score(foreign_flow, inst_flow)
        t_score, trend       = _calc_trend_score(price, ma20, ma60)
        v_score, v_ratio     = _calc_vol_score(cur_tv, avg_tv)
        total                = f_score + t_score + v_score
        strength             = _classify_strength(f_score, total)

        return FlowSignal(
            ticker=code,
            name=name,
            market=market,
            score=total,
            flow_score=f_score,
            trend_score=t_score,
            vol_score=v_score,
            foreign_flow=foreign_flow,
            institution_flow=inst_flow,
            volume_ratio=round(v_ratio, 2),
            signal_strength=strength,
            signal_date=today_str,
            price=price,
            change_pct=cand.change_pct,
            ma20=ma20,
            ma60=ma60,
            trend=trend,
        )

    except Exception as e:
        print(f'    [에러] {name}({code}): {e}')
        return None


# ── 메인 파이프라인 ───────────────────────────────────────────────

def run(top_n: int = 40, min_flow_score: int = _MIN_FLOW_SCORE) -> dict:
    """
    수급 모멘텀 엔진 메인 파이프라인.

    Args:
        top_n: 마켓별 수집 종목 수
        min_flow_score: 결과 포함 최소 flow_score

    Returns:
        저장 형식과 동일한 dict
    """
    today_str = date.today().isoformat()
    t0 = time.time()

    print('=' * 62)
    print(f'  수급 모멘텀 엔진  |  {today_str}')
    print('=' * 62)

    # 1. 유니버스
    print('\n[1] 유니버스 수집 중...')
    universe = build_universe(top_n)

    # 2. 개별 분석
    print(f'\n[2] {len(universe)}개 종목 분석 중...\n')
    signals: list[FlowSignal] = []

    for i, cand in enumerate(universe, 1):
        print(f'  [{i:>3}/{len(universe)}] {cand.name}({cand.code}) ... ', end='')
        sig = analyze_stock(cand, today_str)
        if sig is None:
            print('skip')
        elif sig.flow_score < min_flow_score:
            print(f'flow={sig.flow_score} → 필터')
        else:
            signals.append(sig)
            print(
                f'flow={sig.flow_score} trend={sig.trend_score} '
                f'vol={sig.vol_score} → 총점 {sig.score} [{sig.signal_strength}]'
            )
        time.sleep(0.3)  # Naver 요청 간격

    # 3. 정렬 (총점 → flow_score 우선)
    signals.sort(key=lambda s: (-s.score, -s.flow_score))

    # 4. 통계
    cnt      = len(signals)
    avg_s    = round(sum(s.score for s in signals) / cnt, 1) if signals else 0.0
    strong   = sum(1 for s in signals if s.signal_strength == 'strong')
    moderate = sum(1 for s in signals if s.signal_strength == 'moderate')
    weak     = sum(1 for s in signals if s.signal_strength == 'weak')
    elapsed  = round(time.time() - t0, 1)

    # 5. 결과 출력
    print(f'\n{"=" * 62}')
    print(f'  완료: {cnt}개  (강:{strong} 중:{moderate} 약:{weak})  {elapsed}s')
    print(f'{"=" * 62}')
    print(f'\n  {"#":>3}  {"종목명":<14}  {"총점":>4}  {"외국인":>8}  {"기관":>8}  {"강도"}')
    print(f'  {"─" * 55}')
    for rank, s in enumerate(signals[:15], 1):
        print(
            f'  {rank:>3}  {s.name:<14}  {s.score:>4}  '
            f'{s.foreign_flow:>+7.0f}억  {s.institution_flow:>+7.0f}억  '
            f'{s.signal_strength}'
        )

    return {
        'date': today_str,
        'signals': [_to_dict(s) for s in signals],
        'stats': {
            'total': cnt,
            'avg_score': avg_s,
            'strong': strong,
            'moderate': moderate,
            'weak': weak,
        },
        'processing_time_s': elapsed,
        'updated_at': datetime.now().isoformat(),
    }


def _to_dict(s: FlowSignal) -> dict:
    d = asdict(s)
    d['ma20'] = d['ma20'] or 0
    d['ma60'] = d['ma60'] or 0
    return d


def save_results(result: dict) -> str:
    """flow_momentum_latest.json + 날짜별 파일 저장 후 경로 반환."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    path = os.path.join(_DATA_DIR, 'flow_momentum_latest.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    date_str = result.get('date', '').replace('-', '')
    if len(date_str) == 8:
        dated_path = os.path.join(_DATA_DIR, f'flow_momentum_{date_str}.json')
        with open(dated_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'\n  => {path} 저장 완료 ({result["stats"]["total"]}개 시그널)')
    return path
