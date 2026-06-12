"""
테마 모멘텀 엔진 (Narrative Momentum Engine)
=============================================
실시간 뉴스 수집 + Gemini LLM 분석 + 테마 군집화로
내러티브가 강한 종목을 발굴합니다.

jongga 엔진과의 차이:
  · 유니버스 : 관대한 필터 (등락 3-25%, 거래대금 50억+)
  · 수급 게이트 없음 : 뉴스/테마 퀄리티를 1차 기준으로 사용
  · 테마 군집화 : 같은 테마를 공유하는 종목이 많을수록 테마 모멘텀 ↑
  · 비동기 배치 LLM : asyncio.gather로 전체 종목 병렬 분석

스코어링 (0-10):
  · news_pts    (0-4): LLM 뉴스 점수 → 0→0 / 1→2 / 2→3 / 3→4
  · theme_pts   (0-4): 동일 테마 보유 종목 수 (1종목=1점, 최대 4)
  · vol_pts     (0-2): 거래대금 티어

저장: marketflow/data/narrative_momentum_latest.json
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Optional

import requests

# ── 경로 설정 ──────────────────────────────────────────────────────
_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR   = os.path.normpath(os.path.join(_ENGINE_DIR, '..', 'data'))
sys.path.insert(0, _ENGINE_DIR)

from collectors import get_stock_news   # type: ignore
from llm_analyzer import GeminiAnalyzer  # type: ignore


# ── 상수 ───────────────────────────────────────────────────────────

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
}

_MARKETS = ('KOSPI', 'KOSDAQ')

_EXCLUDE_KW = (
    'ETF', 'ETN', '스팩', '리츠', '인버스', '레버리지',
    'KODEX', 'TIGER', 'KBSTAR', 'ARIRANG', 'ACE ', 'HANARO',
)

# 유니버스 필터 (jongga보다 완화)
_MIN_CHANGE   = 3.0
_MAX_CHANGE   = 25.0
_MIN_PRICE    = 500
_MAX_PRICE    = 500_000
_MIN_TV       = 5_000_000_000    # 50억 (jongga는 500억)
_TOP_N_MARKET = 40               # 마켓별 수집 종목


# ── 데이터 클래스 ──────────────────────────────────────────────────

@dataclass
class _Candidate:
    code: str
    name: str
    market: str
    price: float
    change_pct: float
    trading_value: float


@dataclass
class NarrativeSignal:
    ticker: str
    name: str
    market: str
    score: int              # narrative_score (0-10)
    theme: str              # 대표 테마
    news_sentiment: float   # -1.0 ~ +1.0
    sns_momentum: int       # 0-100 (거래대금 프록시)
    narrative_score: int    # score와 동일 (프론트 호환)
    signal_date: str
    # 상세
    news_pts: int
    theme_pts: int
    vol_pts: int
    all_themes: list[str]
    news_reason: str
    llm_source: str         # 'gemini' | 'keyword_fallback'
    price: float
    change_pct: float
    theme_peers: int        # 같은 테마 보유 종목 수


# ── 유니버스 수집 ──────────────────────────────────────────────────

def _to_int(v) -> int:
    try:
        return int(str(v).replace(',', ''))
    except (ValueError, TypeError):
        return 0

def _to_float(v) -> float:
    try:
        return float(str(v).replace(',', ''))
    except (ValueError, TypeError):
        return 0.0

def _is_excluded(name: str) -> bool:
    if any(kw in name for kw in _EXCLUDE_KW):
        return True
    return name.endswith('우') or name.endswith('우B')


def _fetch_gainers(market: str) -> list[_Candidate]:
    url = (
        f'https://m.stock.naver.com/api/stocks/up/{market}'
        f'?page=1&pageSize={_TOP_N_MARKET}'
    )
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f'    [경고] {market} 수집 실패: {e}')
        return []

    out: list[_Candidate] = []
    for s in data.get('stocks', []):
        name = s.get('stockName', '')
        if _is_excluded(name):
            continue
        price = _to_int(s.get('closePrice', 0))
        if not (_MIN_PRICE <= price <= _MAX_PRICE):
            continue
        chg = _to_float(s.get('fluctuationsRatio', 0))
        if not (_MIN_CHANGE <= chg <= _MAX_CHANGE):
            continue
        tv = _to_int(s.get('accumulatedTradingValue', 0)) * 1_000_000
        if tv < _MIN_TV:
            continue
        out.append(_Candidate(
            code=s['itemCode'],
            name=name,
            market=market.upper(),
            price=float(price),
            change_pct=chg,
            trading_value=float(tv),
        ))
    return out


def build_universe() -> list[_Candidate]:
    seen: set[str] = set()
    result: list[_Candidate] = []
    for market in _MARKETS:
        for c in _fetch_gainers(market):
            if c.code not in seen:
                seen.add(c.code)
                result.append(c)
        time.sleep(0.2)
    print(f'  [유니버스] {len(result)}개 후보')
    return result


# ── 뉴스 수집 (동기, 순차) ────────────────────────────────────────

def collect_all_news(universe: list[_Candidate]) -> dict[str, list]:
    """종목별 뉴스 수집 (title + summary). sleep 포함."""
    news_map: dict[str, list] = {}
    total = len(universe)
    for i, c in enumerate(universe, 1):
        print(f'  [{i:>3}/{total}] {c.name}({c.code}) 뉴스 수집... ', end='', flush=True)
        try:
            items = get_stock_news(c.code, c.name, limit=2)
            news_map[c.code] = [
                {'title': n.title, 'summary': n.summary}
                for n in items
            ]
            print(f'{len(items)}건')
        except Exception as e:
            print(f'실패({e})')
            news_map[c.code] = []
        # Naver 요청 제한 준수
        time.sleep(0.5)
    return news_map


# ── LLM 배치 분석 (비동기) ────────────────────────────────────────

async def _analyze_one(
    analyzer: GeminiAnalyzer,
    code: str,
    name: str,
    news_items: list,
    sem: asyncio.Semaphore,
) -> tuple[str, dict]:
    """단일 종목 LLM 분석. 세마포어로 동시 요청 제한."""
    async with sem:
        result = await analyzer.analyze_news(name, news_items)
        return code, result


async def run_llm_batch(
    universe: list[_Candidate],
    news_map: dict[str, list],
    max_concurrent: int = 5,
) -> dict[str, dict]:
    """
    모든 종목에 대해 Gemini LLM을 병렬 실행.
    뉴스가 없는 종목은 건너뜀.
    """
    # Gemini rate-limit 방지 (순차 처리 + 3초 대기)
    analyzer = GeminiAnalyzer()
    sem      = asyncio.Semaphore(1)

    tasks = [
        _analyze_one(analyzer, c.code, c.name, news_map.get(c.code, []), sem)
        for c in universe
        if news_map.get(c.code)   # 뉴스 있는 종목만
    ]

    print(f'\n  [LLM] {len(tasks)}개 종목 배치 분석 중...')
    results = await asyncio.gather(*tasks, return_exceptions=True)

    llm_map: dict[str, dict] = {}
    for r in results:
        if isinstance(r, Exception):
            print(f'  [LLM 에러] {r}')
            continue
        code, res = r
        llm_map[code] = res
        score  = res.get('score', 0)
        source = res.get('source', '?')
        themes = res.get('themes', [])
        print(f'    {code}  score={score}  themes={themes}  ({source})')

    return llm_map


# ── 테마 군집화 ────────────────────────────────────────────────────

def cluster_themes(llm_map: dict[str, dict]) -> dict[str, int]:
    """
    테마별 보유 종목 수를 계산한다.
    Returns: {테마명: 종목수}
    """
    theme_cnt: dict[str, int] = {}
    for res in llm_map.values():
        for t in res.get('themes', []):
            theme_cnt[t] = theme_cnt.get(t, 0) + 1
    return theme_cnt


# ── 스코어링 ──────────────────────────────────────────────────────

_LLM_TO_NEWS_PTS = {0: 0, 1: 2, 2: 3, 3: 4}

_SENTIMENT_MAP = {0: -0.30, 1: 0.20, 2: 0.50, 3: 0.80}


def _calc_vol_pts(tv: float) -> tuple[int, int]:
    """
    거래대금 → vol_pts(0-2), sns_momentum(0-100).
    """
    if tv >= 1_000_000_000_000:   # 1조+
        return 2, 95
    if tv >= 500_000_000_000:     # 5000억+
        return 2, 80
    if tv >= 200_000_000_000:     # 2000억+
        return 2, 65
    if tv >= 100_000_000_000:     # 1000억+
        return 1, 50
    if tv >= 50_000_000_000:      # 500억+
        return 1, 35
    if tv >= 10_000_000_000:      # 100억+
        return 0, 20
    return 0, 10


def score_stock(
    cand: _Candidate,
    llm_result: dict,
    theme_cnt: dict[str, int],
    today_str: str,
) -> NarrativeSignal:
    """단일 종목 NarrativeSignal 계산."""
    raw_score = llm_result.get('score', 0)
    themes    = llm_result.get('themes', []) or []
    reason    = llm_result.get('reason', '') or ''
    source    = llm_result.get('source', 'unknown')

    # 점수 계산
    news_pts = _LLM_TO_NEWS_PTS.get(int(raw_score), 0)

    # 테마 강도: 가장 강한 테마의 보유 종목 수
    if themes:
        top_peers = max(theme_cnt.get(t, 0) for t in themes)
        top_theme = max(themes, key=lambda t: theme_cnt.get(t, 0))
    else:
        top_peers = 0
        top_theme = '기타'
    theme_pts = min(top_peers, 4)   # 최대 4점

    vol_pts, sns_mom = _calc_vol_pts(cand.trading_value)

    total = news_pts + theme_pts + vol_pts

    sentiment = _SENTIMENT_MAP.get(int(raw_score), 0.0)

    return NarrativeSignal(
        ticker=cand.code,
        name=cand.name,
        market=cand.market,
        score=total,
        theme=top_theme,
        news_sentiment=sentiment,
        sns_momentum=sns_mom,
        narrative_score=total,
        signal_date=today_str,
        news_pts=news_pts,
        theme_pts=theme_pts,
        vol_pts=vol_pts,
        all_themes=themes,
        news_reason=reason[:120],
        llm_source=source,
        price=cand.price,
        change_pct=cand.change_pct,
        theme_peers=top_peers,
    )


# ── 메인 파이프라인 ────────────────────────────────────────────────

async def run_async(min_news_score: int = 1) -> dict:
    """
    테마 모멘텀 엔진 비동기 메인 파이프라인.

    Args:
        min_news_score: 결과 포함 최소 LLM 점수 (0=모두, 1=뉴스있는것만, ...)
    """
    today_str = date.today().isoformat()
    t0 = time.time()

    print('=' * 62)
    print(f'  테마 모멘텀 엔진  |  {today_str}')
    print('=' * 62)

    # 1. 유니버스
    print('\n[1] 유니버스 수집 중...')
    universe = build_universe()

    # 2. 뉴스 수집 (동기)
    print(f'\n[2] 뉴스 수집 ({len(universe)}개)...')
    news_map = collect_all_news(universe)
    has_news = sum(1 for v in news_map.values() if v)
    print(f'    뉴스 보유: {has_news}/{len(universe)}개')

    # 3. LLM 분석 (비동기 배치)
    llm_map = await run_llm_batch(universe, news_map)

    # 4. 테마 군집화
    theme_cnt = cluster_themes(llm_map)
    print(f'\n  [테마 군집] {dict(sorted(theme_cnt.items(), key=lambda x: -x[1]))}')

    # 5. 스코어링
    print('\n[5] 스코어링...')
    signals: list[NarrativeSignal] = []
    for cand in universe:
        llm_res = llm_map.get(cand.code)
        if llm_res is None:
            continue
        if llm_res.get('score', 0) < min_news_score:
            continue
        sig = score_stock(cand, llm_res, theme_cnt, today_str)
        signals.append(sig)

    # 6. 정렬 (narrative_score → news_pts → theme_pts)
    signals.sort(key=lambda s: (-s.score, -s.news_pts, -s.theme_pts))

    # 7. 통계
    cnt      = len(signals)
    avg_s    = round(sum(s.score for s in signals) / cnt, 1) if signals else 0.0
    avg_sent = round(sum(s.news_sentiment for s in signals) / cnt, 2) if signals else 0.0
    top_theme = max(theme_cnt, key=lambda k: theme_cnt[k]) if theme_cnt else '--'
    elapsed  = round(time.time() - t0, 1)

    # 결과 출력
    print(f'\n{"=" * 62}')
    print(f'  완료: {cnt}개  Top테마: {top_theme}({theme_cnt.get(top_theme,0)}종목)  '
          f'평균감성: {avg_sent:+.2f}  {elapsed}s')
    print(f'{"=" * 62}')
    print(f'\n  {"#":>3}  {"종목명":<14}  {"테마":<12}  {"점수":>4}  '
          f'{"N":>2}  {"T":>2}  {"V":>2}  {"감성":>6}  peers')
    print(f'  {"─" * 60}')
    for rank, s in enumerate(signals[:15], 1):
        print(
            f'  {rank:>3}  {s.name:<14}  {s.theme:<12}  {s.score:>4}  '
            f'{s.news_pts:>2}  {s.theme_pts:>2}  {s.vol_pts:>2}  '
            f'{s.news_sentiment:>+.2f}  {s.theme_peers}'
        )

    return {
        'date': today_str,
        'signals': [asdict(s) for s in signals],
        'stats': {
            'total':         cnt,
            'avg_score':     avg_s,
            'top_theme':     top_theme,
            'avg_sentiment': avg_sent,
            'theme_map':     theme_cnt,
        },
        'processing_time_s': elapsed,
        'updated_at': datetime.now().isoformat(),
    }


def run(min_news_score: int = 1) -> dict:
    """동기 래퍼 (run script에서 호출)."""
    return asyncio.run(run_async(min_news_score))


def save_results(result: dict) -> str:
    os.makedirs(_DATA_DIR, exist_ok=True)
    path = os.path.join(_DATA_DIR, 'narrative_momentum_latest.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    date_str = result.get('date', '').replace('-', '')
    if len(date_str) == 8:
        dated_path = os.path.join(_DATA_DIR, f'narrative_momentum_{date_str}.json')
        with open(dated_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'\n  => {path} 저장 완료 ({result["stats"]["total"]}개 시그널)')
    return path
