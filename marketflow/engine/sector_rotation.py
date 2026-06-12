"""
섹터 로테이션 엔진 (Sector Rotation Engine)
============================================
업종 순환 국면을 분석하고 강세 섹터 내 유망 종목을 발굴합니다.

파이프라인:
  1. 최근 jongga 시그널 로드 (이미 LLM 테마 포함)
  2. 테마 → 섹터 매핑
  3. KODEX200(069500) 차트로 KOSPI 20일 수익률 계산
  4. 종목 차트 20일 수익률 계산 → 상대강도(RS) 산출
  5. MA 포지션으로 로테이션 국면 결정
       accumulation : price > MA60, price < MA20  (매집)
       markup       : price > MA20 > MA60         (상승)
       distribution : price < MA20, price > MA60  (분산/하락 경고)
       markdown     : price < MA20, price < MA60  (하락)
  6. 저장: marketflow/data/sector_rotation_latest.json
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

# ── 경로 설정 ──────────────────────────────────────────────────────
_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR   = os.path.normpath(os.path.join(_ENGINE_DIR, '..', 'data'))
sys.path.insert(0, _ENGINE_DIR)

from collectors import get_chart_data  # type: ignore


# ── 섹터 매핑 (테마 → 업종) ────────────────────────────────────────

_THEME_SECTOR: dict[str, str] = {
    # 반도체/전자
    '반도체': '반도체/전자',       '메모리 반도체': '반도체/전자',
    '반도체 소부장': '반도체/전자', 'HBM': '반도체/전자',
    'AI 반도체': '반도체/전자',     '파운드리': '반도체/전자',
    # AI/소프트웨어
    'AI': 'AI/소프트웨어',          '인공지능': 'AI/소프트웨어',
    '클라우드': 'AI/소프트웨어',    '빅데이터': 'AI/소프트웨어',
    '소프트웨어': 'AI/소프트웨어',  'SaaS': 'AI/소프트웨어',
    # 배터리/전기차
    '2차전지': '배터리/전기차',     '배터리': '배터리/전기차',
    '전기차': '배터리/전기차',      'EV': '배터리/전기차',
    '양극재': '배터리/전기차',      '전고체': '배터리/전기차',
    # 방산/항공우주
    '방산': '방산/항공우주',        '방위산업': '방산/항공우주',
    '항공우주': '방산/항공우주',    '드론': '방산/항공우주',
    # 바이오/헬스케어
    '바이오': '바이오/헬스케어',    '헬스케어': '바이오/헬스케어',
    '제약': '바이오/헬스케어',      '신약': '바이오/헬스케어',
    '진단': '바이오/헬스케어',      'ADC': '바이오/헬스케어',
    # 로봇/자동화
    '로봇': '로봇/자동화',          '협동로봇': '로봇/자동화',
    '자동화': '로봇/자동화',        '스마트팩토리': '로봇/자동화',
    # 에너지
    '원전': '에너지/원전',          '신재생에너지': '에너지/원전',
    '태양광': '에너지/원전',        '수소': '에너지/원전',
    # 금융
    '금융': '금융/핀테크',          '핀테크': '금융/핀테크',
    '보험': '금융/핀테크',          '증권': '금융/핀테크',
    # 엔터/콘텐츠
    '엔터': '엔터/콘텐츠',          'K-pop': '엔터/콘텐츠',
    '콘텐츠': '엔터/콘텐츠',        '게임': '엔터/콘텐츠',
    'OTT': '엔터/콘텐츠',
    # 건설/부동산
    '건설': '건설/부동산',          '리츠': '건설/부동산',
    # 화학/소재
    '화학': '화학/소재',            '소재': '화학/소재',
    '철강': '화학/소재',
    # 소비재/유통
    '소비재': '소비재/유통',        '유통': '소비재/유통',
    '음식료': '소비재/유통',        '패션': '소비재/유통',
}


def _map_sector(themes: list[str]) -> str:
    """테마 목록에서 섹터를 결정한다."""
    for theme in themes:
        for keyword, sector in _THEME_SECTOR.items():
            if keyword in theme:
                return sector
    return '기타'


# ── 데이터 클래스 ──────────────────────────────────────────────────

@dataclass
class SectorSignal:
    ticker: str
    name: str
    market: str
    score: int                # 0-10 (relative_strength 기반)
    sector: str
    rotation_phase: str       # accumulation | markup | distribution | markdown
    relative_strength: float  # 0-100
    signal_date: str
    price: float
    ma20: Optional[float]
    ma60: Optional[float]
    rs_raw: float             # 종목 20d 수익률 - KOSPI 20d 수익률


# ── 차트 보조 ──────────────────────────────────────────────────────

def _compute_ma60(charts: list) -> Optional[float]:
    closes = [c.close for c in charts]
    if not closes:
        return None
    return round(sum(closes[-60:]) / min(len(closes), 60), 0)


def _return_20d(charts: list) -> float:
    """최근 20일 수익률(%)."""
    if len(charts) < 21:
        return 0.0
    close_now  = charts[-1].close
    close_prev = charts[-21].close
    if close_prev <= 0:
        return 0.0
    return round((close_now - close_prev) / close_prev * 100, 2)


def _rotation_phase(price: float, ma20: Optional[float], ma60: Optional[float]) -> str:
    """
    MA 포지션으로 로테이션 국면 결정.
    accumulation : price > MA60 AND price <= MA20 (바닥 매집)
    markup       : price > MA20 AND MA20 > MA60   (상승 추세)
    distribution : price < MA20 AND price > MA60  (하락 경고)
    markdown     : price < MA60                   (하락 추세)
    """
    if not price:
        return 'markdown'

    above_ma20 = ma20 and price > ma20
    above_ma60 = ma60 and price > ma60
    ma20_gt_ma60 = ma20 and ma60 and ma20 > ma60

    if above_ma20 and above_ma60 and ma20_gt_ma60:
        return 'markup'
    if not above_ma20 and above_ma60:
        return 'accumulation'
    if above_ma20 and above_ma60 and not ma20_gt_ma60:
        return 'accumulation'
    if not above_ma60 and above_ma20:
        return 'distribution'
    return 'markdown'


def _rs_to_score(rs: float) -> int:
    """상대강도 수치(%) → 0-10점."""
    # rs: 종목 - 시장 수익률 차이
    if rs >= 10:  return 10
    if rs >= 7:   return 9
    if rs >= 5:   return 8
    if rs >= 3:   return 7
    if rs >= 1:   return 6
    if rs >= 0:   return 5
    if rs >= -2:  return 4
    if rs >= -5:  return 3
    if rs >= -8:  return 2
    if rs >= -10: return 1
    return 0


def _rs_to_pct(rs: float) -> float:
    """상대강도 차이(%) → 0-100 표준화."""
    # rs 범위를 -15% ~ +15% 로 가정하고 0-100 스케일
    clamped = max(-15.0, min(15.0, rs))
    return round((clamped + 15) / 30 * 100, 1)


# ── 데이터 로드 ────────────────────────────────────────────────────

def _load_jongga_signals(max_files: int = 15) -> list[dict]:
    files = sorted(
        glob.glob(os.path.join(_DATA_DIR, 'jongga_v2_results_*.json')),
        reverse=True,
    )[:max_files]

    seen: set[str] = set()
    result: list[dict] = []
    for fp in files:
        try:
            d = json.load(open(fp, encoding='utf-8'))
            for s in d.get('signals', []):
                code = s.get('stock_code', '')
                if not code or code in seen:
                    continue
                seen.add(code)
                result.append(s)
        except Exception:
            pass
    return result


def _get_kospi_return() -> float:
    """KODEX200(069500) 20일 수익률로 KOSPI 기준 수익률 계산."""
    try:
        charts = get_chart_data('069500', days=25)
        return _return_20d(charts)
    except Exception:
        return 0.0


# ── 메인 파이프라인 ────────────────────────────────────────────────

def run(max_files: int = 15) -> dict:
    today_str = date.today().isoformat()
    t0 = time.time()

    print('=' * 62)
    print(f'  섹터 로테이션 엔진  |  {today_str}')
    print('=' * 62)

    # KOSPI 기준 수익률
    print('\n[1] KOSPI 기준 수익률(KODEX200) 수집 중...')
    kospi_ret = _get_kospi_return()
    print(f'    KOSPI 20일 수익률: {kospi_ret:+.2f}%')

    # jongga 시그널
    print(f'\n[2] jongga 시그널 로드 (최근 {max_files}개 파일)...')
    raw = _load_jongga_signals(max_files)
    print(f'    {len(raw)}개 종목')

    # 차트 + 계산
    print(f'\n[3] {len(raw)}개 종목 차트 분석 중...\n')
    signals: list[SectorSignal] = []

    for i, s in enumerate(raw, 1):
        code   = s.get('stock_code', '')
        name   = s.get('stock_name', '')
        market = s.get('market', '')
        themes = s.get('themes', []) or []
        sector = _map_sector(themes)

        print(f'  [{i:>3}/{len(raw)}] {name}({code}) ... ', end='')

        try:
            charts = get_chart_data(code, days=60)
            if not charts:
                print('데이터 없음')
                continue

            latest  = charts[-1]
            price   = float(latest.close)
            ma20    = latest.ma20
            ma60    = _compute_ma60(charts)
            ret_20d = _return_20d(charts)
            rs_raw  = round(ret_20d - kospi_ret, 2)
            rs_pct  = _rs_to_pct(rs_raw)
            score   = _rs_to_score(rs_raw)
            phase   = _rotation_phase(price, ma20, ma60)

            signals.append(SectorSignal(
                ticker=code,
                name=name,
                market=market,
                score=score,
                sector=sector,
                rotation_phase=phase,
                relative_strength=rs_pct,
                signal_date=s.get('signal_date', today_str),
                price=price,
                ma20=ma20,
                ma60=ma60,
                rs_raw=rs_raw,
            ))
            print(f'RS={rs_raw:+.1f}% → {phase} [{sector}]')

        except Exception as e:
            print(f'에러: {e}')

        time.sleep(0.3)

    # 정렬 (markup 우선, 이후 RS 내림차순)
    phase_order = {'markup': 0, 'accumulation': 1, 'distribution': 2, 'markdown': 3}
    signals.sort(key=lambda s: (phase_order.get(s.rotation_phase, 9), -s.relative_strength))

    cnt     = len(signals)
    avg_s   = round(sum(s.score for s in signals) / cnt, 1) if signals else 0.0
    sectors = list({s.sector for s in signals})
    avg_rs  = round(sum(s.relative_strength for s in signals) / cnt, 1) if signals else 0.0
    elapsed = round(time.time() - t0, 1)

    print(f'\n{"=" * 62}')
    print(f'  완료: {cnt}개  섹터: {len(sectors)}개  평균RS: {avg_rs:.1f}  {elapsed}s')
    print(f'{"=" * 62}')
    print(f'\n  {"#":>3}  {"종목명":<14}  {"섹터":<14}  {"국면":<12}  RS')
    print(f'  {"─" * 55}')
    for rank, s in enumerate(signals[:10], 1):
        print(f'  {rank:>3}  {s.name:<14}  {s.sector:<14}  {s.rotation_phase:<12}  {s.relative_strength:.1f}')

    return {
        'date': today_str,
        'signals': [_to_dict(s) for s in signals],
        'stats': {
            'total':        cnt,
            'avg_score':    avg_s,
            'sector_count': len(sectors),
            'avg_rs':       avg_rs,
        },
        'processing_time_s': elapsed,
        'updated_at': datetime.now().isoformat(),
    }


def _to_dict(s: SectorSignal) -> dict:
    d = asdict(s)
    d['ma20'] = d['ma20'] or 0
    d['ma60'] = d['ma60'] or 0
    return d


def save_results(result: dict) -> str:
    os.makedirs(_DATA_DIR, exist_ok=True)
    path = os.path.join(_DATA_DIR, 'sector_rotation_latest.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    date_str = result.get('date', '').replace('-', '')
    if len(date_str) == 8:
        dated_path = os.path.join(_DATA_DIR, f'sector_rotation_{date_str}.json')
        with open(dated_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'\n  => {path} 저장 완료 ({result["stats"]["total"]}개 시그널)')
    return path
