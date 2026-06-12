"""
daily_update.py
=================
매 거래일 장 마감(오후 4시) 이후 실행하는 일일 누적 업데이트 스크립트.

실행 순서:
  1. run_engine.py               — 종가베팅(jongga) V2 시그널 생성
  2. vcp_scanner.py              — VCP 패턴 스캔 (날짜별 누적 저장)
  3. run_flow_momentum.py        — 수급 모멘텀
  4. run_narrative_momentum.py   — 테마/내러티브 모멘텀
  5. run_sector_rotation.py      — 섹터 로테이션
  6. run_contrarian_reversal.py  — 역발상 반전
  7. build_daily_prices.py       — daily_prices.csv 누적 업데이트

사용법:
  python daily_update.py                 # 거래일 자동 체크 후 실행
  python daily_update.py --force         # 주말/휴일 무시하고 강제 실행
  python daily_update.py --no-telegram   # 텔레그램 알림 비활성화
  python daily_update.py --only jongga vcp  # 특정 엔진만 실행
"""

import argparse
import logging
import os
import subprocess
import sys
from datetime import date, datetime

# ── 경로 ─────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.join(BASE_DIR, 'engine')
LOG_DIR    = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# ── 로깅 ─────────────────────────────────────────────────────────
log_file = os.path.join(LOG_DIR, f"daily_update_{date.today().strftime('%Y%m%d')}.log")
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.stream = open(sys.stdout.fileno(), mode='w', encoding='utf-8', errors='replace', closefd=False)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        _stream_handler,
    ],
)
log = logging.getLogger(__name__)

# ── 한국 공휴일 ───────────────────────────────────────────────────
try:
    import holidays as _holidays_lib
    _HOLIDAYS_PKG = True
except ImportError:
    _holidays_lib = None  # type: ignore[assignment]
    _HOLIDAYS_PKG = False
    log.warning("holidays 패키지 미설치 — 하드코딩 공휴일만 사용 (pip install holidays)")

# holidays 패키지 없을 때 사용하는 정적 폴백 (2026–2027)
_STATIC_KR_HOLIDAYS: frozenset[date] = frozenset({
    # 2026
    date(2026, 1,  1), date(2026, 1, 28), date(2026, 1, 29), date(2026, 1, 30),
    date(2026, 3,  1), date(2026, 3,  2), date(2026, 5,  5), date(2026, 5, 25),
    date(2026, 6,  6), date(2026, 8, 15), date(2026, 9, 24), date(2026, 9, 25),
    date(2026, 9, 26), date(2026, 10, 3), date(2026, 10, 5), date(2026, 10, 9),
    date(2026, 12, 25),
    # 2027
    date(2027, 1,  1), date(2027, 2, 16), date(2027, 2, 17), date(2027, 2, 18),
    date(2027, 3,  1), date(2027, 5,  5), date(2027, 5, 13), date(2027, 6,  6),
    date(2027, 8, 15), date(2027, 10, 3), date(2027, 10, 9), date(2027, 12, 25),
})

_holiday_cache: dict[int, set[date]] = {}


def _get_kr_holidays(year: int) -> set[date]:
    """해당 연도의 한국 공휴일 집합을 반환. 결과를 캐시한다."""
    if year in _holiday_cache:
        return _holiday_cache[year]
    if _HOLIDAYS_PKG:
        hl: set[date] = set(_holidays_lib.KR(years=year).keys())
    else:
        hl = {d for d in _STATIC_KR_HOLIDAYS if d.year == year}
        if not hl:
            log.warning("연도 %d 공휴일 데이터 없음 — holidays 패키지 설치 권장", year)
    _holiday_cache[year] = hl
    return hl


def is_trading_day(d: date | None = None) -> bool:
    """주말 및 한국 공휴일이 아닌 평일인지 확인."""
    if d is None:
        d = date.today()
    if d.weekday() >= 5:      # 토(5), 일(6)
        return False
    return d not in _get_kr_holidays(d.year)


def run_script(name: str, script: str, extra_args: list[str]) -> bool:
    """엔진 스크립트를 ENGINE_DIR 에서 실행하고 성공 여부를 반환."""
    script_path = os.path.join(ENGINE_DIR, script)
    if not os.path.exists(script_path):
        log.error(f"  스크립트 없음: {script_path}")
        return False

    cmd = [sys.executable, script_path] + extra_args
    log.info(f"  $ {' '.join(os.path.basename(c) for c in cmd)}")

    # Windows cp949 환경에서 이모지/한글 print 오류 방지
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'

    try:
        proc = subprocess.run(
            cmd,
            cwd=ENGINE_DIR,           # 상대 import가 동작하도록 engine/ 에서 실행
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=600,
            env=env,
        )
        for line in (proc.stdout or '').strip().splitlines():
            log.info(f"    {line}")
        for line in (proc.stderr or '').strip().splitlines():
            log.warning(f"    [stderr] {line}")
        if proc.returncode != 0:
            log.error(f"  → 실패 (exit={proc.returncode})")
            return False
        log.info(f"  → 완료")
        return True
    except subprocess.TimeoutExpired:
        log.error(f"  → 타임아웃 (600초 초과)")
        return False
    except Exception as e:
        log.error(f"  → 예외 발생: {e}")
        return False


# ── 실행할 단계 목록 ──────────────────────────────────────────────
# (alias, 표시명, 스크립트파일, 추가인수 플래그키)
STEPS = [
    ("jongga",     "종가베팅 V2",          "run_engine.py",               "telegram"),
    ("vcp",        "VCP 패턴 스캐너",       "vcp_scanner.py",              None),
    ("flow",       "수급 모멘텀",           "run_flow_momentum.py",        "telegram"),
    ("narrative",  "테마 모멘텀",           "run_narrative_momentum.py",   "telegram"),
    ("sector",     "섹터 로테이션",         "run_sector_rotation.py",      "telegram"),
    ("contrarian", "역발상 반전",           "run_contrarian_reversal.py",  "telegram"),
    ("prices",     "daily_prices 업데이트", "build_daily_prices.py",       None),
]


def main() -> None:
    parser = argparse.ArgumentParser(description='일일 시장 데이터 자동 업데이트')
    parser.add_argument('--force',       action='store_true',
                        help='거래일 여부를 무시하고 강제 실행')
    parser.add_argument('--no-telegram', action='store_true',
                        help='텔레그램 알림 비활성화')
    parser.add_argument('--only',        nargs='+', metavar='ALIAS',
                        help=f"지정한 엔진만 실행 (alias: {', '.join(s[0] for s in STEPS)})")
    args = parser.parse_args()

    today = date.today()
    log.info('=' * 60)
    log.info(f"일일 업데이트 시작: {today}  {datetime.now().strftime('%H:%M:%S')}")
    log.info('=' * 60)

    # ── 거래일 체크 ───────────────────────────────────────────────
    if not args.force and not is_trading_day(today):
        day_names = ['월', '화', '수', '목', '금', '토', '일']
        reason = '공휴일' if today in _get_kr_holidays(today.year) else f"주말({day_names[today.weekday()]}요일)"
        log.info(f"오늘({today})은 {reason}입니다. 업데이트를 건너뜁니다.")
        log.info("강제 실행: python daily_update.py --force")
        return

    # ── 실행 대상 필터 ────────────────────────────────────────────
    only = set(args.only) if args.only else None
    steps_to_run = [s for s in STEPS if only is None or s[0] in only]
    if only:
        unknown = only - {s[0] for s in STEPS}
        if unknown:
            log.warning(f"알 수 없는 alias: {', '.join(unknown)}")

    # ── 각 엔진 순차 실행 ─────────────────────────────────────────
    results: list[tuple[str, bool]] = []
    for alias, display_name, script, telegram_flag in steps_to_run:
        log.info(f"\n[{display_name}]")
        extra: list[str] = []
        if telegram_flag == 'telegram' and args.no_telegram:
            extra.append('--no-telegram')
        ok = run_script(display_name, script, extra)
        results.append((display_name, ok))

    # ── Supabase 동기화 ───────────────────────────────────────────
    sys.path.insert(0, os.path.dirname(BASE_DIR))  # project root

    # 1) 전략 결과 → mf_* 테이블
    try:
        from marketflow.engine.supabase_sync import sync_all
        sync_results = sync_all()
        total_rows = sum(sync_results.values())
        log.info(f"[Supabase] mf_* 동기화: 총 {total_rows} rows")
    except Exception as e:
        log.error(f"[Supabase] mf_* 동기화 실패: {e}")

    # 2) 시그널 종목 → watch_list
    try:
        from autostock.scripts.import_marketflow import import_to_watchlist
        wl_count = import_to_watchlist()
        log.info(f"[Supabase] watch_list 갱신: {wl_count}개")
    except Exception as e:
        log.error(f"[Supabase] watch_list 갱신 실패: {e}")

    # ── 최종 요약 ─────────────────────────────────────────────────
    log.info(f"\n{'=' * 60}")
    log.info(f"업데이트 완료 요약 ({today})")
    log.info('-' * 60)
    success_count = sum(1 for _, ok in results if ok)
    for display_name, ok in results:
        mark = '✓' if ok else '✗'
        log.info(f"  {mark}  {display_name}")
    log.info('-' * 60)
    log.info(f"성공: {success_count}/{len(results)}")
    log.info('=' * 60)


if __name__ == '__main__':
    main()
