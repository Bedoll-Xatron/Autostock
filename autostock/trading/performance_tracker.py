"""과거 BUY 결정 성과 추적 — pykrx로 d5/d10 수익률 계산 후 JSON 저장."""
import json
from datetime import date, timedelta
from pathlib import Path

from autostock.db import supabase as db
from autostock.logger import get_logger

log = get_logger(__name__)

OUTPUT_PATH = Path(__file__).parent.parent.parent / "data" / "performance_log.json"


def run_performance_tracker() -> list[dict]:
    """
    최근 25일 내 BUY+체결 결정에 대해 d5/d10 수익률 계산.

    Returns:
        결과 목록 (파일에도 저장)
    """
    try:
        from pykrx import stock as pykrx_stock
    except ImportError:
        log.warning("pykrx 미설치 — 성과 추적 건너뜀")
        return []

    today = date.today()
    since = (today - timedelta(days=25)).isoformat()

    decisions = db.fetch_decisions_since(since)

    # ticker+date 기준 중복 제거 — order_qty 가장 큰 항목 우선
    _seen: dict[tuple, dict] = {}
    for d in decisions:
        if d.get("action") != "BUY" or (d.get("order_qty") or 0) <= 0:
            continue
        key = (d.get("ticker", ""), (d.get("date") or d.get("created_at", ""))[:10])
        if key not in _seen or (d.get("order_qty") or 0) > (_seen[key].get("order_qty") or 0):
            _seen[key] = d
    buy_decisions = list(_seen.values())

    if not buy_decisions:
        log.info("추적할 BUY 결정 없음")
        return []

    end_str = today.strftime("%Y%m%d")
    results: list[dict] = []

    for d in buy_decisions:
        ticker = d.get("ticker", "")
        entry_price = float(d.get("price_reference") or 0)
        decision_date_str: str = (d.get("date") or d.get("created_at", ""))[:10]

        if not ticker or entry_price <= 0 or not decision_date_str:
            continue

        try:
            dec_date = date.fromisoformat(decision_date_str)
        except ValueError:
            continue

        start_str = dec_date.strftime("%Y%m%d")
        try:
            df = pykrx_stock.get_market_ohlcv_by_date(start_str, end_str, ticker)
        except Exception as e:
            log.debug("pykrx 조회 실패 %s: %s", ticker, e)
            continue

        if df is None or len(df) < 2:
            continue

        closes = df["종가"].values.astype(float)
        row: dict = {
            "ticker": ticker,
            "name": d.get("name", ""),
            "date": decision_date_str,
            "entry_price": entry_price,
        }

        for days, key in [(5, "d5_return"), (10, "d10_return")]:
            if len(closes) > days:
                row[key] = round((closes[days] - entry_price) / entry_price * 100, 2)

        results.append(row)
        log.debug("성과 추적: %s %s", ticker, {k: v for k, v in row.items() if "return" in k})

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    log.info("성과 추적 완료: %d건 → %s", len(results), OUTPUT_PATH)
    return results
