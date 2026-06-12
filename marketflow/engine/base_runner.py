"""Shared utilities for run_*.py engine entry points."""
import logging
import os
import sys
from typing import Callable

log = logging.getLogger(__name__)


def ensure_engine_path() -> None:
    """Add the engine directory to sys.path if not already present."""
    engine_dir = os.path.dirname(os.path.abspath(__file__))
    if engine_dir not in sys.path:
        sys.path.insert(0, engine_dir)


def send_telegram_safe(msg: str) -> bool:
    """Send a Telegram message, logging errors instead of raising."""
    try:
        from notifier import send_telegram  # type: ignore[import]
        send_telegram(msg)
        log.info("텔레그램 전송 완료")
        return True
    except ImportError:
        log.debug("notifier 모듈 없음 — 텔레그램 건너뜀")
        return False
    except Exception as e:
        log.warning("텔레그램 전송 실패: %s", e)
        return False


def maybe_send_telegram(
    result: dict,
    build_msg: Callable[[dict], str],
    no_telegram: bool,
) -> None:
    """Send telegram if enabled and result contains at least one signal."""
    if no_telegram:
        return
    if result.get("stats", {}).get("total", 0) == 0:
        return
    send_telegram_safe(build_msg(result))
