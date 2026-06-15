"""지정가 매수 + 5분 타임아웃 — 갭 내 단계적 가격 인상 / 눌림 대기 진입."""
import asyncio
from datetime import datetime, time as dtime

import pytz

from autostock.trading.kis_client import (
    get_current_price,
    get_intraday_5min,
    limit_buy,
    get_order_fill_qty,
    cancel_order,
    round_to_tick,
)
from autostock.logger import get_logger

KST = pytz.timezone("Asia/Seoul")

PULLBACK_WAIT_START    = dtime(9, 15)   # 눌림 대기 시작 (장 안정화 후)
PULLBACK_TIMEOUT       = dtime(9, 45)   # 눌림 미발생 시 포기 시각
PULLBACK_TOLERANCE_PCT = 0.5            # 5MA 대비 ±0.5% 이내면 눌림 도달로 판단

log = get_logger(__name__)


def _get_now_kst() -> dtime:
    """현재 KST 시각 반환 (테스트에서 monkeypatch 대상)."""
    return datetime.now(KST).time().replace(tzinfo=None)


async def _wait_for_pullback(ticker: str) -> tuple[bool, int]:
    """09:15~09:45 사이 5분봉 5MA 부근 눌림 대기.

    Returns:
        (True, target_price) — 눌림 도달
        (False, 0)           — 09:45 초과로 포기
    """
    while True:
        now = _get_now_kst()

        if now >= PULLBACK_TIMEOUT:
            return False, 0

        if now < PULLBACK_WAIT_START:
            await asyncio.sleep(60)
            continue

        bars = get_intraday_5min(ticker, count=5)
        if not bars or len(bars) < 5:
            await asyncio.sleep(60)
            continue

        ma5 = sum(b["close"] for b in bars) / len(bars)
        cur = get_current_price(ticker)
        if cur <= 0 or ma5 <= 0:
            await asyncio.sleep(60)
            continue

        gap_pct = abs((cur / ma5 - 1) * 100)
        if gap_pct <= PULLBACK_TOLERANCE_PCT:
            return True, round_to_tick(ma5)

        await asyncio.sleep(60)


async def limit_buy_with_pullback(
    ticker: str,
    qty: int,
    price_reference: float = 0.0,
) -> tuple[int, int]:
    """눌림 대기 후 5MA 지정가 매수.

    09:15~09:45 사이 현재가가 5분봉 5MA ±0.5% 이내로 눌릴 때까지 대기.

    Returns:
        (filled_qty, fill_price) — 실제 체결 수량과 체결가.
        미체결/포기 시 (0, 0). 부분 체결 시 잔량은 취소하고 (부분수량, 체결가) 반환.
    """
    success, target = await _wait_for_pullback(ticker)
    if not success:
        log.info("[%s] 눌림 미발생 (09:15~09:45) — 추격매수 포기", ticker)
        return 0, 0

    log.info("[%s] 눌림 도달 — 5MA %d 지정가 진입 qty=%d", ticker, target, qty)
    try:
        data = limit_buy(ticker, qty, target)
    except Exception as e:
        log.error("[%s] limit_buy 실패: %s", ticker, e)
        return 0, 0

    order_no = data.get("output", {}).get("ODNO", "")
    if not order_no:
        log.error("[%s] ODNO 없음 — 응답: %s", ticker, data)
        return 0, 0

    # 5분 체결 대기 (30초 × 10)
    for _ in range(10):
        await asyncio.sleep(30)
        filled = get_order_fill_qty(order_no)
        if filled >= qty:
            log.info("[%s] 눌림 체결 완료: qty=%d price=%d", ticker, qty, target)
            return qty, target

    # 타임아웃 — 부분 체결분 확정 후 잔량 취소
    filled = get_order_fill_qty(order_no)
    cancel_order(order_no, ticker, qty, target)
    if filled > 0:
        log.warning("[%s] 눌림 부분 체결 %d/%d — 부분 수량으로 진입 (잔량 취소)", ticker, filled, qty)
        return filled, target
    log.warning("[%s] 눌림 체결 타임아웃 — 미체결 주문 취소 (ODNO=%s)", ticker, order_no)
    return 0, 0


POLL_INTERVAL_SEC = 30
ENTRY_DISCOUNT    = 0.995   # 1차 시도: 현재가 대비 0.5% 아래
RETRY_COUNT       = 2       # 최대 시도 횟수


def _build_price_ladder(cur: float, price_reference: float) -> list[int]:
    """
    매수 시도 가격 목록 생성.
    - price_reference > cur: 갭을 (RETRY_COUNT+1) 등분해 단계적으로 인상
    - 그 외: 단일 시도 (현재가 × ENTRY_DISCOUNT)
    """
    base = round_to_tick(cur * ENTRY_DISCOUNT)
    if price_reference > cur:
        gap  = price_reference - cur
        step = gap / (RETRY_COUNT + 1)
        prices = [round_to_tick(cur * ENTRY_DISCOUNT + step * i) for i in range(RETRY_COUNT)]
        # 마지막 가격이 AI 목표가를 초과하지 않도록 클램프
        cap = round_to_tick(price_reference)
        prices = [min(p, cap) for p in prices]
        # 중복 제거 (tick 정렬 후 같아지는 경우)
        seen: set[int] = set()
        unique: list[int] = []
        for p in prices:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        return unique
    return [base]


async def limit_buy_with_timeout(
    ticker: str,
    qty: int,
    price_reference: float = 0.0,
    timeout_sec: int = 300,
) -> tuple[int, int]:
    """
    지정가 매수, 최대 RETRY_COUNT회 시도, 전체 timeout_sec(기본 5분) 이내.

    price_reference(AI 목표가) > 현재가이면 갭 내에서 가격을 단계적으로 인상.
    각 시도는 '잔여 수량'만 주문하며, 부분 체결분은 누적한다.

    Returns:
        (filled_qty, fill_price) — 실제 누적 체결 수량과 마지막 체결가.
        전혀 체결 안 되면 (0, 마지막가).
    """
    cur = get_current_price(ticker)
    if cur <= 0:
        log.error("[%s] 현재가 조회 실패 — 지정가 매수 불가", ticker)
        return 0, 0

    prices   = _build_price_ladder(cur, price_reference)
    slot_sec = timeout_sec // len(prices)

    log.info(
        "[%s] 지정가 매수 계획: %d회 시도 %s (현재가=%.0f ref=%.0f slot=%ds)",
        ticker, len(prices), prices, cur, price_reference, slot_sec,
    )

    total_filled = 0
    remaining    = qty
    last_price   = prices[-1]

    for attempt, price in enumerate(prices, 1):
        last_price = price
        log.info("[%s] 시도 %d/%d: qty=%d price=%d", ticker, attempt, len(prices), remaining, price)
        try:
            data = limit_buy(ticker, remaining, price)
        except Exception as e:
            log.error("[%s] limit_buy 실패 (시도 %d): %s", ticker, attempt, e)
            continue

        order_no = data.get("output", {}).get("ODNO", "")
        if not order_no:
            log.error("[%s] ODNO 없음 — 응답: %s", ticker, data)
            continue

        elapsed = 0
        while elapsed < slot_sec:
            await asyncio.sleep(POLL_INTERVAL_SEC)
            elapsed += POLL_INTERVAL_SEC
            filled = get_order_fill_qty(order_no)
            log.debug("[%s] 체결 조회 %d/%d (%ds)", ticker, filled, remaining, elapsed)
            if filled >= remaining:
                total_filled += filled
                log.info("[%s] 체결 완료: 누적 qty=%d price=%d (시도 %d)", ticker, total_filled, price, attempt)
                return total_filled, price

        # 슬롯 내 미체결/부분체결 → 부분분 확정, 잔량 취소 후 다음 가격으로
        filled = get_order_fill_qty(order_no)
        cancel_order(order_no, ticker, remaining, price)
        if filled > 0:
            total_filled += filled
            remaining    -= filled
            log.warning("[%s] 시도 %d 부분 체결 %d (누적 %d/%d) — 잔량 취소",
                        ticker, attempt, filled, total_filled, qty)
            if remaining <= 0:
                return total_filled, price
        else:
            log.warning("[%s] 시도 %d 미체결 — 취소 (ODNO=%s)", ticker, attempt, order_no)

    return total_filled, last_price
