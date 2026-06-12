"""Reflection 노드 — 리서치 결과 품질 검토."""
from langchain_core.messages import SystemMessage, HumanMessage

from autostock import config
from autostock.models import TradingState, ReflectionResult
from autostock.logger import get_logger
from autostock.research.llm_factory import get_boss_llm

log = get_logger(__name__)

_llm = get_boss_llm()


def reflection_node(state: TradingState) -> dict:
    """
    리서치 결과를 검토. REVIEW_COUNT 초과 시 강제 통과.
    실패 에이전트 목록을 failed_agents에 기록.
    """
    retry_count = state.get("retry_count", 0)

    if retry_count >= config.REVIEW_COUNT:
        log.info("reflection_node: REVIEW_COUNT(%d) 초과 — 강제 통과", config.REVIEW_COUNT)
        return {
            "reflection_passed": True,
            "reflection_feedback": "",
            "failed_agents": [],
        }

    tech = state.get("technical_reports", {})
    fund = state.get("fundamental_reports", {})
    sent = state.get("sentiment_reports", {})

    summary = (
        f"기술적 분석:\n{tech}\n\n"
        f"기본적 분석:\n{fund}\n\n"
        f"감성 분석:\n{sent}"
    )

    system = SystemMessage(content=(
        "당신은 투자 리서치 품질관리 책임자입니다. "
        "아래 리서치 결과를 검토하고 품질이 충분한지 판단하세요. "
        "불완전하거나 데이터가 누락된 에이전트가 있으면 failed_agents 리스트에 이름을 넣으세요. "
        "에이전트 이름 규칙: technical_agent / fundamental_agent / sentiment_agent\n\n"
        "주의사항:\n"
        "- PER=0 또는 PER가 매우 높은 경우(>100)는 적자기업이거나 성장주의 정상적인 수치이므로 실패로 보지 마세요.\n"
        "- 감성 점수가 낮아도(0~4점) 데이터가 있으면 성공으로 판단하세요.\n"
        "- 분석 텍스트가 있고 주요 수치가 채워져 있으면 통과로 판단하세요."
    ))
    human = HumanMessage(content=f"리서치 결과:\n{summary}")

    structured = _llm.with_structured_output(ReflectionResult)
    result: ReflectionResult = structured.invoke([system, human])

    new_retry = retry_count + (0 if result.passed else 1)
    log.info(
        "reflection_node: passed=%s retry=%d→%d failed=%s",
        result.passed, retry_count, new_retry, result.failed_agents
    )

    return {
        "reflection_passed": result.passed,
        "reflection_feedback": result.feedback,
        "failed_agents": result.failed_agents,
        "retry_count": new_retry,
    }


def route_after_reflection(state: TradingState) -> str:
    """Reflection 결과에 따라 다음 노드 결정."""
    if state.get("reflection_passed"):
        return "bull_node"
    return "supervisor"
