"""포트폴리오 누적 손실 서킷 브레이커 (W5)."""
import os
from datetime import date, timedelta

from autostock.db import supabase as db
from autostock.logger import get_logger

log = get_logger(__name__)

CIRCUIT_DRAWDOWN_PCT = -5.0     # 최근 5일 평균 -5% 이하면 발동
CIRCUIT_LOOKBACK_DAYS = 5
CIRCUIT_PAUSE_DAYS = 7          # 발동 시 일주일 매수 중단


def _fetch_realtime_pnl(lookback_days: int) -> list[float]:
    """최근 lookback_days 내 체결 BUY 결정의 현재 수익률 목록 (pykrx 기반).

    pykrx 미설치 또는 조회 실패 시 빈 리스트 반환 → 호출부에서 fallback 처리.
    """
    try:
        from pykrx import stock as pykrx_stock
    except ImportError:
        log.debug("pykrx 미설치 — 실시간 PnL 계산 건너뜀")
        return []

    end = date.today()
    start = end - timedelta(days=lookback_days)

    decisions = db.fetch_executed_buys_between(start.isoformat(), end.isoformat())
    if not decisions:
        return []

    today_str = end.strftime("%Y%m%d")
    pnl_list: list[float] = []

    for d in decisions:
        ticker = d.get("ticker", "")
        entry_price = float(d.get("price_reference") or 0)
        if not ticker or entry_price <= 0:
            continue

        dec_date_str = (d.get("date") or "")[:10]
        try:
            dec_date = date.fromisoformat(dec_date_str)
        except ValueError:
            continue

        start_str = dec_date.strftime("%Y%m%d")
        try:
            df = pykrx_stock.get_market_ohlcv_by_date(start_str, today_str, ticker)
            if df is not None and not df.empty:
                cur_price = float(df["종가"].iloc[-1])
                if cur_price > 0:
                    pnl = (cur_price / entry_price - 1) * 100
                    pnl_list.append(pnl)
                    log.debug("[%s] 실시간 PnL: 진입가=%.0f 현재가=%.0f (%.2f%%)",
                              ticker, entry_price, cur_price, pnl)
        except Exception as e:
            log.debug("[%s] pykrx 조회 실패 — 건너뜀: %s", ticker, e)

    return pnl_list


def check_circuit_breaker() -> tuple[bool, str]:
    """
    Returns:
        (paused, reason): paused=True면 신규 매수 중단
    """
    if os.getenv("CIRCUIT_BREAKER_ENABLED", "true").lower() == "false":
        return False, "disabled"

    end = date.today()

    # 최근 발동 후 PAUSE_DAYS 미경과 시 즉시 반환 (불필요한 조회 방지)
    last_trip = db.fetch_last_circuit_trip()
    if last_trip:
        days_since = (end - date.fromisoformat(last_trip)).days
        if days_since < CIRCUIT_PAUSE_DAYS:
            return True, f"서킷 일시정지 ({CIRCUIT_PAUSE_DAYS - days_since}일 남음)"

    # 실시간 PnL 계산 (pykrx — d5 지연 없음)
    pnl_values = _fetch_realtime_pnl(CIRCUIT_LOOKBACK_DAYS)

    # fallback: performance_log.json d5_return (pykrx 불가 시)
    if not pnl_values:
        start = end - timedelta(days=CIRCUIT_LOOKBACK_DAYS)
        rows = db.fetch_pnl_between(start.isoformat(), end.isoformat())
        if not rows:
            return False, "no_data"
        pnl_values = [r["pnl_pct"] for r in rows]
        log.debug("서킷 브레이커: d5_return fallback 사용 (%d건)", len(pnl_values))

    avg_pnl_pct = sum(pnl_values) / len(pnl_values)
    log.debug("서킷 브레이커: 최근 %d일 평균 PnL=%.2f%% (종목 %d건)",
              CIRCUIT_LOOKBACK_DAYS, avg_pnl_pct, len(pnl_values))

    if avg_pnl_pct <= CIRCUIT_DRAWDOWN_PCT:
        log.warning(
            "서킷 브레이커 발동: %d일 평균 %.2f%% <= %.1f%%",
            CIRCUIT_LOOKBACK_DAYS, avg_pnl_pct, CIRCUIT_DRAWDOWN_PCT,
        )
        db.save_circuit_trip(end.isoformat())
        return True, f"평균 손익 {avg_pnl_pct:+.1f}% (최근 {CIRCUIT_LOOKBACK_DAYS}일, {len(pnl_values)}건)"

    return False, "ok"
