"""asyncio Event — Telegram Bot ↔ FastAPI 브릿지."""
import asyncio
from autostock.logger import get_logger

log = get_logger(__name__)

# FastAPI /hitl-response → resume_graph()로 전달되는 응답
# 타임아웃 처리를 위해 asyncio.Event + 결과 저장 구조 사용
_pending: dict[str, asyncio.Event] = {}
_results: dict[str, dict] = {}
_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """main.py 에서 FastAPI 루프 시작 전에 한 번 호출."""
    global _main_loop
    _main_loop = loop


def register(thread_id: str) -> asyncio.Event:
    """HITL 이벤트 등록. FastAPI가 set() 호출 시 깨어남."""
    event = asyncio.Event()
    _pending[thread_id] = event
    _results.pop(thread_id, None)
    return event


def resolve(thread_id: str, status: str, approved_qty: dict) -> None:
    """Telegram 버튼 클릭 또는 /hitl-response → 여기 호출.

    Telegram Bot은 별도 스레드(다른 이벤트 루프)에서 실행되므로
    call_soon_threadsafe 로 메인 루프에 event.set() 을 안전하게 전달한다.
    """
    _results[thread_id] = {"status": status, "approved_qty": approved_qty}
    event = _pending.get(thread_id)
    if event:
        log.info("hitl_state.resolve: thread_id=%s status=%s", thread_id, status)
        if _main_loop is not None and _main_loop.is_running():
            _main_loop.call_soon_threadsafe(event.set)
        else:
            event.set()


def get_result(thread_id: str) -> dict | None:
    return _results.get(thread_id)


def cleanup(thread_id: str) -> None:
    _pending.pop(thread_id, None)
    _results.pop(thread_id, None)
