"""FastAPI 엔드포인트."""
from fastapi import APIRouter, BackgroundTasks, HTTPException

from autostock.api.schemas import HitlResponseRequest, RunRequest
from autostock.hitl import hitl_state
from autostock.logger import get_logger

log = get_logger(__name__)
router = APIRouter()


@router.get("/health")
async def health():
    return {"ok": True}


@router.post("/hitl-response")
async def hitl_response(req: HitlResponseRequest, bg: BackgroundTasks):
    """
    Telegram 버튼 클릭 시 호출되는 엔드포인트.
    hitl_state.resolve() → 대기 중인 resume_graph() 깨움.
    """
    log.info("/hitl-response: thread_id=%s status=%s qty=%s",
             req.thread_id, req.status, req.approved_qty)

    if req.status not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="status must be 'approved' or 'rejected'")

    hitl_state.resolve(req.thread_id, req.status, req.approved_qty)
    return {"ok": True}


@router.post("/run")
async def manual_run(req: RunRequest, bg: BackgroundTasks):
    """수동으로 일일 매매 파이프라인을 즉시 실행 (테스트/디버그용)."""
    from autostock.scheduler.jobs import run_daily_pipeline
    bg.add_task(run_daily_pipeline)
    return {"ok": True, "message": "파이프라인 시작됨"}
