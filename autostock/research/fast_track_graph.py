"""실시간 급등주 스캐너 전용 가벼운 LangGraph. (Fast Track)"""
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from autostock.models import TradingState
from autostock.research.agents import technical_agent, sentiment_agent
from autostock.logger import get_logger

log = get_logger(__name__)


def route_to_fast_research(state: TradingState):
    """Send API — 기술적/수급 분석과 뉴스/감성 분석만 병렬 실행."""
    return [
        Send("technical_agent", state),
        Send("sentiment_agent", state),
    ]


async def fast_supervisor_node(state: TradingState) -> dict:
    """재무 및 토론 생략, 즉각적인 BUY/HOLD 판단."""
    from autostock.models import FinalDecision
    from autostock.research.llm_factory import get_boss_llm

    watchlist = state.get("watchlist", [])
    if not watchlist:
        return {}

    target = watchlist[0]
    ticker = target["ticker"]
    name = target.get("name", ticker)
    price = target.get("price_reference", 0)

    tech_rep = state.get("technical_reports", {}).get(ticker)
    sent_rep = state.get("sentiment_reports", {}).get(ticker)

    prompt = f"""당신은 실시간 돌파매매(Breakout) 전문 데이트레이더입니다.
이 종목은 오늘 장중 시가 대비 5% 이상 급등한 종목입니다.

[분석 대상 종목]
{name} ({ticker})
- 현재가: {price}

[최근 차트/수급 요약]
{tech_rep.model_dump() if tech_rep else "데이터 없음"}

[최신 뉴스/모멘텀 요약]
{sent_rep.model_dump() if sent_rep else "데이터 없음"}

위 데이터를 바탕으로 지금 당장 시장가로 추격 매수(BUY)할지, 
단순한 윗꼬리 페이크일 가능성이 커 관망(HOLD)할지 10점 만점 신뢰도로 평가하세요.
(급등 돌파매매이므로, 실체가 있는 강력한 호재/수급이면 적극 BUY, 모호한 이유면 HOLD)

응답은 반드시 FinalDecision JSON 형태로 작성하세요.
"""
    llm = get_boss_llm().with_structured_output(FinalDecision)
    try:
        decision: FinalDecision = await llm.ainvoke(prompt)
        # 종목 정보 보정
        decision.ticker = ticker
        decision.price_reference = price
        if decision.stop_loss_price <= 0:
            decision.stop_loss_price = price * 0.97  # 기본 -3% 손절
        log.info("fast_supervisor decision: %s → %s (confidence=%.1f)", ticker, decision.action, decision.confidence)
        return {"final_decisions": [decision]}
    except Exception as e:
        log.error("fast_supervisor 실패: %s", e)
        return {}


def build_fast_track_graph() -> tuple:
    """재무/토론/승인 단계를 모두 생략한 고속 그래프 반환."""
    _serde = JsonPlusSerializer(
        allowed_msgpack_modules=[
            ("autostock.models", "TechnicalReport"),
            ("autostock.models", "SentimentReport"),
            ("autostock.models", "FinalDecision"),
        ]
    )
    checkpointer = MemorySaver(serde=_serde)
    builder = StateGraph(TradingState)

    # 노드 등록 (최소한의 노드)
    builder.add_node("fast_supervisor", fast_supervisor_node)
    builder.add_node("technical_agent", technical_agent)
    builder.add_node("sentiment_agent", sentiment_agent)

    # 엣지 연결: START -> (technical, sentiment) 병렬 -> fast_supervisor -> END
    builder.add_conditional_edges(START, route_to_fast_research, ["technical_agent", "sentiment_agent"])
    builder.add_edge("technical_agent", "fast_supervisor")
    builder.add_edge("sentiment_agent", "fast_supervisor")
    builder.add_edge("fast_supervisor", END)

    compiled = builder.compile(checkpointer=checkpointer)
    return compiled, checkpointer
