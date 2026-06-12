"""일일 KPI 리포트 — 17:30 KST Telegram 발송."""
from datetime import datetime

import pytz

from autostock.db import supabase as db
from autostock.hitl import telegram_bot as bot_ui
from autostock.logger import get_logger

log = get_logger(__name__)

KST = pytz.timezone("Asia/Seoul")


async def run_daily_kpi_report() -> None:
    """오늘 매매 KPI를 집계해 Telegram으로 발송."""
    today = datetime.now(KST).date().isoformat()
    rows = db.fetch_decisions_by_date(today)
    if not rows:
        log.info("KPI 리포트: 오늘(%s) 결정 없음", today)
        return

    buy_rows = [r for r in rows if r.get("action") == "BUY" and (r.get("order_qty") or 0) > 0]
    blocked = [r for r in rows if r.get("blocked_reason")]
    sold_rows = db.fetch_sells_by_date(today)
    stop_loss_rows = [r for r in sold_rows if r.get("reason") == "손절"]

    avg_gap = (
        sum(r.get("gap_pct") or 0 for r in buy_rows) / len(buy_rows)
        if buy_rows else 0
    )
    avg_slip = (
        sum(r.get("entry_slippage_pct") or 0 for r in buy_rows) / len(buy_rows)
        if buy_rows else 0
    )
    stop_rate = (
        len(stop_loss_rows) / len(buy_rows) * 100
        if buy_rows else 0
    )

    blocked_detail = ""
    if blocked:
        names = ", ".join(r.get("name") or r.get("ticker", "?") for r in blocked)
        blocked_detail = f"\n차단 종목: {names}"

    gate_blocks = [r for r in rows if (r.get("blocked_reason") or "").startswith("BLOCK_")]
    gate_block_detail = ""
    if gate_blocks:
        by_reason: dict[str, int] = {}
        for r in gate_blocks:
            key = r["blocked_reason"]
            by_reason[key] = by_reason.get(key, 0) + 1
        lines = "\n".join(f"  - {k}: {v}건" for k, v in by_reason.items())
        gate_block_detail = f"\n게이트 차단:\n{lines}"

    stop_pcts = [float(r["stop_pct"]) for r in buy_rows if r.get("stop_pct")]
    stop_pct_line = ""
    if stop_pcts:
        stop_pct_line = (
            f"\n손절폭: {min(stop_pcts):.1f}% ~ {max(stop_pcts):.1f}% "
            f"(평균 {sum(stop_pcts) / len(stop_pcts):.2f}%)"
        )

    # W5: 포지션 섹터 분포 (07_KPI대시보드규격 §4)
    held = db.fetch_held_positions()
    watchlist = db.fetch_watchlist()
    w_map = {w["ticker"]: w for w in watchlist}
    position_line = ""
    if held:
        sector_counts: dict[str, int] = {}
        for h in held:
            sec = w_map.get(h["ticker"], {}).get("sector", "기타")
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
        sector_str = " / ".join(f"{s} {c}" for s, c in sector_counts.items())
        position_line = f"\n현재 보유: {len(held)}종목 ({sector_str})"

    msg = (
        f"📊 *일일 KPI 리포트* — {today}\n\n"
        f"매수 실행: {len(buy_rows)}건 (차단 {len(blocked)}건){blocked_detail}\n"
        f"손절: {len(stop_loss_rows)}/{len(buy_rows) or 1} ({stop_rate:.1f}%){stop_pct_line}\n"
        f"평균 갭업: {avg_gap:+.2f}%\n"
        f"평균 슬리피지: {avg_slip:+.2f}%"
        f"{gate_block_detail}"
        f"{position_line}"
    )
    bot_ui.schedule_message(msg)
    log.info("KPI 리포트 발송: %s", today)
