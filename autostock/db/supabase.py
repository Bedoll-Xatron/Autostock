"""Supabase 클라이언트 — watch_list, market_daily, trading_decisions."""
import json
from datetime import date
from pathlib import Path
from supabase import create_client, Client
from autostock import config
from autostock.logger import get_logger

_CIRCUIT_STATE_PATH    = Path(__file__).parent.parent.parent / "data" / "circuit_state.json"
_PERF_LOG_PATH         = Path(__file__).parent.parent.parent / "data" / "performance_log.json"
_PREMARKET_SKIP_PATH   = Path(__file__).parent.parent.parent / "data" / "premarket_skip.json"

log = get_logger(__name__)

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    return _client


# ── watch_list ───────────────────────────────────────────

def fetch_watchlist() -> list[dict]:
    """Supabase watch_list 전체 조회. 최신 신호 순으로 반환."""
    try:
        res = (
            get_client()
            .table("watch_list")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
        return res.data or []
    except Exception as e:
        log.error("fetch_watchlist failed: %s", e)
        return []


def add_to_watchlist(
    ticker: str,
    name: str,
    sector: str,
    prev_close: float,
    *,
    signal_score: float | None = None,
    strategies: str | None = None,
    strategy_count: int = 1,
    foreign_5d: int | None = None,
    inst_5d: int | None = None,
    signal_strength: str | None = None,
    theme: str | None = None,
    rotation_phase: str | None = None,
    change_pct: float | None = None,
) -> bool:
    """워치리스트에 종목 추가. 이미 존재하면 업서트."""
    row: dict = {"ticker": ticker, "name": name, "sector": sector, "prev_close": prev_close}
    if signal_score is not None:
        row["signal_score"] = signal_score
    if strategies is not None:
        row["strategies"] = strategies
    row["strategy_count"] = strategy_count
    if foreign_5d is not None:
        row["foreign_5d"] = foreign_5d
    if inst_5d is not None:
        row["inst_5d"] = inst_5d
    if signal_strength is not None:
        row["signal_strength"] = signal_strength
    if theme is not None:
        row["theme"] = theme
    if rotation_phase is not None:
        row["rotation_phase"] = rotation_phase
    if change_pct is not None:
        row["change_pct"] = change_pct
    try:
        get_client().table("watch_list").upsert(row, on_conflict="ticker").execute()
        return True
    except Exception as e:
        log.error("add_to_watchlist %s failed: %s", ticker, e)
        return False


def remove_from_watchlist(ticker: str) -> bool:
    """워치리스트에서 종목 제거."""
    try:
        get_client().table("watch_list").delete().eq("ticker", ticker).execute()
        return True
    except Exception as e:
        log.error("remove_from_watchlist %s failed: %s", ticker, e)
        return False


# ── market_daily ─────────────────────────────────────────

def fetch_latest_market_data() -> dict | None:
    """가장 최근 market_daily 레코드 조회."""
    try:
        res = (
            get_client()
            .table("market_daily")
            .select("*")
            .order("date", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as e:
        log.error("fetch_latest_market_data failed: %s", e)
        return None


def upsert_market_data(data: dict) -> bool:
    """market_daily 업서트."""
    try:
        get_client().table("market_daily").upsert(data, on_conflict="date").execute()
        return True
    except Exception as e:
        log.error("upsert_market_data failed: %s", e)
        return False


# ── trading_decisions ────────────────────────────────────

def save_decision(decision: dict) -> bool:
    """매매 결정 저장."""
    try:
        get_client().table("trading_decisions").insert({
            **decision,
            "date": date.today().isoformat(),
        }).execute()
        return True
    except Exception as e:
        log.error("save_decision failed: %s", e)
        return False


def fetch_today_buy_count() -> int:
    """오늘 이미 체결된 BUY 종목 수 조회 (일일 신규 진입 한도 연속성 유지용)."""
    try:
        res = (
            get_client()
            .table("trading_decisions")
            .select("ticker", count="exact")
            .eq("date", date.today().isoformat())
            .eq("action", "BUY")
            .gt("order_qty", 0)
            .execute()
        )
        return res.count or 0
    except Exception as e:
        log.error("fetch_today_buy_count failed: %s", e)
        return 0


def fetch_executed_buys_between(start: str, end: str) -> list[dict]:
    """start~end 기간 내 실제 체결된 BUY 결정 조회 (order_qty > 0)."""
    try:
        res = (
            get_client()
            .table("trading_decisions")
            .select("ticker, date, price_reference, open_price, entry_slippage_pct")
            .gte("date", start)
            .lte("date", end)
            .eq("action", "BUY")
            .gt("order_qty", 0)
            .execute()
        )
        return res.data or []
    except Exception as e:
        log.error("fetch_executed_buys_between failed: %s", e)
        return []


def fetch_decisions_since(since_date: str) -> list[dict]:
    """since_date(YYYY-MM-DD) 이후 trading_decisions 조회."""
    try:
        res = (
            get_client()
            .table("trading_decisions")
            .select("*")
            .gte("date", since_date)
            .order("date", desc=False)
            .execute()
        )
        return res.data or []
    except Exception as e:
        log.error("fetch_decisions_since failed: %s", e)
        return []


def fetch_decisions_by_date(date_str: str) -> list[dict]:
    """특정 날짜(YYYY-MM-DD)의 trading_decisions 전체 조회."""
    try:
        res = (
            get_client()
            .table("trading_decisions")
            .select("*")
            .eq("date", date_str)
            .execute()
        )
        return res.data or []
    except Exception as e:
        log.error("fetch_decisions_by_date failed: %s", e)
        return []


def fetch_sells_by_date(date_str: str) -> list[dict]:
    """특정 날짜의 SELL 결정 조회. trailing_stop 손절은 W2에서 추가 예정."""
    try:
        res = (
            get_client()
            .table("trading_decisions")
            .select("*")
            .eq("date", date_str)
            .eq("action", "SELL")
            .execute()
        )
        return res.data or []
    except Exception as e:
        log.error("fetch_sells_by_date failed: %s", e)
        return []


# ── held_positions (트레일링 손절 재개용) ────────────────────

def save_held_position(pos: dict) -> bool:
    """장 마감 후 보유 포지션 저장 (upsert). ticker를 PK로."""
    try:
        data = pos if "entry_date" in pos else {**pos, "entry_date": date.today().isoformat()}
        get_client().table("held_positions").upsert(data, on_conflict="ticker").execute()
        return True
    except Exception as e:
        log.error("save_held_position %s failed: %s", pos.get("ticker"), e)
        return False


def fetch_held_positions() -> list[dict]:
    """held_positions 전체 조회 (서버 재시작 시 복구용)."""
    try:
        res = get_client().table("held_positions").select("*").execute()
        return res.data or []
    except Exception as e:
        log.error("fetch_held_positions failed: %s", e)
        return []


def delete_held_position(ticker: str) -> bool:
    """손절 완료 후 해당 포지션 삭제."""
    try:
        get_client().table("held_positions").delete().eq("ticker", ticker).execute()
        return True
    except Exception as e:
        log.error("delete_held_position %s failed: %s", ticker, e)
        return False


def update_held_position(ticker: str, peak_price: float, stop_price: float, phase: str) -> bool:
    """트레일링 중 peak_price / stop_price / phase 갱신."""
    try:
        get_client().table("held_positions").update(
            {"peak_price": peak_price, "stop_price": stop_price, "phase": phase}
        ).eq("ticker", ticker).execute()
        return True
    except Exception as e:
        log.error("update_held_position %s failed: %s", ticker, e)
        return False


# ── 서킷 브레이커 (W5) ──────────────────────────────────

def fetch_pnl_between(start: str, end: str) -> list[dict]:
    """performance_log.json에서 날짜 범위의 d5 수익률 조회."""
    if not _PERF_LOG_PATH.exists():
        return []
    try:
        rows = json.loads(_PERF_LOG_PATH.read_text(encoding="utf-8"))
        return [
            {"pnl_pct": r["d5_return"]}
            for r in rows
            if r.get("date") and start <= r["date"] <= end and r.get("d5_return") is not None
        ]
    except Exception as e:
        log.error("fetch_pnl_between failed: %s", e)
        return []


def fetch_last_circuit_trip() -> str | None:
    """서킷 브레이커 마지막 발동 날짜 조회 (YYYY-MM-DD)."""
    if not _CIRCUIT_STATE_PATH.exists():
        return None
    try:
        state = json.loads(_CIRCUIT_STATE_PATH.read_text(encoding="utf-8"))
        return state.get("last_trip")
    except Exception as e:
        log.error("fetch_last_circuit_trip failed: %s", e)
        return None


def save_circuit_trip(date_str: str) -> None:
    """서킷 브레이커 발동 날짜 저장."""
    try:
        _CIRCUIT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CIRCUIT_STATE_PATH.write_text(
            json.dumps({"last_trip": date_str}), encoding="utf-8"
        )
    except Exception as e:
        log.error("save_circuit_trip failed: %s", e)


# ── 프리마켓 갭업 스킵 (W6) ─────────────────────────────

def mark_watchlist_skip_today(ticker: str) -> None:
    """당일 프리마켓 갭업 종목을 로컬 파일에 스킵 마킹."""
    try:
        _PREMARKET_SKIP_PATH.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if _PREMARKET_SKIP_PATH.exists():
            data = json.loads(_PREMARKET_SKIP_PATH.read_text(encoding="utf-8"))
        today = date.today().isoformat()
        skips = set(data.get(today, []))
        skips.add(ticker)
        data[today] = sorted(skips)
        _PREMARKET_SKIP_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.error("mark_watchlist_skip_today %s failed: %s", ticker, e)


def fetch_premarket_skips_today() -> set[str]:
    """당일 프리마켓 스킵 종목 코드 집합 반환."""
    try:
        if not _PREMARKET_SKIP_PATH.exists():
            return set()
        data = json.loads(_PREMARKET_SKIP_PATH.read_text(encoding="utf-8"))
        return set(data.get(date.today().isoformat(), []))
    except Exception as e:
        log.error("fetch_premarket_skips_today failed: %s", e)
        return set()
