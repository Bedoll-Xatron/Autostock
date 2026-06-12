"""Supervisor 노드 및 라우터."""
from langchain_core.messages import SystemMessage, HumanMessage

from autostock import config
from autostock.models import TradingState, SupervisorDecision
from autostock.logger import get_logger
from autostock.research.llm_factory import get_boss_llm

log = get_logger(__name__)

_llm = get_boss_llm()


def supervisor_node(state: TradingState) -> dict:
    """
    CEO 역할. 3가지 진입 케이스를 처리:
    1. 최초 진입 → 스크리닝 지시 (상태 업데이트 없음, route_supervisor가 분기)
    2. Reflection 반려 재진입 → 리서치 재지시 (상태 업데이트 없음, route_supervisor가 분기)
    3. 디베이트 완료 후 재진입 → 최종 결정 생성
    """
    bull_reports = state.get("bull_reports", {})
    bear_reports = state.get("bear_reports", {})

    # 케이스 3: 디베이트 완료 — 최종 결정 생성
    if bull_reports and bear_reports and state.get("debate_round", 0) >= config.MAX_DEBATE_ROUNDS:
        return _make_final_decisions(state)

    # 케이스 1 & 2: 스크리닝 또는 재지시 — route_supervisor가 처리하므로 상태 변경 없음
    return {}


def _make_final_decisions(state: TradingState) -> dict:
    """Bull/Bear 디베이트 결과를 바탕으로 종목별 최종 매매 결정 생성."""
    tickers = state["selected_tickers"]
    bull_reports = state["bull_reports"]
    bear_reports = state["bear_reports"]
    watchlist = state["watchlist"]
    ticker_map = {w["ticker"]: w for w in watchlist}

    debate_summary = ""
    for ticker in tickers:
        bull = bull_reports.get(ticker)
        bear = bear_reports.get(ticker)
        prev_close = ticker_map.get(ticker, {}).get("prev_close", 0)
        debate_summary += (
            f"\n=== {ticker} (전일종가: {prev_close}) ===\n"
            f"Bull(점수:{bull.bull_score if bull else 'N/A'}): {bull.bull_summary if bull else 'N/A'}\n"
            f"Bear(점수:{bear.bear_score if bear else 'N/A'}): {bear.bear_summary if bear else 'N/A'}\n"
        )

    buy_threshold = 5 if config.KIS_SIMULATED_MODE else 7
    mode_hint = (
        f"모의투자 모드: confidence {buy_threshold}점 이상이면 BUY 적극 권장 (백테스트 데이터 수집 목적)"
        if config.KIS_SIMULATED_MODE else
        f"실거래 모드: confidence {buy_threshold}점 이상일 때만 BUY"
    )
    system = SystemMessage(content=(
        "당신은 포트폴리오 매니저(CIO)입니다. "
        "Bull/Bear 디베이트 결과를 점수 기반으로 종합하여 각 종목에 대해 BUY/SELL/HOLD 결정을 내리세요.\n\n"
        "【결정 기준】\n"
        f"1. confidence {buy_threshold}점 이상이고 bull_score가 bear_score보다 높으면 BUY를 적극 고려하세요.\n"
        "2. bull_score - bear_score >= 1.5 이면 BUY 신호로 해석하세요.\n"
        "3. bear_score가 8 이상이거나 치명적 리스크(상장폐지 위험, 횡령 등)가 있을 때만 HOLD하세요.\n"
        "4. 'Bear 논거가 하나라도 있다'는 이유만으로 HOLD하지 마세요. 모든 종목에는 리스크가 존재합니다.\n\n"
        f"신뢰도(confidence)는 0~10. ({mode_hint})\n"
        "모든 분석 내용과 이유는 반드시 한국어로 작성하세요."
    ))
    human = HumanMessage(content=(
        f"시장 상황: {state['market_data']}\n\n"
        f"디베이트 결과:{debate_summary}\n\n"
        f"각 종목의 최종 매매 결정을 내려주세요."
    ))

    structured = _llm.with_structured_output(SupervisorDecision)
    result: SupervisorDecision = structured.invoke([system, human])

    for d in result.final_decisions:
        log.info("supervisor decision: %s → %s (confidence=%.1f)", d.ticker, d.action, d.confidence)

    return {"final_decisions": result.final_decisions}


def route_supervisor(state: TradingState) -> str:
    """Supervisor 다음 노드 결정."""
    if state.get("final_decisions"):
        return "human_review_node"

    if state.get("retry_count", 0) > 0 and not state.get("reflection_passed"):
        return "retry_research"

    return "screening_agent"
