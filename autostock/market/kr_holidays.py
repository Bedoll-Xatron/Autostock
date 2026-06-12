"""
한국 공휴일 조회 — data.go.kr 한국천문연구원 특일정보 API
임시공휴일·선거일 포함 (공식 정부 데이터).

캐시 전략:
  1순위: 메모리 캐시 (_HOLIDAY_CACHE)
  2순위: 디스크 캐시 (logs/holidays_{year}.json) — 재시작 시에도 유지
  3순위: API 조회 (HOLIDAY_API_KEY 필요)
  4순위: 정적 fallback (2025~2027 주요 공휴일 하드코딩)
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Optional

import pytz

from autostock.logger import get_logger

log = get_logger(__name__)

KST = pytz.timezone("Asia/Seoul")
MARKET_OPEN  = dtime(9, 0)
MARKET_CLOSE = dtime(20, 0)
_HOLIDAY_CACHE: dict[int, set[date]] = {}
_CACHE_DIR = Path(__file__).parent.parent.parent / "logs"
_API_URL = "http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"

# ── 정적 fallback (2025~2027) ─────────────────────────────────────
_STATIC: set[date] = {
    # 2025
    date(2025, 1, 1),
    date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30),
    date(2025, 3, 1),
    date(2025, 5, 5), date(2025, 5, 6),
    date(2025, 6, 6),
    date(2025, 8, 15),
    date(2025, 10, 3), date(2025, 10, 5), date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 9),
    date(2025, 12, 25),
    # 2026
    date(2026, 1, 1),
    date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19),
    date(2026, 3, 1),
    date(2026, 5, 5),
    date(2026, 6, 6),
    date(2026, 8, 15),
    date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 26),
    date(2026, 10, 3),
    date(2026, 10, 9),
    date(2026, 12, 25),
    # 2027
    date(2027, 1, 1),
    date(2027, 2, 6), date(2027, 2, 7), date(2027, 2, 8),
    date(2027, 3, 1),
    date(2027, 5, 5),
    date(2027, 6, 6),
    date(2027, 8, 15),
    date(2027, 10, 3),
    date(2027, 10, 9),
    date(2027, 10, 14), date(2027, 10, 15), date(2027, 10, 16),
    date(2027, 12, 25),
}


def _api_key() -> str:
    return os.environ.get("HOLIDAY_API_KEY", "").strip()


def _cache_path(year: int) -> Path:
    return _CACHE_DIR / f"holidays_{year}.json"


def _load_disk_cache(year: int) -> Optional[set[date]]:
    path = _cache_path(year)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {date.fromisoformat(d) for d in data}
    except Exception:
        return None


def _save_disk_cache(year: int, holidays: set[date]) -> None:
    try:
        _CACHE_DIR.mkdir(exist_ok=True)
        _cache_path(year).write_text(
            json.dumps(sorted(d.isoformat() for d in holidays), ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("공휴일 캐시 저장 실패: %s", e)


def _fetch_month(year: int, month: int) -> list[date]:
    """data.go.kr API — 해당 월의 공휴일 반환."""
    key = _api_key()
    if not key:
        return []
    params = urllib.parse.urlencode({
        "serviceKey": key,
        "solYear":    str(year),
        "solMonth":   f"{month:02d}",
        "_type":      "json",
        "numOfRows":  "20",
    })
    try:
        with urllib.request.urlopen(f"{_API_URL}?{params}", timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        items = (
            body.get("response", {})
                .get("body", {})
                .get("items", {})
                .get("item", [])
        )
        if isinstance(items, dict):
            items = [items]
        result = []
        for item in items:
            loc = str(item.get("locdate", ""))
            if len(loc) == 8:
                try:
                    result.append(date(int(loc[:4]), int(loc[4:6]), int(loc[6:])))
                except ValueError:
                    pass
        return result
    except Exception as e:
        log.warning("공휴일 API 실패 (%d-%02d): %s", year, month, e)
        return []


def _fetch_year(year: int) -> set[date]:
    all_holidays: set[date] = set()
    for month in range(1, 13):
        all_holidays.update(_fetch_month(year, month))
    return all_holidays


def get_holidays(year: int) -> set[date]:
    """연도별 공휴일 set (메모리 → 디스크 → API → static fallback)."""
    if year in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[year]

    from_disk = _load_disk_cache(year)
    if from_disk is not None:
        _HOLIDAY_CACHE[year] = from_disk
        log.debug("공휴일 디스크 캐시 로드: %d년 %d건", year, len(from_disk))
        return from_disk

    log.info("공휴일 API 조회: %d년", year)
    fetched = _fetch_year(year)
    if fetched:
        _HOLIDAY_CACHE[year] = fetched
        _save_disk_cache(year, fetched)
        log.info("공휴일 API 완료: %d년 %d건", year, len(fetched))
        return fetched

    static = {d for d in _STATIC if d.year == year}
    log.warning("공휴일 API 실패 — 정적 목록 사용: %d년 %d건", year, len(static))
    _HOLIDAY_CACHE[year] = static
    return static


def is_holiday(d: Optional[date] = None) -> bool:
    """주어진 날짜(기본: 오늘 KST)가 공휴일인지 반환."""
    if d is None:
        d = datetime.now(KST).date()
    return d in get_holidays(d.year)


def is_trading_day(d: Optional[date] = None) -> bool:
    """주어진 날짜(기본: 오늘 KST)가 거래일(평일 + 공휴일 아님)인지 반환."""
    if d is None:
        d = datetime.now(KST).date()
    if d.weekday() >= 5:
        return False
    return not is_holiday(d)


def is_market_hours() -> bool:
    """현재 KST 시각이 주식 거래 시간(09:00~15:20)이고 거래일인지 반환."""
    if not is_trading_day():
        return False
    now_t = datetime.now(KST).time().replace(second=0, microsecond=0)
    return MARKET_OPEN <= now_t <= MARKET_CLOSE


def refresh_cache(year: int) -> int:
    """강제 API 재조회 후 캐시 갱신. 갱신된 공휴일 수 반환."""
    _HOLIDAY_CACHE.pop(year, None)
    p = _cache_path(year)
    if p.exists():
        p.unlink()
    return len(get_holidays(year))
