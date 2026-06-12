from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


class Grade(Enum):
    A = "A"  # 11점 이상 — 적극 매수
    B = "B"  # 9~10점 — 관심 관찰
    C = "C"  # 8점 이하 또는 필수 미통과 — 패스
    W = "W"  # VCP 형성 중 — 워치리스트 (포지션 없음)


@dataclass
class StockData:
    code: str
    name: str
    market: str
    open: int
    high: int
    low: int
    close: int
    volume: int
    trading_value: int
    market_cap: int
    change_pct: float
    high_52w: int
    low_52w: int


@dataclass
class SupplyData:
    code: str
    foreign_net_5d: int
    inst_net_5d: int
    foreign_hold_pct: float


@dataclass
class ChartData:
    code: str
    date: date
    open: int
    high: int
    low: int
    close: int
    volume: int
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None


@dataclass
class ScoreDetail:
    news: int = 0             # 뉴스/재료 0~3점
    volume: int = 0           # 거래대금 0~3점
    chart: int = 0            # 차트패턴 0~3점
    candle: int = 0           # 캔들형태 0~1점
    consolidation: int = 0    # 기간조정 0~1점
    supply: int = 0           # 수급 0~2점
    retracement: int = 0      # 조정폭 회복 0~1점
    pullback_support: int = 0 # 되돌림 지지 0~1점
    vdu_score: int = 0        # 거래량 고갈 (VDU) 0~1점
    rs_score: int = 0         # 상대강도 (RS) 0~2점
    vcp_score: int = 0        # VCP 패턴 0~2점
    short_score: int = 0      # 공매도 잔고 역이용 (Short Squeeze) 0~2점
    rvol_score: int = 0       # 상대거래량 조기감지 (RVOL) 0~2점
    llm_reason: str = ""      # LLM 분석 이유

    @property
    def total(self) -> int:
        return (
            self.news + self.volume + self.chart + self.candle +
            self.consolidation + self.supply + self.retracement +
            self.pullback_support + self.vdu_score + self.rs_score +
            self.vcp_score + self.short_score + self.rvol_score
        )

    @property
    def mandatory_passed(self) -> bool:
        # W5: RVOL 또는 VCP 중 하나는 반드시 존재해야 통과 (하드 게이트)
        return (
            self.news >= 1
            and self.volume >= 1
            and (self.rvol_score >= 1 or self.vcp_score >= 1)
        )

    def to_dict(self) -> dict:
        return {
            "news": self.news,
            "volume": self.volume,
            "chart": self.chart,
            "candle": self.candle,
            "consolidation": self.consolidation,
            "supply": self.supply,
            "retracement": self.retracement,
            "pullback_support": self.pullback_support,
            "vdu_score": self.vdu_score,
            "rs_score": self.rs_score,
            "vcp_score": self.vcp_score,
            "short_score": self.short_score,
            "rvol_score": self.rvol_score,
            "llm_reason": self.llm_reason,
            "total": self.total,
        }


@dataclass
class ChecklistDetail:
    """체크리스트 상세 — 필수/보조/부정적 조건"""

    # 필수 조건
    has_news: bool = False
    news_sources: list[str] = field(default_factory=list)
    volume_sufficient: bool = False

    # 보조 조건
    is_new_high: bool = False               # 52주 신고가
    is_breakout: bool = False               # 돌파
    ma_aligned: bool = False                # 단기 정배열 (MA5>MA10>MA20)
    ma_aligned_long: bool = False           # 중장기 정배열 (MA20>MA60>MA120)
    good_candle: bool = False               # 좋은 캔들
    upper_wick_long: bool = False           # 윗꼬리 김
    has_consolidation: bool = False         # 기간조정
    supply_positive: bool = False           # 수급 양호
    retracement_recovery: bool = False
    pullback_support_confirmed: bool = False

    # 부정적
    negative_news: bool = False

    def to_dict(self) -> dict[str, dict]:
        return {
            "mandatory": {
                "has_news": self.has_news,
                "news_sources": self.news_sources,
                "volume_sufficient": self.volume_sufficient,
            },
            "optional": {
                "is_new_high": self.is_new_high,
                "is_breakout": self.is_breakout,
                "ma_aligned": self.ma_aligned,
                "ma_aligned_long": self.ma_aligned_long,
                "good_candle": self.good_candle,
                "upper_wick_long": self.upper_wick_long,
                "has_consolidation": self.has_consolidation,
                "supply_positive": self.supply_positive,
                "retracement_recovery": self.retracement_recovery,
                "pullback_support_confirmed": self.pullback_support_confirmed,
            },
            "negative": {
                "negative_news": self.negative_news,
            },
        }


@dataclass
class NewsData:
    code: str
    title: str
    source: str
    published_at: datetime
    url: Optional[str] = None
    summary: str = ""


@dataclass
class Signal:
    """매매 시그널"""

    # 종목 정보
    stock_code: str
    stock_name: str
    market: str

    # 시그널
    signal_date: date
    grade: Grade

    # 점수
    score: ScoreDetail
    checklist: ChecklistDetail

    # 가격
    current_price: int
    entry_price: int
    stop_price: int
    target_price: int

    # 포지션
    r_value: float
    position_size: int
    quantity: int
    r_multiplier: float = 0.0

    # 시장 데이터
    trading_value: int = 0
    change_pct: float = 0.0
    foreign_5d: int = 0
    inst_5d: int = 0

    # 품질
    quality: float = 0.0

    # 뉴스
    news_items: list[dict] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "market": self.market,
            "signal_date": self.signal_date.isoformat(),
            "grade": self.grade.value,
            "score": self.score.to_dict(),
            "checklist": self.checklist.to_dict(),
            "current_price": self.current_price,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "r_value": self.r_value,
            "position_size": self.position_size,
            "quantity": self.quantity,
            "r_multiplier": self.r_multiplier,
            "trading_value": self.trading_value,
            "change_pct": self.change_pct,
            "foreign_5d": self.foreign_5d,
            "inst_5d": self.inst_5d,
            "quality": self.quality,
            "news_items": self.news_items,
            "themes": self.themes,
        }


@dataclass
class ScreenerResult:
    """스크리너 결과"""
    date: date
    total_candidates: int              # 전체 후보 수
    filtered_count: int                # 필터 통과 수
    signals: list[Signal] = field(default_factory=list)
    by_grade: dict[str, int] = field(default_factory=dict)   # 등급별 개수
    by_market: dict[str, int] = field(default_factory=dict)  # 시장별 개수
    processing_time_ms: float = 0.0    # 소요 시간 (ms)

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "total_candidates": self.total_candidates,
            "filtered_count": self.filtered_count,
            "signals": [s.to_dict() for s in self.signals],
            "by_grade": self.by_grade,
            "by_market": self.by_market,
            "processing_time_ms": self.processing_time_ms,
        }
