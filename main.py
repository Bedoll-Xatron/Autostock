"""
bedoll AutoStock - 진입점 겸 서버 관리 도구

[서버 실행]
  python main.py            # 서버 시작
  python main.py --restart  # 기존 서버 종료 후 재시작
  python main.py --stop     # 서버 중지
  python main.py --status   # 현재 상태 확인

[단일 프로세스 구성]
  1. FastAPI (uvicorn)  - /hitl-response, /health 엔드포인트
  2. Telegram Bot       - HITL 버튼, 워치리스트 관리
  3. APScheduler        - 08:40 KST 일일 파이프라인, 16:00 KST MarketFlow 스캔
"""
import argparse
import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import uvicorn

from autostock.api.app import app
from autostock.hitl.telegram_bot import start_bot_background
from autostock.hitl import hitl_state
from autostock.scheduler.jobs import create_scheduler
from autostock import config
from autostock.logger import get_logger

log = get_logger("main")

BASE_DIR = Path(__file__).parent
PID_FILE = BASE_DIR / "logs" / "server.pid"
DAEMON_PID_FILE = BASE_DIR / "logs" / "daemon.pid"
STOP_SENTINEL = BASE_DIR / "logs" / ".stop"
LOG_FILE = BASE_DIR / "logs" / "server.log"


# ── 프로세스 관리 헬퍼 ──────────────────────────────────────────

def _taskkill(pid: int) -> bool:
    res = subprocess.run(
        f"C:\\Windows\\System32\\taskkill.exe /F /PID {pid}",
        capture_output=True, shell=True,
    )
    return res.returncode == 0


def _is_running(pid: int) -> bool:
    r = subprocess.run(
        f'C:\\Windows\\System32\\tasklist.exe /FI "PID eq {pid}" /FO CSV /NH',
        capture_output=True, shell=True,
    )
    return "python.exe" in r.stdout.decode("cp949", errors="replace")


def _find_all_main_pids() -> list[int]:
    """main.py를 실행 중인 모든 Python 프로세스 PID 반환 (자기 자신 제외)."""
    try:
        r = subprocess.run(
            'C:\\Windows\\System32\\wbem\\wmic.exe process where "name=\'python.exe\'" get ProcessId,CommandLine /format:csv',
            capture_output=True, shell=True,
        )
        output = r.stdout.decode("cp949", errors="replace")
        my_pid = os.getpid()
        pids = []
        for line in output.splitlines():
            if "main.py" not in line:
                continue
            parts = line.strip().split(",")
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[-1].strip())
                if pid and pid != my_pid:
                    pids.append(pid)
            except ValueError:
                pass
        return pids
    except Exception:
        return []


def _tail_log(n: int = 15) -> None:
    if not LOG_FILE.exists():
        return
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-n:]:
            # 시스템 인코딩으로 출력 가능한 것만 출력하거나 에러를 무시
            print(f"  {line}".encode(sys.stdout.encoding, errors='replace').decode(sys.stdout.encoding))
    except Exception:
        pass


def _stop() -> None:
    # 데몬에게 재시작하지 말라고 알린다
    STOP_SENTINEL.parent.mkdir(exist_ok=True)
    STOP_SENTINEL.touch()

    stopped_any = False

    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        if _is_running(pid):
            print(f"[stop] 서버 PID {pid} 종료 중...")
            _taskkill(pid)
            stopped_any = True
        PID_FILE.unlink(missing_ok=True)

    if DAEMON_PID_FILE.exists():
        dpid = int(DAEMON_PID_FILE.read_text().strip())
        if _is_running(dpid):
            print(f"[stop] 데몬 PID {dpid} 종료 중...")
            _taskkill(dpid)
            stopped_any = True
        DAEMON_PID_FILE.unlink(missing_ok=True)

    # PID 파일에 기록되지 않은 고아 프로세스도 정리
    for pid in _find_all_main_pids():
        print(f"[stop] 고아 프로세스 PID {pid} 종료 중...")
        _taskkill(pid)
        stopped_any = True

    if not stopped_any:
        print("[stop] 실행 중인 서버 없음")
    else:
        print("[stop] 완료")


def _status() -> None:
    if not PID_FILE.exists():
        print("[status] 서버 중지 상태")
        return
    pid = int(PID_FILE.read_text().strip())
    if _is_running(pid):
        print(f"[status] 실행 중 - PID {pid}")
        _tail_log(5)
    else:
        print(f"[status] PID {pid} 비정상 종료")
        PID_FILE.unlink(missing_ok=True)


def _run_daemon() -> None:
    """서버 자동 재시작 데몬 — 비정상 종료 시 5초 후 재시작."""
    LOG_FILE.parent.mkdir(exist_ok=True)
    DAEMON_PID_FILE.write_text(str(os.getpid()))
    STOP_SENTINEL.unlink(missing_ok=True)

    # 이미 실행 중인 main.py 프로세스 모두 정리 (고아 프로세스 방지)
    for pid in _find_all_main_pids():
        _taskkill(pid)
    if _find_all_main_pids():
        time.sleep(1)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    restart_count = 0
    try:
        while not STOP_SENTINEL.exists():
            with open(LOG_FILE, "a", encoding="utf-8") as log_f:
                proc = subprocess.Popen(
                    [sys.executable, __file__, "--serve"],
                    cwd=str(BASE_DIR), env=env,
                    stdout=log_f, stderr=log_f,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
                )
            PID_FILE.write_text(str(proc.pid))
            exit_code = proc.wait()
            if STOP_SENTINEL.exists() or exit_code == 0:
                break
            restart_count += 1
            time.sleep(5)
    finally:
        DAEMON_PID_FILE.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
        STOP_SENTINEL.unlink(missing_ok=True)


def _start_background() -> None:
    """데몬 프로세스를 백그라운드로 시작 — 데몬이 서버 자동 재시작을 담당."""
    LOG_FILE.parent.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    with open(LOG_FILE, "a", encoding="utf-8") as log_f:
        daemon_proc = subprocess.Popen(
            [sys.executable, __file__, "--daemon"],
            cwd=str(BASE_DIR),
            env=env,
            stdout=log_f,
            stderr=log_f,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        )

    print(f"[start] 데몬 시작 - PID {daemon_proc.pid} (서버 자동 재시작 활성)")

    time.sleep(3)
    if _is_running(daemon_proc.pid):
        print("[start] 서버 정상 실행 중")
        _tail_log(8)
    else:
        print("[start] 서버가 즉시 종료됨 - 로그를 확인하세요:")
        _tail_log(20)


# ── 실제 서버 로직 (--serve 플래그로 내부 호출) ──────────────────

def _wait_port_free(port: int, timeout_sec: int = 15) -> bool:
    """포트가 해제될 때까지 최대 timeout_sec초 대기. 해제되면 True 반환."""
    for i in range(timeout_sec):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                if i > 0:
                    log.info("포트 %d 해제 확인 (%d초 대기)", port, i)
                return True
        if i == 0:
            log.warning("포트 %d 사용 중 — 최대 %d초 대기", port, timeout_sec)
        time.sleep(1)
    log.error("포트 %d %d초 내 해제 안 됨 — 강제 진행", port, timeout_sec)
    return False


async def _run_fastapi() -> None:
    _wait_port_free(config.API_PORT)
    server_config = uvicorn.Config(
        app=app,
        host=config.API_HOST,
        port=config.API_PORT,
        log_level="warning",
    )
    server = uvicorn.Server(server_config)
    await server.serve()


async def _serve() -> None:
    log.info("bedoll AutoStock 시작")
    log.info("KIS 모드: %s", "모의투자" if config.KIS_SIMULATED_MODE else "실거래")
    log.info("REVIEW_COUNT: %d", config.REVIEW_COUNT)
    schedule_strs = [f"{h:02d}:{m:02d}" for h, m in config.PIPELINE_SCHEDULES]
    log.info("스케줄: 매일 %s KST", ", ".join(schedule_strs))

    hitl_state.set_main_loop(asyncio.get_event_loop())
    start_bot_background()
    log.info("Telegram Bot 스레드 시작")

    mode = "모의투자" if config.KIS_SIMULATED_MODE else "실거래"

    async def _send_startup_notify():
        await asyncio.sleep(8)  # Bot 폴링 루프 준비 대기
        from autostock.hitl import telegram_bot as bot_ui
        bot_ui.schedule_message(f"🟢 AutoStock 서버 시작 ({mode})")

    asyncio.create_task(_send_startup_notify())

    scheduler = create_scheduler()
    scheduler.start()
    log.info("APScheduler 시작")

    # ── 싱글턴 감시 루프 시작 (항상 실행 — 포지션 없어도 큐/DB 대기) ──
    from autostock.trading.trailing_stop import load_held_positions, watch_trailing_stops
    held = load_held_positions()
    if held:
        names = ", ".join(f"{p.name}({p.ticker})" for p in held)
        log.info("이전 포지션 %d개 복구 — 트레일링 손절 재시작: [%s]", len(held), names)
    asyncio.create_task(watch_trailing_stops(held))

    from autostock.trading.breakout_scanner import watch_breakouts
    asyncio.create_task(watch_breakouts())

    from autostock.trading.kis_ws_client import run_price_stream
    asyncio.create_task(run_price_stream())

    await _run_fastapi()


# ── CLI 진입점 ────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="bedoll AutoStock 서버 관리",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예시:\n"
            "  python main.py            서버 시작\n"
            "  python main.py --restart  재시작\n"
            "  python main.py --stop     중지\n"
            "  python main.py --status   상태 확인\n"
        ),
    )
    parser.add_argument("--restart", action="store_true", help="서버 재시작")
    parser.add_argument("--stop",    action="store_true", help="서버 중지")
    parser.add_argument("--status",  action="store_true", help="상태 확인")
    parser.add_argument("--serve",   action="store_true", help=argparse.SUPPRESS)  # 내부용
    parser.add_argument("--daemon",  action="store_true", help=argparse.SUPPRESS)  # 내부용
    args = parser.parse_args()

    if args.serve:
        asyncio.run(_serve())
    elif args.daemon:
        _run_daemon()
    elif args.status:
        _status()
    elif args.stop:
        _stop()
    elif args.restart:
        _stop()
        time.sleep(1)
        _start_background()
    else:
        # 기본: 백그라운드로 시작
        _start_background()


if __name__ == "__main__":
    main()
