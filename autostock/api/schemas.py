"""FastAPI 요청/응답 스키마."""
from pydantic import BaseModel


class HitlResponseRequest(BaseModel):
    thread_id: str
    status: str                       # "approved" | "rejected"
    approved_qty: dict[str, int] = {} # ticker → 수량


class RunRequest(BaseModel):
    """수동 실행 요청 (테스트/디버그용)."""
    force: bool = False
