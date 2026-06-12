"""에이전트 함수 — screening, technical, fundamental, sentiment (ReAct 루프)."""
import datetime as _dt
import random as _random
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from autostock.research.llm_factory import get_basic_llm, get_boss_llm

from autostock import config
from autostock.models import (
    TradingState,
    ScreeningResult,
    TechnicalReport,
    FundamentalReport,
    SentimentReport,
)
from autostock.research.tools import (
    get_technical_indicators,
    get_fundamental_indicators,
    get_sentiment_indicators,
)
from autostock.market.fetcher import get_large_cap_tickers
from autostock.logger import get_logger

log = get_logger(__name__)

_llm_basic = get_basic_llm()
_llm_boss = get_boss_llm()

SCREENING_TOOLS = []
TECHNICAL_TOOLS = [get_technical_indicators]
FUNDAMENTAL_TOOLS = [get_fundamental_indicators]
SENTIMENT_TOOLS = [get_sentiment_indicators]


def _react_loop(llm_with_tools, messages: list) -> list:
    """ReAct 루프 — tool_calls가 없을 때까지 반복."""
    while True:
        response = llm_with_tools.invoke(messages)
        messages.append(response)
        if not getattr(response, "tool_calls", None):
            break
        # 도구 실행
        tool_map = {
            "get_technical_indicators": get_technical_indicators,
            "get_fundamental_indicators": get_fundamental_indicators,
            "get_sentiment_indicators": get_sentiment_indicators,
        }
        for tc in response.tool_calls:
            tool_fn = tool_map.get(tc["name"])
            if tool_fn:
                result = tool_fn.invoke(tc["args"])
                messages.append(
                    ToolMessage(content=str(result), tool_call_id=tc["id"])
                )
    return messages


_MAX_SCREEN_POOL = 50  # LLM에 보여줄 최대 종목 수
_BOB_MIN = 7          # Best of Best 최소 보장 수 (이 이상이면 AI 선별 생략)

_BAD_PHASES = {"distribution", "markdown"}  # Supervisor가 Bear 논거로 즉시 기각하는 국면


def _shuffle_by_date(watchlist: list[dict]) -> list[dict]:
    """
    마켓플로우 시그널 종목을 앞에 고정(전략수 내림차순), 나머지는 날짜 내 셔플.
    시그널 종목이 _MAX_SCREEN_POOL 슬롯에서 밀리는 문제 방지.
    """
    signaled = [w for w in watchlist if w.get("strategies")]
    no_signal = [w for w in watchlist if not w.get("strategies")]

    # 시그널 종목: 전략수 내림차순 → 점수 내림차순
    signaled.sort(key=lambda w: (-(w.get("strategy_count") or 1), -(w.get("signal_score") or 0)))

    # 시그널 없는 종목: 날짜 버킷 내 셔플
    keyfn = lambda w: (w.get("created_at") or "")[:10]
    groups: dict[str, list] = {}
    for w in no_signal:
        k = keyfn(w)
        groups.setdefault(k, []).append(w)
    rest: list[dict] = []
    for d in sorted(groups.keys(), reverse=True):
        bucket = groups[d]
        _random.shuffle(bucket)
        rest.extend(bucket)

    return signaled + rest


def _signal_age_label(created_at_str: str) -> str:
    """created_at ISO 문자열로부터 '오늘 신규' 또는 'N일 전 신호' 반환."""
    try:
        ts = _dt.datetime.fromisoformat(created_at_str)
        today = _dt.datetime.now(ts.tzinfo) if ts.tzinfo else _dt.datetime.now()
        days = (today - ts).days
        return "오늘 신규" if days == 0 else f"{days}일 전 신호"
    except Exception:
        return ""


def _get_bob_candidates(watchlist: list[dict], n: int) -> list[dict]:
    """
    Best of Best 후보 추출 (대형주 제외).
    """
    # 대형주(KOSPI 100, KOSDAQ 50) 제외 필터 적용
    large_caps = get_large_cap_tickers()
    watchlist = [w for w in watchlist if w["ticker"] not in large_caps]

    signaled = [w for w in watchlist if w.get("strategies")]

    def _phase_penalty(w: dict) -> int:
        return 1 if w.get("rotation_phase") in _BAD_PHASES else 0

    multi = sorted(
        [w for w in signaled if (w.get("strategy_count") or 1) >= 2],
        key=lambda w: (_phase_penalty(w), -(w.get("strategy_count") or 1), -(w.get("signal_score") or 0)),
    )
    single = sorted(
        [w for w in signaled if (w.get("strategy_count") or 1) < 2],
        key=lambda w: (_phase_penalty(w), -(w.get("signal_score") or 0)),
    )
    return (multi + single)[:n]


def screening_agent(state: TradingState) -> dict:
    """
    Best of Best 우선 선정 후 AI 스크리닝으로 나머지 보충.

    BoB(_BOB_MIN개) 조건이 충족되면 AI 선별을 생략하고 BoB를 그대로 통과시킨다.
    부족한 경우 AI가 워치리스트 풀에서 나머지 슬롯을 채운다.
    """
    watchlist = state["watchlist"]
    market_data = state["market_data"]

    bob = _get_bob_candidates(watchlist, _BOB_MIN)
    bob_tickers = [w["ticker"] for w in bob]

    if len(bob) >= _BOB_MIN:
        reason = (
            f"Best of Best {len(bob)}개 자동 선정 "
            f"(멀티컨펌 {sum(1 for w in bob if (w.get('strategy_count') or 1) >= 2)}개 포함): "
            + ", ".join(f"{w['ticker']}({w.get('strategies','')})" for w in bob)
        )
        log.info("screening_agent [BoB 자동선정]: %s", bob_tickers)
        return {"selected_tickers": bob_tickers, "screening_reason": reason}

    # BoB 부족 → AI가 나머지 보충
    remaining_needed = _BOB_MIN - len(bob)
    # 대형주·악화 국면 제외 필터 (distribution/markdown은 Supervisor가 Bear 논거로 즉시 기각)
    large_caps = get_large_cap_tickers()
    bob_ticker_set = set(bob_tickers)
    pool = [
        w for w in _shuffle_by_date(watchlist)
        if w["ticker"] not in bob_ticker_set
        and w["ticker"] not in large_caps
        and w.get("rotation_phase") not in _BAD_PHASES
    ]
    pool = pool[:_MAX_SCREEN_POOL]

    def _fmt_flow(v) -> str:
        if v is None:
            return ""
        sign = "+" if v >= 0 else ""
        return f"{sign}{v / 1e8:.1f}억"

    lines = []
    for w in pool:
        age = _signal_age_label(w["created_at"]) if w.get("created_at") else ""
        age_str = f" [{age}]" if age else ""
        strategies = w.get("strategies") or ""
        score = w.get("signal_score")
        score_str = f" 시그널:{strategies}(최고{score:.0f})" if score is not None and strategies else ""
        scnt = w.get("strategy_count") or 1
        scnt_str = f" 전략수:{scnt}" if scnt > 1 else ""
        f5 = w.get("foreign_5d")
        i5 = w.get("inst_5d")
        flow_parts = []
        if f5 is not None:
            flow_parts.append(f"외:{_fmt_flow(f5)}")
        if i5 is not None:
            flow_parts.append(f"기:{_fmt_flow(i5)}")
        flow_str = f" [{', '.join(flow_parts)}]" if flow_parts else ""
        chg = w.get("change_pct")
        chg_str = f" 등락:{chg:+.1f}%" if chg is not None else ""
        theme = w.get("theme") or ""
        phase = w.get("rotation_phase") or ""
        extra = " | ".join(filter(None, [theme, phase]))
        extra_str = f" [{extra}]" if extra else ""
        lines.append(
            f"- {w['ticker']} {w['name']} ({w['sector']}) "
            f"전일종가:{w['prev_close']}"
            f"{score_str}{scnt_str}{flow_str}{chg_str}{extra_str}{age_str}"
        )

    bob_summary = ", ".join(f"{w['ticker']}({w.get('strategies','')})" for w in bob) or "없음"
    system = SystemMessage(content=(
        f"【국면(rotation_phase) 및 기술적 필터 — 최우선】\n"
        f"- accumulation(매집): 에너지가 응축된 구간, 손익비 최우수 → 최우선 선택\n"
        f"- markup(상승): 정배열(20 > 60) 초기 단계이면서 change_pct가 +7% 미만인 종목 선호\n"
        f"- VCP(변동성 축소): 최근 변동성이 줄어들며 힘을 모은 종목에 높은 가산점\n\n"
        f"【수급 및 재료 기준】\n"
        f"1. 거래량: 최근 5일 평균 대비 오늘 거래량이 2배 이상 터진 '수급 폭발' 종목 주목\n"
        f"2. 재료: 대기업(삼성, LG, SK 등)과의 MOU, 신규 투자, 독점 공급 소식 필수 확인\n"
        f"3. 수급: 외국인·기관이 최근 3~5일간 연속 매집 중인 종목\n"
        f"4. 제외: 대형주, 이미 +10% 이상 급등한 종목, distribution/markdown 국면 종목\n\n"
        f"당신은 인지도가 낮더라도 데이터상 시그널이 명확한 '숨겨진 보석'을 찾아내야 합니다."
    ))
    human = HumanMessage(content=(
        f"시장 상황:\n{market_data}\n\n"
        f"워치리스트 (BoB 제외):\n" + "\n".join(lines) + f"\n\n"
        f"추가 {remaining_needed}개 종목을 선택하고 이유를 설명하세요."
    ))

    structured = _llm_boss.with_structured_output(ScreeningResult)
    result: ScreeningResult = structured.invoke([system, human])

    ai_picks = [t for t in result.selected_tickers if t not in bob_ticker_set]
    selected = bob_tickers + ai_picks[:remaining_needed]
    reason = f"BoB {len(bob)}개 + AI추가 {len(ai_picks[:remaining_needed])}개: {result.screening_reason}"
    log.info("screening_agent [BoB+AI]: %s", selected)
    return {"selected_tickers": selected, "screening_reason": reason}


def technical_agent(state: TradingState) -> dict:
    """선택 종목별 기술 분석 (RSI, MACD, 추세, 손절가)."""
    tickers = state["selected_tickers"]
    llm_with_tools = _llm_basic.bind_tools(TECHNICAL_TOOLS)
    technical_reports = dict(state.get("technical_reports", {}))

    for ticker in tickers:
        system = SystemMessage(content=(
            "당신은 기술적 분석 전문가입니다. "
            "get_technical_indicators 도구를 반드시 사용해 데이터를 조회한 후 "
            "기술적 분석 보고서를 작성하세요. "
            "모든 분석 내용은 반드시 한국어로 작성하세요."
        ))
        human = HumanMessage(content=f"ticker: {ticker} 의 기술적 지표를 분석하세요.")
        messages = _react_loop(llm_with_tools, [system, human])

        structured = _llm_basic.with_structured_output(TechnicalReport)
        report = structured.invoke(messages + [HumanMessage(
            content="위 분석 결과를 바탕으로 TechnicalReport 형식으로 요약하세요. 모든 텍스트 필드는 한국어로 작성하세요."
        )])
        technical_reports[ticker] = report
        log.info("technical_agent done: %s RSI=%.1f", ticker, report.rsi)

    return {"technical_reports": technical_reports}


def fundamental_agent(state: TradingState) -> dict:
    """선택 종목별 기본 분석 (PER, PBR, ROE, 실적)."""
    tickers = state["selected_tickers"]
    llm_with_tools = _llm_basic.bind_tools(FUNDAMENTAL_TOOLS)
    fundamental_reports = dict(state.get("fundamental_reports", {}))

    for ticker in tickers:
        system = SystemMessage(content=(
            "당신은 재무 분석 전문가입니다. "
            "get_fundamental_indicators 도구를 반드시 사용해 데이터를 조회한 후 "
            "기본적 분석 보고서를 작성하세요. "
            "모든 분석 내용은 반드시 한국어로 작성하세요."
        ))
        human = HumanMessage(content=f"ticker: {ticker} 의 기본 지표를 분석하세요.")
        messages = _react_loop(llm_with_tools, [system, human])

        structured = _llm_basic.with_structured_output(FundamentalReport)
        report = structured.invoke(messages + [HumanMessage(
            content="위 분석 결과를 바탕으로 FundamentalReport 형식으로 요약하세요. 모든 텍스트 필드는 한국어로 작성하세요."
        )])
        fundamental_reports[ticker] = report
        log.info("fundamental_agent done: %s PER=%.1f", ticker, report.per)

    return {"fundamental_reports": fundamental_reports}


def sentiment_agent(state: TradingState) -> dict:
    """선택 종목별 감성 분석 (뉴스, 수급)."""
    tickers = state["selected_tickers"]
    watchlist = state["watchlist"]
    ticker_name_map = {w["ticker"]: w["name"] for w in watchlist}
    llm_with_tools = _llm_basic.bind_tools(SENTIMENT_TOOLS)
    sentiment_reports = dict(state.get("sentiment_reports", {}))

    for ticker in tickers:
        name = ticker_name_map.get(ticker, ticker)
        system = SystemMessage(content=(
            "당신은 시장 감성 및 재료 분석 전문가입니다. "
            "get_sentiment_indicators 도구를 사용해 뉴스를 조회한 후 보고서를 작성하세요.\n\n"
            "특별 지침:\n"
            "1. 'MOU', '대기업 협력', '투자 계획', '공급 계약', '매집' 키워드에 집중하세요.\n"
            "2. 단순 홍보성 뉴스보다 실질적인 기업 가치 변화(원석 발굴) 가능성을 높게 평가하세요.\n"
            "3. 모든 분석 내용은 한국어로 작성하세요."
        ))
        human = HumanMessage(
            content=f"ticker: {ticker}, 종목명: {name} 의 감성 지표를 분석하세요."
        )
        messages = _react_loop(llm_with_tools, [system, human])

        structured = _llm_basic.with_structured_output(SentimentReport)
        report = structured.invoke(messages + [HumanMessage(
            content="위 분석 결과를 바탕으로 SentimentReport 형식으로 요약하세요. 모든 텍스트 필드는 한국어로 작성하세요."
        )])
        sentiment_reports[ticker] = report
        log.info("sentiment_agent done: %s score=%.1f", ticker, report.news_score)

    return {"sentiment_reports": sentiment_reports}


def retry_research(state: TradingState) -> dict:
    """Reflection 반려 후 재실행을 위한 경유 노드 (상태 유지)."""
    log.info("retry_research: retry_count=%d", state.get("retry_count", 0))
    return {}
