"""Bull/Bear 디베이트 노드."""
from langchain_core.messages import SystemMessage, HumanMessage

from autostock import config
from autostock.models import TradingState, BullReport, BearReport
from autostock.logger import get_logger
from autostock.research.llm_factory import get_boss_llm

log = get_logger(__name__)

_llm = get_boss_llm()


def _build_research_summary(state: TradingState, ticker: str) -> str:
    tech = state.get("technical_reports", {}).get(ticker, {})
    fund = state.get("fundamental_reports", {}).get(ticker, {})
    sent = state.get("sentiment_reports", {}).get(ticker, {})
    return (
        f"기술적 분석: {tech}\n"
        f"기본적 분석: {fund}\n"
        f"감성 분석:   {sent}"
    )


def bull_node(state: TradingState) -> dict:
    """종목별 Bull(매수) 논거 작성."""
    tickers = state["selected_tickers"]
    bear_reports = state.get("bear_reports", {})
    bull_reports = dict(state.get("bull_reports", {}))

    for ticker in tickers:
        research = _build_research_summary(state, ticker)
        bear = bear_reports.get(ticker)
        bear_text = f"\nBear 이전 논거:\n{bear.bear_summary}" if bear else ""

        system = SystemMessage(content=(
            "당신은 낙관적 투자 분석가(Bull)입니다. "
            "제공된 리서치 데이터를 바탕으로 해당 종목의 매수 근거를 제시하고, "
            "Bear 논거가 있다면 논리적으로 반박하세요. "
            "모든 분석 내용은 반드시 한국어로 작성하세요."
        ))
        human = HumanMessage(content=f"종목: {ticker}\n{research}{bear_text}")

        structured = _llm.with_structured_output(BullReport)
        report: BullReport = structured.invoke([system, human])
        bull_reports[ticker] = report
        log.info("bull_node: %s score=%.1f", ticker, report.bull_score)

    return {"bull_reports": bull_reports, "debate_round": state.get("debate_round", 0)}


def bear_node(state: TradingState) -> dict:
    """종목별 Bear(매도) 논거 작성 + 라운드 카운트."""
    tickers = state["selected_tickers"]
    bull_reports = state.get("bull_reports", {})
    bear_reports = dict(state.get("bear_reports", {}))

    for ticker in tickers:
        research = _build_research_summary(state, ticker)
        bull = bull_reports.get(ticker)
        bull_text = f"\nBull 이전 논거:\n{bull.bull_summary}" if bull else ""

        system = SystemMessage(content=(
            "당신은 리스크를 중시하는 비관적 투자 분석가(Bear)입니다. "
            "제공된 리서치 데이터를 바탕으로 해당 종목의 단점, 고평가 논란, "
            "역배열, 거래량 감소 등 매도/위험 근거를 분석하세요. "
            "실질적인 리스크가 없다면 낮은 bear_score(1~3)를 부여하세요. "
            "치명적 리스크(상장폐지 위험, 횡령, 거품 붕괴 임박 등)에만 높은 점수(8~10)를 부여하세요. "
            "Bull 논거가 있다면 논리적으로 가장 약한 고리를 공격하여 반박하세요. "
            "모든 분석 내용은 반드시 한국어로 작성하세요."
        ))
        human = HumanMessage(content=f"종목: {ticker}\n{research}{bull_text}")

        structured = _llm.with_structured_output(BearReport)
        report: BearReport = structured.invoke([system, human])
        bear_reports[ticker] = report
        log.info("bear_node: %s score=%.1f", ticker, report.bear_score)

    new_round = state.get("debate_round", 0) + 1
    log.info("bear_node: debate_round %d → %d (max=%d)", state.get("debate_round", 0), new_round, config.MAX_DEBATE_ROUNDS)
    return {"bear_reports": bear_reports, "debate_round": new_round}


def route_debate(state: TradingState) -> str:
    """디베이트 라운드 수에 따라 루프 또는 supervisor 진행."""
    if state.get("debate_round", 0) < config.MAX_DEBATE_ROUNDS:
        return "bull_node"
    return "supervisor"
