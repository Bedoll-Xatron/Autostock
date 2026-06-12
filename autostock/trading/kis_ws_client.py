"""KIS 실시간 주식 현재가 WebSocket 구독 — 가격 캐시 유지.

연결 상태와 무관하게 get_ws_price() 는 항상 호출 가능.
WebSocket 미연결 시 0.0 반환 → trailing_stop 이 HTTP fallback 으로 전환.
"""
import asyncio
import json

import httpx
import websockets

from autostock import config
from autostock.hitl.telegram_bot import get_kis_simulated
from autostock.logger import get_logger

log = get_logger(__name__)

_WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
_WS_URL_SIM  = "ws://ops.koreainvestment.com:31000"
_TR_REALTIME = "H0STCNT0"  # 주식 체결 실시간

_price_cache: dict[str, float] = {}
_subscribed:  set[str]         = set()
_ws_active:   bool             = False


def subscribe_ticker(ticker: str) -> None:
    _subscribed.add(ticker)


def unsubscribe_ticker(ticker: str) -> None:
    _subscribed.discard(ticker)
    _price_cache.pop(ticker, None)


def get_ws_price(ticker: str) -> float:
    return _price_cache.get(ticker, 0.0)


def is_ws_active() -> bool:
    return _ws_active


async def _get_approval_key() -> str:
    base = config.KIS_BASE_URL_SIM if get_kis_simulated() else config.KIS_BASE_URL_REAL
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base}/oauth2/Approval",
            json={
                "grant_type": "client_credentials",
                "appkey":     config.KIS_APP_KEY,
                "secretkey":  config.KIS_APP_SECRET,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["approval_key"]


def _sub_msg(approval_key: str, ticker: str, subscribe: bool) -> str:
    return json.dumps({
        "header": {
            "approval_key": approval_key,
            "custtype":     "P",
            "tr_type":      "1" if subscribe else "2",
            "content-type": "utf-8",
        },
        "body": {"input": {"tr_id": _TR_REALTIME, "tr_key": ticker}},
    })


async def run_price_stream(reconnect_delay: int = 5) -> None:
    """
    KIS 실시간 체결 WebSocket 루프 (싱글턴 long-running task).

    - _subscribed 집합을 폴링하며 동적으로 구독/해제
    - 체결 데이터: pipe-delimited, index 0=ticker, index 2=현재가
    - 연결 끊기면 reconnect_delay 초 후 자동 재연결
    """
    global _ws_active
    ws_url = _WS_URL_SIM if get_kis_simulated() else _WS_URL_REAL

    while True:
        _ws_active = False
        try:
            approval_key = await _get_approval_key()
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                _ws_active = True
                log.info("KIS WebSocket 연결: %s (simulated=%s)", ws_url, get_kis_simulated())

                known: set[str] = set()

                while True:
                    # 신규 구독
                    for t in _subscribed - known:
                        await ws.send(_sub_msg(approval_key, t, subscribe=True))
                        known.add(t)
                        log.debug("WS 구독: %s", t)

                    # 구독 해제
                    for t in list(known - _subscribed):
                        await ws.send(_sub_msg(approval_key, t, subscribe=False))
                        known.discard(t)
                        log.debug("WS 구독 해제: %s", t)

                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=35)
                    except asyncio.TimeoutError:
                        continue

                    # 시스템 응답(JSON)은 무시, 실시간 데이터만 처리
                    if not msg or msg.startswith("{"):
                        continue

                    parts = msg.split("|")
                    if len(parts) >= 3:
                        ticker = parts[0]
                        if ticker in _subscribed:
                            try:
                                price = float(parts[2])
                                if price > 0:
                                    _price_cache[ticker] = price
                            except ValueError:
                                pass

        except Exception as e:
            _ws_active = False
            log.warning("KIS WebSocket 오류 (%s) — %ds 후 재연결", e, reconnect_delay)
            await asyncio.sleep(reconnect_delay)
