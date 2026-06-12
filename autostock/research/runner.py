"""그래프 실행 및 interrupt/resume 헬퍼 (async 버전)."""
from datetime import date
from langgraph.types import Command
from autostock.models import TradingState
from autostock.logger import get_logger

log = get_logger(__name__)


def get_thread_id(suffix: str = "") -> str:
    """날짜별 고유 thread ID — suffix로 오전/오후 체크포인트 분리."""
    return f"trading_{date.today().isoformat()}{suffix}"


def get_thread_config(suffix: str = "") -> dict:
    return {"configurable": {"thread_id": get_thread_id(suffix)}}


def build_initial_state(market_data: dict, watchlist: list[dict]) -> TradingState:
    return {
        "market_data":         market_data,
        "watchlist":           watchlist,
        "selected_tickers":    [],
        "screening_reason":    "",
        "technical_reports":   {},
        "fundamental_reports": {},
        "sentiment_reports":   {},
        "reflection_passed":   False,
        "reflection_feedback": "",
        "failed_agents":       [],
        "retry_count":         0,
        "bull_reports":        {},
        "bear_reports":        {},
        "debate_round":        0,
        "final_decisions":     [],
        "hitl_result":         "pending",
        "approved_qty":        {},
    }


async def run_until_interrupt(
    graph, market_data: dict, watchlist: list[dict], thread_suffix: str = ""
) -> dict | None:
    """
    그래프를 초기 상태로 비동기 실행하여 interrupt 지점까지 진행.
    interrupt가 발생하면 현재 State를 반환.
    interrupt 없이 완료되면 None 반환.
    """
    thread_config = get_thread_config(thread_suffix)
    initial_state = build_initial_state(market_data, watchlist)
    await graph.ainvoke(initial_state, config=thread_config)

    state_snapshot = graph.get_state(thread_config)
    if state_snapshot.next:
        log.info("run_until_interrupt: interrupt 발생 — Telegram HITL 대기")
        return state_snapshot.values
    log.warning("run_until_interrupt: interrupt 없이 완료 (비정상)")
    return None


async def resume_graph(
    graph, status: str, approved_qty: dict | None = None, thread_suffix: str = ""
) -> dict:
    """
    interrupt()로 멈춘 그래프를 비동기로 재개한다.

    Args:
        graph: 컴파일된 그래프.
        status: "approved" 또는 "rejected".
        approved_qty: ticker → 수량 딕셔너리 (승인 시 수량 정보).
        thread_suffix: 오전 "" / 오후 "_afternoon" 등 체크포인트 분리용.

    Returns:
        최종 State 딕셔너리.
    """
    log.info("resume_graph: status=%s qty=%s suffix=%s", status, approved_qty, thread_suffix)
    result = await graph.ainvoke(
        Command(resume={"status": status, "approved_qty": approved_qty or {}}),
        config=get_thread_config(thread_suffix),
    )
    return result
