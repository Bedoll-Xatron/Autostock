"""Pydantic 스키마 및 LangGraph TradingState TypedDict."""
from typing import TypedDict
from pydantic import BaseModel, Field


# ── 입력 스키마 ─────────────────────────────────────────

class WatchlistItem(BaseModel):
    ticker: str
    name: str
    sector: str
    prev_close: float


class MarketData(BaseModel):
    date: str
    fear_greed_score: float
    fear_greed_rating: str
    vix: float
    vix_movement: str
    kospi: float
    kospi_movement: str
    condition: str


# ── 리서치 스키마 ────────────────────────────────────────

class ScreeningResult(BaseModel):
    selected_tickers: list[str] = Field(description="선택된 종목 코드 리스트 (최대 3개)")
    screening_reason: str = Field(description="종목 선정 이유")


class TechnicalReport(BaseModel):
    ticker: str
    trend: str = Field(description="상승/하락/횡보")
    rsi: float = Field(description="RSI 값 (0~100)")
    macd: str = Field(description="MACD 시그널 설명")
    signal: str = Field(description="기술적 매매 신호 (BUY/SELL/HOLD)")
    stop_loss_price: float = Field(description="손절 기준가")


class FundamentalReport(BaseModel):
    ticker: str
    per: float = Field(description="PER")
    pbr: float = Field(description="PBR")
    roe: float = Field(description="ROE (%)")
    earnings: str = Field(description="최근 실적 요약")
    valuation: str = Field(description="저평가/고평가/적정")


class SentimentReport(BaseModel):
    ticker: str
    news_score: float = Field(description="뉴스 감성 점수 (0~10)")
    disclosure: str = Field(description="최근 공시 요약")
    foreign_net: str = Field(description="외국인 순매수/순매도")
    inst_net: str = Field(description="기관 순매수/순매도")


# ── 검토 스키마 ─────────────────────────────────────────

class ReflectionResult(BaseModel):
    passed: bool = Field(description="검토 통과 여부")
    feedback: str = Field(description="반려 시 피드백 (통과 시 빈 문자열)")
    failed_agents: list[str] = Field(
        default_factory=list,
        description="재실행이 필요한 에이전트 이름 리스트"
    )


# ── 디베이트 스키마 ─────────────────────────────────────

class BullReport(BaseModel):
    ticker: str
    bull_score: float = Field(description="매수 강도 점수 (0~10)")
    bull_summary: str = Field(description="매수 논거")
    bull_rebuttal: str = Field(description="Bear 논거에 대한 반박")


class BearReport(BaseModel):
    ticker: str
    bear_score: float = Field(description="매도 강도 점수 (0~10)")
    bear_summary: str = Field(description="매도 논거")
    bear_rebuttal: str = Field(description="Bull 논거에 대한 반박")


# ── 최종 결정 스키마 ────────────────────────────────────

class FinalDecision(BaseModel):
    ticker: str
    action: str = Field(description="BUY / SELL / HOLD")
    price_reference: float = Field(description="기준가 (전일 종가 또는 현재가)")
    stop_loss_price: float = Field(description="손절 기준가")
    confidence: float = Field(description="신뢰도 점수 (0~10)")
    bull_summary: str
    bear_summary: str
    final_reason: str = Field(description="최종 결정 이유")


class SupervisorDecision(BaseModel):
    final_decisions: list[FinalDecision]


# ── LangGraph 상태 ───────────────────────────────────────

class TradingState(TypedDict):
    # 입력
    market_data: dict
    watchlist: list[WatchlistItem]

    # 스크리닝
    selected_tickers: list[str]
    screening_reason: str

    # 리서치팀
    technical_reports: dict[str, TechnicalReport]
    fundamental_reports: dict[str, FundamentalReport]
    sentiment_reports: dict[str, SentimentReport]

    # Reflection
    reflection_passed: bool
    reflection_feedback: str
    failed_agents: list[str]
    retry_count: int

    # 디베이트
    bull_reports: dict[str, BullReport]
    bear_reports: dict[str, BearReport]
    debate_round: int

    # 최종 결정
    final_decisions: list[FinalDecision]

    # HITL
    hitl_result: str   # "pending" / "approved" / "rejected"
    approved_qty: dict[str, int]  # ticker → 사람이 입력한 수량 (없으면 AI 계산)
