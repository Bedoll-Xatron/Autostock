"""LangGraph StateGraph 조립 및 컴파일."""
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from autostock.models import TradingState
from autostock.research.agents import (
    screening_agent,
    technical_agent,
    fundamental_agent,
    sentiment_agent,
    retry_research,
)
from autostock.research.reflection import reflection_node, route_after_reflection
from autostock.research.debate import bull_node, bear_node, route_debate
from autostock.research.supervisor import supervisor_node, route_supervisor
from autostock.research.hitl_node import human_review_node


def route_to_research(state: TradingState):
    """Send API — 3개 리서치 에이전트를 병렬 실행."""
    return [
        Send("technical_agent",   state),
        Send("fundamental_agent", state),
        Send("sentiment_agent",   state),
    ]


def build_graph() -> tuple:
    """
    그래프 조립 후 (compiled_graph, checkpointer) 반환.
    checkpointer는 interrupt() 재개에 필요.
    """
    _serde = JsonPlusSerializer(
        allowed_msgpack_modules=[
            ("autostock.models", "TechnicalReport"),
            ("autostock.models", "FundamentalReport"),
            ("autostock.models", "SentimentReport"),
            ("autostock.models", "BullReport"),
            ("autostock.models", "BearReport"),
            ("autostock.models", "FinalDecision"),
        ]
    )
    checkpointer = MemorySaver(serde=_serde)
    builder = StateGraph(TradingState)

    # 노드 등록
    builder.add_node("supervisor",        supervisor_node)
    builder.add_node("screening_agent",   screening_agent)
    builder.add_node("technical_agent",   technical_agent)
    builder.add_node("fundamental_agent", fundamental_agent)
    builder.add_node("sentiment_agent",   sentiment_agent)
    builder.add_node("reflection_node",   reflection_node)
    builder.add_node("bull_node",         bull_node)
    builder.add_node("bear_node",         bear_node)
    builder.add_node("retry_research",    retry_research)
    builder.add_node("human_review_node", human_review_node)

    # 엣지
    builder.add_edge(START, "supervisor")

    builder.add_conditional_edges("supervisor", route_supervisor, {
        "screening_agent":   "screening_agent",
        "retry_research":    "retry_research",
        "human_review_node": "human_review_node",
    })

    builder.add_conditional_edges(
        "screening_agent", route_to_research,
        ["technical_agent", "fundamental_agent", "sentiment_agent"]
    )
    builder.add_conditional_edges(
        "retry_research", route_to_research,
        ["technical_agent", "fundamental_agent", "sentiment_agent"]
    )

    builder.add_edge("technical_agent",   "reflection_node")
    builder.add_edge("fundamental_agent", "reflection_node")
    builder.add_edge("sentiment_agent",   "reflection_node")

    builder.add_conditional_edges("reflection_node", route_after_reflection, {
        "bull_node":  "bull_node",
        "supervisor": "supervisor",
    })

    builder.add_edge("bull_node", "bear_node")
    builder.add_conditional_edges("bear_node", route_debate, {
        "bull_node":  "bull_node",
        "supervisor": "supervisor",
    })

    builder.add_edge("human_review_node", END)

    compiled = builder.compile(checkpointer=checkpointer)
    return compiled, checkpointer
