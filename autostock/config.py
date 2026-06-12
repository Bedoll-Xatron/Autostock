"""전역 설정 — .env 로드 및 상수 정의."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Required env var missing: {key}")
    return val


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, default))


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in ("1", "true", "yes")


# ── AI Provider ─────────────────────────────────────────
AI_PROVIDER: str = os.getenv("AI_PROVIDER", "openai").lower()

# ── OpenAI ──────────────────────────────────────────────
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")

# ── Gemini ──────────────────────────────────────────────
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_BASIC_MODEL: str = os.getenv("GEMINI_BASIC_MODEL", "gemini-2.5-flash")
GEMINI_BOSS_MODEL: str = os.getenv("GEMINI_BOSS_MODEL", "gemini-2.5-pro")

# ── Search APIs (SerpAPI / Tavily) ───────────────────────
SERPAPI_API_KEY: str = os.getenv("SERPAPI_API_KEY", "")
TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")

# ── Supabase ─────────────────────────────────────────────
SUPABASE_URL: str = _require("SUPABASE_URL")
SUPABASE_KEY: str = _require("SUPABASE_KEY")

# ── KIS ──────────────────────────────────────────────────
KIS_APP_KEY: str = _require("KIS_APP_KEY")
KIS_APP_SECRET: str = _require("KIS_APP_SECRET")
KIS_CANO: str = _require("KIS_CANO")
KIS_ACNT_PRDT_CD: str = os.getenv("KIS_ACNT_PRDT_CD", "01")
KIS_SIMULATED_MODE: bool = _bool("KIS_SIMULATED_MODE", True)

KIS_BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"
KIS_BASE_URL_SIM = "https://openapivts.koreainvestment.com:29443"

# ── Telegram ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ── KRX (공공데이터포털) ──────────────────────────────────
KRX_API_KEY: str = os.getenv("krx_API_KEY", "")

# ── FastAPI ──────────────────────────────────────────────
API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
API_PORT: int = _int("API_PORT", 8000)

# ── 스케줄 (KST) ─────────────────────────────────────────
_default_schedule = f"{os.getenv('SCHEDULE_HOUR', 8)}:{os.getenv('SCHEDULE_MINUTE', 30)}"
PIPELINE_SCHEDULES_STR = os.getenv("PIPELINE_SCHEDULES", _default_schedule)

PIPELINE_SCHEDULES: list[tuple[int, int]] = []
for tm in PIPELINE_SCHEDULES_STR.split(","):
    tm = tm.strip()
    if ":" in tm:
        h, m = tm.split(":")
        PIPELINE_SCHEDULES.append((int(h), int(m)))

# ── MarketFlow 연동 ──────────────────────────────────────
MARKETFLOW_DATA_DIR: str = os.getenv(
    "MARKETFLOW_DATA_DIR",
    r"D:\INFORUN\chutzrit\autostock\marketflow\data",
)
MARKETFLOW_SCRIPT: str = os.getenv(
    "MARKETFLOW_SCRIPT",
    r"D:\INFORUN\chutzrit\autostock\marketflow\daily_update.py",
)
MARKETFLOW_HOUR: int = _int("MARKETFLOW_HOUR", 16)
MARKETFLOW_MINUTE: int = _int("MARKETFLOW_MINUTE", 0)

# ── 오후 시그널 스캐너 ────────────────────────────────────
AFTERNOON_SCANNER_HOUR: int = _int("AFTERNOON_SCANNER_HOUR", 13)
AFTERNOON_SCANNER_MINUTE: int = _int("AFTERNOON_SCANNER_MINUTE", 0)
AFTERNOON_SCANNER_SCRIPT: str = os.getenv(
    "AFTERNOON_SCANNER_SCRIPT",
    r"D:\INFORUN\chutzrit\autostock\marketflow\engine\run_afternoon.py",
)

# ── 트레이딩 파라미터 ────────────────────────────────────
REVIEW_COUNT: int = max(3, min(10, _int("REVIEW_COUNT", 3)))
HITL_TIMEOUT_MINUTES: int = _int("HITL_TIMEOUT_MINUTES", 10)
MAX_ANALYSIS_STOCKS: int = _int("MAX_ANALYSIS_STOCKS", 3)
MAX_DEBATE_ROUNDS: int = 2

# ── 수량 결정 기준 (신뢰도 → 잔고 비율) ─────────────────
CONFIDENCE_TIERS: list[tuple[float, float]] = [
    (9.0, 0.15),
    (7.0, 0.10),
    (5.0, 0.05),
]

# ── 포지션 사이징 기준값 ──────────────────────────────────
R_RATIO: float = 0.005           # 가용 자금 대비 기본 R값 (0.5%) — marketflow config.py와 통일
STOP_LOSS_PCT: float = 0.08      # 기본 손절 비율 (8%) — 백테스트 실손절 패턴 반영
