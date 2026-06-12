"""08:00 KST — 시간외 호가로 워치리스트 프리마켓 갭업 종목 당일 제외 (W6)."""
import os

from autostock.db import supabase as db
from autostock.trading.kis_client import get_premarket_price
from autostock.hitl import telegram_bot as bot_ui
from autostock.logger import get_logger

log = get_logger(__name__)

WATCHLIST_GAP_FILTER_PCT: float = 5.0  # 시간외 +5% 이상이면 당일 제외


async def refresh_watchlist_premarket() -> None:
    """시간외단일가 기준 갭업 종목을 당일 스킵 마킹.

    PREMARKET_FILTER_ENABLED=false 환경변수로 비활성화 가능.
    시간외 API 실패 종목은 조용히 건너뛰어 파이프라인 진행에 영향 없음.
    """
    if os.getenv("PREMARKET_FILTER_ENABLED", "true").lower() == "false":
        log.info("프리마켓 필터 비활성화 (PREMARKET_FILTER_ENABLED=false)")
        return

    watchlist = db.fetch_watchlist()
    if not watchlist:
        log.info("프리마켓 필터: 워치리스트 비어있음")
        return

    blocked: list[tuple[str, str, float]] = []
    for w in watchlist:
        prev_close = float(w.get("prev_close") or 0)
        if prev_close <= 0:
            continue
        try:
            pre = get_premarket_price(w["ticker"])
            if pre <= 0:
                continue
            gap = (pre / prev_close - 1) * 100
            if gap >= WATCHLIST_GAP_FILTER_PCT:
                blocked.append((w["ticker"], w.get("name", w["ticker"]), gap))
                db.mark_watchlist_skip_today(w["ticker"])
        except Exception as e:
            log.warning("[%s] 프리마켓 필터 오류 — 건너뜀: %s", w["ticker"], e)

    if blocked:
        lines = "\n".join(f"  {name}({t}): {gap:+.1f}%" for t, name, gap in blocked)
        log.info("프리마켓 갭업 제외 %d종목:\n%s", len(blocked), lines)
        bot_ui.schedule_message(
            f"🌅 08:00 프리마켓 필터 — {len(blocked)}종목 당일 제외\n{lines}"
        )
    else:
        log.info("프리마켓 필터: 갭업 종목 없음 (전체 통과)")
