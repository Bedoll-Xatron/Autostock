"""human_review_node — LangGraph interrupt() 기반 HITL 노드."""
from langgraph.types import interrupt
from autostock.models import TradingState
from autostock.logger import get_logger

log = get_logger(__name__)


def human_review_node(state: TradingState) -> dict:
    """
    interrupt()로 그래프를 일시 정지하고 사람의 승인을 기다린다.
    재개 시 human_response에서 status와 approved_qty를 받는다.

    human_response 구조:
      {
        "status": "approved" | "rejected",
        "approved_qty": {"005930": 10, "000660": 5}  # 선택사항 — 없으면 AI 계산
      }
    """
    log.info("human_review_node: interrupt() 호출 — Telegram HITL 대기")

    human_response = interrupt({
        "final_decisions": [d.model_dump() for d in state["final_decisions"]],
        "message": "최종 매매 결정을 검토하고 승인 또는 거절해주세요.",
    })

    log.info("human_review_node: 재개 — status=%s", human_response.get("status"))
    return {
        "hitl_result": human_response["status"],
        "approved_qty": human_response.get("approved_qty", {}),
    }
