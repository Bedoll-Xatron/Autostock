"""
트레일링 손절 감시 — asyncio background task.

3단계 손절 전략:
  Phase 1 (stop): 진입가 대비 ATR 기반 손절선 (종목별 2.5~6%, 기본 3%), +2% 미만 구간에서 유지
  Phase 2 (even): +2% 도달 시 손절가를 진입가(본전)로 1회 인상
  Phase 3 (trail): +5% 돌파 시 peak 기준 2.5% trailing 활성화

  결과: 손실은 최대 -3%로 제한, +2%+ 도달 시 본전 보호, 수익은 5~9%+ 구간에서 실현

동작 규칙:
  - 장 시간(09:00~15:20) 에만 폴링 (30초 간격)
  - Phase 1: price <= entry × 0.97 → 손절 매도
  - Phase 2 진입: price >= entry × 1.02 → stop = entry (본전으로 인상)
  - Phase 3 진입: price >= entry × 1.05 → trailing 활성화 (stop/even 양쪽에서 직행 가능)
  - Phase 3: price > peak → peak 갱신, stop = peak × (1 - PROFIT_TRAIL_PCT)
             price <= stop → 익절 매도
  - 매도 실패 시 trail 폭을 3 → 5 → 7%로 확대하며 최대 3회 재시도 후 텔레그램 알림
  - 15:20 KST 이후 → 잔여 종목 held_positions 저장 (루프는 유지, 다음 날 재개)
  - 서버 재시작 시 held_positions 로드 → 감시 재개

신규 포지션 추가 방법 (서버 재시작 불필요):
  - 동일 프로세스: add_positions(positions) 호출 → 즉시 큐 주입
  - 외부 프로세스: save_held_position() 후 ~5분 내 Supabase 재동기화로 자동 감지
"""
import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from typing import Optional

import pytz

from autostock.market.kr_holidays import is_market_hours, MARKET_OPEN, MARKET_CLOSE
from autostock.trading.kis_client import get_current_price, get_current_price_async, get_holding_qty, market_sell
from autostock.trading import kis_ws_client as ws
from autostock.db import supabase as db
from autostock.hitl import telegram_bot as bot_ui
from autostock.logger import get_logger

log = get_logger(__name__)

KST = pytz.timezone("Asia/Seoul")
POLL_INTERVAL = 30              # 초
DB_RESYNC_INTERVAL = 10         # DB 재동기화 주기 (10 × 30s = 5분)

STOP_LOSS_PCT       = 3.0       # 진입가 대비 고정 손절 (%)
BREAKEVEN_TRIGGER_PCT = 2.0     # 본전 보호 활성화 기준 수익률 (%)
PROFIT_TRIGGER_PCT  = 5.0       # trailing 활성화 기준 수익률 (%)
PROFIT_TRAIL_PCT    = 2.5       # trailing 활성화 후 peak 대비 trail (%)
SELL_RETRY_TRAIL_START = 3.0    # 매도 실패 시 1차 trail 폭 (%)
SELL_RETRY_TRAIL_STEP  = 2.0    # 실패할 때마다 확대 폭 → 3 → 5 → 7
SELL_RETRY_TRAIL_MAX   = 7.0    # 최대 trail 폭 (3차 재시도)
RETRY_WAIT_SEC      = 10        # 재시도 대기 (초)

FAST_TRAIL_PCT        = 4.0     # fast mode trail (%) → 3, 7, 11% 구간
FAST_POLL_INTERVAL    = 10      # fast mode 폴링 간격 (초)
VELOCITY_DROP_TRIGGER = -2.0    # 단일 poll 낙폭 (%) — fast mode 진입
VELOCITY_HISTORY_LEN  = 4       # 연속 하락 검출 히스토리 길이

# W3: 장 시작 갭다운 매도 회피
MORNING_OPEN_END       = dtime(9, 5)   # 버퍼 적용 시간 상한 (09:05 KST)
GAP_DOWN_BUFFER_MIN    = 5             # 갭다운 감지 후 대기 시간 (분)
GAP_DOWN_THRESHOLD_PCT = 2.0           # 손절가 대비 낙폭 임계값 (%)

# W5: Time Stop 파라미터
TIME_STOP_DAYS          = 10           # 보유 일수 기준 (5일 → 10일)
TIME_STOP_THRESHOLD_PCT = -1.0         # 수익률 임계값 — -1% 이하만 청산

# 신규 포지션 주입용 모듈 레벨 큐 (동일 프로세스)
_position_queue: "Optional[asyncio.Queue[TrailingPosition]]" = None
_pos_map_ref: "Optional[dict[str, TrailingPosition]]" = None


def _get_queue() -> "asyncio.Queue[TrailingPosition]":
    global _position_queue
    if _position_queue is None:
        _position_queue = asyncio.Queue()
    return _position_queue


def add_positions(positions: "list[TrailingPosition]") -> None:
    """실행 중인 감시 루프에 신규 포지션을 즉시 주입한다 (서버 재시작 불필요)."""
    if not positions:
        return
    q = _get_queue()
    for pos in positions:
        q.put_nowait(pos)
    names = ", ".join(f"{p.name}({p.ticker})" for p in positions)
    log.info("포지션 큐 주입: %d종목 [%s]", len(positions), names)


@dataclass
class TrailingPosition:
    ticker:      str
    name:        str
    qty:         int
    avg_price:   float          # KIS 보유 평균가
    entry_price: float          # 최초 진입가 (고정 손절 기준)
    stop_price:  float          # 현재 적용 손절가
    peak_price:  float          # 감시 중 최고가
    stop_pct:     float = 3.0    # 종목별 ATR 손절폭 (%)
    phase:        str  = 'stop'  # 'stop' | 'even' | 'trail'
    entry_date:   str  = field(default_factory=lambda: datetime.now().strftime('%Y-%m-%d'))
    fast_mode:    bool = field(default=False)
    price_history: list = field(default_factory=list)
    gap_down_hold_until: "Optional[datetime]" = field(default=None)

    def to_dict(self) -> dict:
        return {
            "ticker":      self.ticker,
            "name":        self.name,
            "qty":         self.qty,
            "avg_price":   self.avg_price,
            "entry_price": self.entry_price,
            "stop_price":  self.stop_price,
            "peak_price":  self.peak_price,
            "stop_pct":    self.stop_pct,
            "phase":       self.phase,
            "entry_date":  self.entry_date,
        }


def _now_kst() -> dtime:
    return datetime.now(KST).time().replace(tzinfo=None)


def _fixed_stop(entry: float, stop_pct: float = STOP_LOSS_PCT) -> int:
    from autostock.trading.kis_client import round_to_tick
    return round_to_tick(entry * (1 - stop_pct / 100))


def _trail_stop(peak: float, fast: bool = False) -> int:
    from autostock.trading.kis_client import round_to_tick
    pct = FAST_TRAIL_PCT if fast else PROFIT_TRAIL_PCT
    return round_to_tick(peak * (1 - pct / 100))


def _detect_velocity(pos: "TrailingPosition", price: float) -> bool:
    """fast mode 활성화: 단일 poll에서 -2% 이상 낙폭 OR 연속 3회 하락."""
    h = pos.price_history
    h.append(price)
    if len(h) > VELOCITY_HISTORY_LEN:
        h.pop(0)
    if len(h) >= 2:
        if (price / h[-2] - 1) * 100 <= VELOCITY_DROP_TRIGGER:
            return True
    if len(h) >= 3 and h[-1] < h[-2] < h[-3]:
        return True
    return False


def _check_gap_down_buffer(pos: "TrailingPosition", price: float) -> bool:
    """W3: 장 시작 갭다운 감지 시 즉시 손절 대신 버퍼 대기 여부를 반환한다.

    반환값 True  → 이번 폴 손절 스킵 (대기 중 또는 버퍼 새로 시작)
    반환값 False → 정상 손절 진행
    """
    now_dt = datetime.now(KST)
    now_t  = now_dt.time().replace(tzinfo=None)

    # 대기 중 — 만료 여부 확인
    if pos.gap_down_hold_until is not None:
        if now_dt < pos.gap_down_hold_until:
            return True   # 아직 대기 시간
        pos.gap_down_hold_until = None   # 만료 → 손절 진행
        return False

    # 장 시작 직후(09:00~09:05)이고 손절가 대비 낙폭이 임계값 이상일 때만 버퍼 적용
    if now_t > MORNING_OPEN_END:
        return False

    gap_pct = (pos.stop_price / price - 1) * 100
    if gap_pct < GAP_DOWN_THRESHOLD_PCT:
        return False

    resume_dt = now_dt + timedelta(minutes=GAP_DOWN_BUFFER_MIN)
    pos.gap_down_hold_until = resume_dt
    log.info(
        "[%s] 갭다운 회피(W3) — 손절가 %.0f 대비 %.1f%% 하락, %d분 버퍼 시작 (재평가 %s KST)",
        pos.ticker, pos.stop_price, gap_pct, GAP_DOWN_BUFFER_MIN,
        resume_dt.strftime("%H:%M:%S"),
    )
    bot_ui.schedule_message(
        f"⏸ <b>{pos.name}({pos.ticker})</b> 갭다운 회피(W3)\n"
        f"손절가 {pos.stop_price:,.0f} 대비 {gap_pct:.1f}% 하락\n"
        f"재평가: {resume_dt.strftime('%H:%M')} KST ({GAP_DOWN_BUFFER_MIN}분 버퍼)"
    )
    return True


def _save_remaining(pos_map: dict[str, "TrailingPosition"]) -> None:
    for pos in pos_map.values():
        db.save_held_position(pos.to_dict())


def _auto_sell_allowed() -> tuple[bool, str]:
    """자동 손절 매도 허용 여부 + 모드 문자열.

    안전핀: 모의투자는 항상 허용. 실거래(KIS_SIMULATED_MODE=false)는
    REAL_AUTO_SELL_CONFIRMED=true 환경변수가 명시될 때만 허용한다.
    실거래 전환 시 사용자가 의식적으로 안전핀을 풀어야 자동 매도가 동작.
    """
    from autostock.trading.kis_client import get_kis_simulated
    if get_kis_simulated():
        return True, "simulated"
    if os.getenv("REAL_AUTO_SELL_CONFIRMED", "false").lower() == "true":
        return True, "real_confirmed"
    return False, "real_unconfirmed"


async def _sell_with_retry(pos: "TrailingPosition", trigger_price: float, reason: str) -> bool:
    """손절/익절/Time stop 트리거 시 실제 시장가 매도.

    실패 시 trail 폭을 3 → 5 → 7%로 확대하며 최대 3회 재시도 (macmini 방식 복원).
    확대 시 손절가를 peak 기준으로 더 넓게 갱신하고 텔레그램으로 재시도를 알린다.

    안전핀: 실거래에서 REAL_AUTO_SELL_CONFIRMED 미설정이면 매도하지 않고 False를
    반환한다 (호출부가 pending_sell + 수동 매도 알림으로 폴백).

    Returns:
        True  — 매도 성공 (또는 이미 보유 0)
        False — 안전핀 미해제 / 매도 전량 실패 (수동 폴백 필요)
    """
    from autostock.trading.kis_client import round_to_tick

    allowed, mode = _auto_sell_allowed()
    if not allowed:
        log.warning(
            "[%s] %s 트리거 — 실거래 자동 손절 안전핀 미해제(REAL_AUTO_SELL_CONFIRMED) "
            "→ 자동 매도 보류, 수동 매도 필요", pos.ticker, reason,
        )
        return False

    # 실제 보유 수량 확인 (유령 포지션 오매도 방지)
    qty = await asyncio.to_thread(get_holding_qty, pos.ticker)
    if qty <= 0:
        log.info("[%s] 보유 수량 0 — 매도 스킵 (이미 정리됨)", pos.ticker)
        return True

    original_stop = pos.stop_price  # M5: 확대 후 전량 실패 시 원복용
    trail = SELL_RETRY_TRAIL_START
    attempt = 0
    while trail <= SELL_RETRY_TRAIL_MAX:
        attempt += 1
        try:
            await asyncio.to_thread(market_sell, pos.ticker, qty)
            log.info("[%s] %s 시장가 매도 성공 (시도 %d, trail=%.0f%%, qty=%d, mode=%s, 현재가=%.0f)",
                     pos.ticker, reason, attempt, trail, qty, mode, trigger_price)
            return True
        except Exception as e:
            next_trail = trail + SELL_RETRY_TRAIL_STEP
            if next_trail > SELL_RETRY_TRAIL_MAX:
                log.error("[%s] %s 매도 최종 실패 (시도 %d, trail=%.0f%%): %s — 손절가 원복",
                          pos.ticker, reason, attempt, trail, e)
                pos.stop_price = original_stop  # 확대분 잔류 방지(다음 트리거 지연 방지)
                return False
            log.error(
                "[%s] %s 매도 실패 (시도 %d, trail=%.0f%%): %s — %.0f%%로 확대 후 %d초 대기",
                pos.ticker, reason, attempt, trail, e, next_trail, RETRY_WAIT_SEC,
            )
            # 손절 폭 확대 반영 (peak 기준)
            widened = round_to_tick(pos.peak_price * (1 - next_trail / 100))
            pos.stop_price = widened
            db.update_held_position(pos.ticker, trigger_price, widened, pos.phase)
            bot_ui.schedule_message(
                f"⚠️ <b>{pos.name}({pos.ticker})</b> 손절 {attempt}차 실패\n"
                f"trail {trail:.0f}% → {next_trail:.0f}% 확대 재시도 중...",
                throttle_key=f"sellfail_{pos.ticker}",
            )
            await asyncio.sleep(RETRY_WAIT_SEC)
            trail = next_trail
    return False


async def _execute_sell_or_fallback(
    pos: "TrailingPosition", price: float, reason: str, emoji: str,
) -> bool:
    """매도 트리거 처리. 자동 매도 성공 시 DB 정리 후 True, 실패/안전핀 시 pending_sell 폴백 후 False.

    Returns:
        True  — 매도 완료 (호출부에서 감시 목록 제거)
        False — 수동 매도 대기 (pending_sell 마킹됨)
    """
    pnl_pct = (price / pos.entry_price - 1) * 100 if pos.entry_price > 0 else 0.0
    sold = await _sell_with_retry(pos, price, reason)
    if sold:
        db.delete_held_position(pos.ticker)
        bot_ui.schedule_message(
            f"{emoji} <b>{pos.name}({pos.ticker})</b> {reason} 자동 매도 완료\n"
            f"진입가 {pos.entry_price:,.0f} → 매도 {price:,.0f} ({pnl_pct:+.1f}%)",
            throttle_key=f"sold_{pos.ticker}",
        )
        return True

    pos.phase = 'pending_sell'
    db.update_held_position(pos.ticker, price, pos.stop_price, 'pending_sell')
    bot_ui.schedule_message(
        f"{emoji} <b>{pos.name}({pos.ticker})</b> {reason} 라인 도달 — ⚠️ 자동 매도 보류/실패\n"
        f"진입가 {pos.entry_price:,.0f} → 현재가 {price:,.0f} ({pnl_pct:+.1f}%)\n"
        f"수동 매도 후 잔고 0 확인 시 자동 정리",
        throttle_key=f"stop_{pos.ticker}",
    )
    return False


async def _run_sell_task(
    pos: "TrailingPosition", price: float, reason: str, emoji: str,
    selling: set[str], sold: set[str],
) -> None:
    """매도 트리거를 백그라운드로 실행 (감시 루프 비블로킹).

    매도 성공 시 ticker를 sold에 등록해 메인 루프가 pos_map에서 제거하게 한다.
    완료 시 selling에서 해제하여 다음 트리거가 가능하도록 한다.
    """
    try:
        if await _execute_sell_or_fallback(pos, price, reason, emoji):
            sold.add(pos.ticker)
    except Exception as e:
        log.error("[%s] 매도 태스크 예외: %s", pos.ticker, e)
    finally:
        selling.discard(pos.ticker)


async def watch_trailing_stops(positions: list["TrailingPosition"]) -> None:
    """
    단일 장기 실행 감시 루프.

    - initial positions: 서버 시작 시 직접 전달
    - 신규 포지션: add_positions() 큐 주입 (동일 프로세스 즉시 반영)
    - 외부 프로세스 매수(manual_buy 등): 5분마다 Supabase 재동기화로 자동 감지
    - 장 마감(15:20) 후 pos_map 저장 후 초기화 — 루프는 유지해 다음 날 재개
    """
    pos_map: dict[str, TrailingPosition] = {p.ticker: p for p in positions}
    global _pos_map_ref
    _pos_map_ref = pos_map
    outside_market = not is_market_hours()

    if pos_map:
        names_str = ", ".join(f"{p.name}({p.ticker})" for p in positions)
        log.info("트레일링 손절 감시 시작: %d종목 [%s]", len(pos_map), names_str)
        if not outside_market:
            async def _notify_watch_start(msg: str) -> None:
                await asyncio.sleep(12)  # 텔레그램 봇 폴링 준비 대기
                bot_ui.schedule_message(msg)
            asyncio.create_task(_notify_watch_start(
                f"📈 손절 감시 시작 — {names_str}\n"
                f"손절: 진입가 -{STOP_LOSS_PCT:.0f}% | "
                f"+{BREAKEVEN_TRIGGER_PCT:.0f}% 본전 보호 | "
                f"+{PROFIT_TRIGGER_PCT:.0f}% trailing 활성화"
            ))
    else:
        log.info("트레일링 손절 감시 대기 중 (초기 포지션 없음 — 큐/DB 동기화 대기)")

    # 서버 재시작 시 이미 장 마감 이후면 알림 생략 (재시작마다 중복 전송 방지)
    close_notified = not is_market_hours() and _now_kst() >= MARKET_CLOSE
    tick = 0

    # 비블로킹 매도 상태: 매도 진행 중(selling)/완료(sold) ticker 추적
    _selling: set[str] = set()
    _sold: set[str] = set()

    while True:
        # ── 1. 큐에서 신규 포지션 흡수 (동일 프로세스 즉시 주입) ──
        q = _get_queue()
        new_from_queue: list[TrailingPosition] = []
        while not q.empty():
            try:
                pos = q.get_nowait()
                if pos.ticker not in pos_map:
                    pos_map[pos.ticker] = pos
                    ws.subscribe_ticker(pos.ticker)
                    new_from_queue.append(pos)
                    db.save_held_position(pos.to_dict())
            except asyncio.QueueEmpty:
                break
        if new_from_queue:
            names = ", ".join(f"{p.name}({p.ticker})" for p in new_from_queue)
            log.info("큐 주입 — 신규 %d종목 추가: [%s]", len(new_from_queue), names)
            throttle_key = f"queue_{'_'.join(p.ticker for p in new_from_queue)}"
            bot_ui.schedule_message(
                f"📥 신규 포지션 추가 — {names}\n"
                f"손절: 진입가 -{STOP_LOSS_PCT:.0f}%",
                throttle_key=throttle_key,
            )

        # ── 2. 주기적 Supabase 재동기화 (외부 프로세스 매수 자동 감지) ──
        tick += 1
        if tick % DB_RESYNC_INTERVAL == 0 and is_market_hours():
            try:
                db_rows = db.fetch_held_positions()
                for r in db_rows:
                    t = r["ticker"]
                    if t not in pos_map:
                        entry = float(r.get("entry_price") or r["avg_price"])
                        new_pos = TrailingPosition(
                            ticker=t,
                            name=r.get("name", ""),
                            qty=int(r["qty"]),
                            avg_price=float(r["avg_price"]),
                            entry_price=entry,
                            stop_price=float(r["stop_price"]),
                            peak_price=float(r["peak_price"]),
                            stop_pct=float(r.get("stop_pct") or 3.0),
                            phase=r.get("phase", "stop"),
                            entry_date=r.get("entry_date", datetime.now().strftime('%Y-%m-%d')),
                        )
                        pos_map[t] = new_pos
                        ws.subscribe_ticker(t)
                        log.info("[%s] DB 재동기화 — 신규 포지션 자동 감지", t)
                        bot_ui.schedule_message(
                            f"📥 *{new_pos.name}({t})* 자동 감지\n"
                            f"진입가 {new_pos.entry_price:,.0f} | stop {new_pos.stop_price:,.0f}",
                            throttle_key=f"detect_{t}",
                        )
            except Exception as e:
                log.warning("Supabase 재동기화 실패: %s", e)

        any_fast = any(p.fast_mode for p in pos_map.values())
        await asyncio.sleep(FAST_POLL_INTERVAL if any_fast else POLL_INTERVAL)

        # ── 3. 장 외 시간 처리 ──────────────────────────────────
        if not is_market_hours():
            now = _now_kst()
            if pos_map and now >= MARKET_CLOSE and not close_notified:
                _save_remaining(pos_map)
                saved = list(pos_map.keys())
                log.info("장 마감 — 잔여 %d종목 held_positions 저장: %s", len(saved), saved)
                bot_ui.schedule_message(f"💾 장 마감 — {', '.join(saved)} 내일 감시 재개")
                for t in saved:
                    ws.unsubscribe_ticker(t)
                pos_map.clear()
                close_notified = True
            continue

        close_notified = False  # 장 개장 시 플래그 초기화

        if not pos_map:
            continue

        # ── 3.5. 백그라운드 매도 완료분 정리 (pos_map 안전 제거) ──
        if _sold:
            for t in list(_sold):
                ws.unsubscribe_ticker(t)
                pos_map.pop(t, None)
            log.info("매도 완료 정리: %s", list(_sold))
            _sold.clear()

        # ── 4. 보유 종목 폴링 (WS 우선, HTTP 병렬 fallback) ──────
        to_remove: list[str] = []
        tickers = [t for t in pos_map.keys() if t not in _selling]  # 매도 진행 중 종목 제외

        # WS 캐시 우선, 없으면 HTTP 병렬 조회
        prices: dict[str, float] = {t: ws.get_ws_price(t) for t in tickers}
        http_needed = [t for t in tickers if prices[t] <= 0]
        if http_needed:
            http_results = await asyncio.gather(
                *(get_current_price_async(t) for t in http_needed),
                return_exceptions=True,
            )
            for t, r in zip(http_needed, http_results):
                if isinstance(r, (int, float)) and r > 0:
                    prices[t] = float(r)

        for ticker in tickers:
            pos = pos_map[ticker]
            try:
                price = prices.get(ticker, 0.0)
                if price <= 0:
                    log.warning("[%s] 현재가 조회 실패 — 스킵", ticker)
                    continue

                # ── pending_sell: KIS 잔고 0 확인 후 자동 정리 ──────
                if pos.phase == 'pending_sell':
                    qty = await asyncio.to_thread(get_holding_qty, ticker)
                    if qty == 0:
                        db.delete_held_position(ticker)
                        bot_ui.schedule_message(
                            f"✅ *{pos.name}({ticker})* 매도 확인 — 자동 정리 완료"
                        )
                        to_remove.append(ticker)
                    continue

                # ── velocity 감지 → fast mode 전환 ──────────────
                if not pos.fast_mode and _detect_velocity(pos, price):
                    pos.fast_mode = True
                    log.info("[%s] Fast mode 진입 (trail %.1f%% → %.1f%%)",
                             ticker, PROFIT_TRAIL_PCT, FAST_TRAIL_PCT)
                    bot_ui.schedule_message(
                        f"⚡ *{pos.name}({ticker})* fast mode 진입\n"
                        f"trail stop: {PROFIT_TRAIL_PCT:.1f}% → {FAST_TRAIL_PCT:.1f}%"
                    )

                # ── Phase 전환: stop/even → trail (우선 체크) ────
                if pos.phase in ('stop', 'even') and price >= pos.entry_price * (1 + PROFIT_TRIGGER_PCT / 100):
                    new_stop = _trail_stop(price, fast=pos.fast_mode)
                    pos.phase = 'trail'
                    pos.stop_price = max(pos.stop_price, new_stop)
                    pos.peak_price = price
                    db.update_held_position(ticker, price, pos.stop_price, 'trail')
                    gain_pct = (price / pos.entry_price - 1) * 100
                    trail_pct = FAST_TRAIL_PCT if pos.fast_mode else PROFIT_TRAIL_PCT
                    log.info(
                        "[%s] Phase trail 전환 (+%.1f%%) peak=%.0f stop=%.0f",
                        ticker, gain_pct, price, pos.stop_price,
                    )
                    bot_ui.schedule_message(
                        f"🟢 *{pos.name}({ticker})* trailing 활성화\n"
                        f"진입가 {pos.entry_price:,.0f} → 현재 {price:,.0f} (+{gain_pct:.1f}%)\n"
                        f"trailing stop: {pos.stop_price:,.0f} (peak -{trail_pct:.1f}%)"
                    )

                # ── Phase 전환: stop → even (본전 보호) ──────────
                elif pos.phase == 'stop' and price >= pos.entry_price * (1 + BREAKEVEN_TRIGGER_PCT / 100):
                    pos.phase = 'even'
                    pos.stop_price = pos.entry_price
                    pos.peak_price = price
                    db.update_held_position(ticker, price, pos.stop_price, 'even')
                    gain_pct = (price / pos.entry_price - 1) * 100
                    log.info(
                        "[%s] Phase even 전환 (+%.1f%%) stop=본전 %.0f",
                        ticker, gain_pct, pos.stop_price,
                    )
                    bot_ui.schedule_message(
                        f"🔵 *{pos.name}({ticker})* 본전 보호 활성화\n"
                        f"진입가 {pos.entry_price:,.0f} → 현재 {price:,.0f} (+{gain_pct:.1f}%)\n"
                        f"손절가 본전으로 인상: {pos.stop_price:,.0f}"
                    )

                # ── Phase trail: peak 갱신 ────────────────────
                elif pos.phase == 'trail' and price > pos.peak_price:
                    new_stop = _trail_stop(price, fast=pos.fast_mode)
                    if new_stop > pos.stop_price:
                        old_stop = pos.stop_price
                        pos.stop_price = new_stop
                        db.update_held_position(ticker, price, new_stop, 'trail')
                        log.info("[%s] peak 갱신 %.0f → stop %.0f → %.0f",
                                 ticker, price, old_stop, new_stop)
                    pos.peak_price = price

                # ── 손절/익절 트리거 → 자동 매도 (실패/안전핀 시 수동 폴백) ──
                if price <= pos.stop_price and _check_gap_down_buffer(pos, price):
                    continue  # W3: 갭다운 버퍼 대기 중
                if price <= pos.stop_price:
                    pnl_pct = (price / pos.entry_price - 1) * 100
                    prev_phase = pos.phase
                    reason = "익절" if prev_phase == 'trail' else ("본전회수" if prev_phase == 'even' else "손절")
                    emoji = "🟡" if prev_phase == 'trail' else ("🟠" if prev_phase == 'even' else "🔴")
                    log.warning(
                        "[%s] %s 트리거 — 현재가=%.0f ≤ stop=%.0f (P&L=%.1f%%) → 자동 매도 시도",
                        ticker, reason, price, pos.stop_price, pnl_pct,
                    )
                    # 비블로킹: 매도(최대 3회 재시도)를 백그라운드로 분리해 다른 종목 감시 지속
                    _selling.add(ticker)
                    asyncio.create_task(_run_sell_task(pos, price, reason, emoji, _selling, _sold))

                # ── 시간 제한 손절 (Time Stop): TIME_STOP_DAYS 경과 & 수익 ≤ 임계값 ──
                else:
                    e_date = datetime.strptime(pos.entry_date, "%Y-%m-%d").date()
                    days_held = (datetime.now(KST).date() - e_date).days
                    profit_pct = (price / pos.entry_price - 1) * 100

                    if days_held >= TIME_STOP_DAYS and profit_pct <= TIME_STOP_THRESHOLD_PCT:
                        reason = "시간해제 (Time Stop)"
                        log.warning("[%s] %d일 경과 수익률 %.1f%% 저조 — %s 트리거 → 자동 매도 시도",
                                    ticker, days_held, profit_pct, reason)
                        _selling.add(ticker)
                        asyncio.create_task(_run_sell_task(pos, price, reason, "⏳", _selling, _sold))
            except Exception as e:
                log.error("[%s] 감시 중 예외 발생: %s", ticker, e)

        for t in to_remove:
            ws.unsubscribe_ticker(t)
            pos_map.pop(t, None)

    log.info("트레일링 손절 감시 종료")


async def liberate_capital(target_ratio: float = 0.30) -> None:
    """
    가용 잔고가 총 운용 자본의 target_ratio 미만일 때,
    손실률 높은 포지션부터 pending_sell 마킹해 목표 잔고 확보 요청.
    """
    from autostock.trading.kis_client import get_available_cash, get_all_holdings

    try:
        cash = await asyncio.to_thread(get_available_cash)
        holdings = await asyncio.to_thread(get_all_holdings)
    except Exception as e:
        log.error("liberate_capital: 잔고 조회 실패: %s", e)
        return

    if cash is None:
        log.error("liberate_capital: 잔고 조회 실패 (API 오류) — 손절 스킵")
        return

    if not holdings:
        log.warning("liberate_capital: 보유 포지션 없음 — 자본 확보 불가")
        bot_ui.schedule_message(f"⚠️ 잔고 부족 — 보유 포지션 없음\n현재 잔고: {cash:,.0f}원")
        return

    # 현재가 조회 (WS 우선, HTTP 병렬 fallback)
    tickers = [h["ticker"] for h in holdings]
    prices: dict[str, float] = {t: ws.get_ws_price(t) for t in tickers}
    http_needed = [t for t in tickers if prices[t] <= 0]
    if http_needed:
        results = await asyncio.gather(
            *(get_current_price_async(t) for t in http_needed),
            return_exceptions=True,
        )
        for t, r in zip(http_needed, results):
            if isinstance(r, (int, float)) and r > 0:
                prices[t] = float(r)

    # 총 운용 자본 = 가용 현금 + 보유 시가
    holding_value = sum(
        prices.get(h["ticker"], h["avg_price"]) * h["qty"] for h in holdings
    )
    total_capital = cash + holding_value
    target_cash = total_capital * target_ratio

    if cash >= target_cash:
        return

    shortfall = target_cash - cash
    log.info(
        "자본 확보 시작: 현재 잔고 %.0f / 목표 %.0f (총 자본 %.0f의 %.0f%%)",
        cash, target_cash, total_capital, target_ratio * 100,
    )

    # 손실률 오름차순 정렬 (손실 큰 종목 우선)
    def _loss_key(h: dict) -> float:
        p = prices.get(h["ticker"], h["avg_price"])
        return (p / h["avg_price"] - 1) * 100 if h["avg_price"] > 0 else 0.0

    candidates = sorted(holdings, key=_loss_key)
    pos_map = _pos_map_ref or {}
    freed = 0.0
    marked: list[str] = []

    for h in candidates:
        if freed >= shortfall:
            break
        ticker = h["ticker"]
        pos = pos_map.get(ticker)
        if pos is None or pos.phase == 'pending_sell':
            continue

        price = prices.get(ticker, h["avg_price"])
        loss_pct = (price / pos.entry_price - 1) * 100
        sell_value = price * pos.qty

        log.warning(
            "[%s] 자본 확보 손절 마킹: 현재가 %.0f / 진입가 %.0f (%.1f%%) 예상 %.0f원",
            ticker, price, pos.entry_price, loss_pct, sell_value,
        )
        pos.phase = 'pending_sell'
        db.update_held_position(ticker, price, pos.stop_price, 'pending_sell')
        bot_ui.schedule_message(
            f"💸 *{pos.name}({ticker})* 자본 확보 손절\n"
            f"진입가 {pos.entry_price:,.0f} → 현재 {price:,.0f} ({loss_pct:+.1f}%)\n"
            f"⚠️ 수동 매도 후 잔고 0 확인 시 자동 정리"
        )
        freed += sell_value
        marked.append(ticker)

    if not marked:
        log.warning("자본 확보: 손절 가능 포지션 없음 (잔고 %.0f, 목표 %.0f)", cash, target_cash)
        bot_ui.schedule_message(
            f"⚠️ 잔고 부족 — 손절 가능 포지션 없음\n"
            f"현재 잔고: {cash:,.0f}원 / 목표: {target_cash:,.0f}원"
        )
    else:
        log.info(
            "자본 확보 손절 마킹 완료: %d종목 %s (예상 %.0f원)",
            len(marked), marked, freed,
        )
        bot_ui.schedule_message(
            f"📢 자본 확보 요청: {len(marked)}종목 매도 필요\n"
            f"목표 확보액: {shortfall:,.0f}원 / 예상 확보: {freed:,.0f}원\n"
            f"매도 완료 후 다음 매수 기회를 활용하세요"
        )


def purge_phantom_positions() -> int:
    """Supabase held_positions 중 KIS 실제 잔고에 없는 항목을 즉시 삭제한다.

    info-only 모드에서 파이프라인이 쌓아 놓은 유령 포지션을 정리할 때 호출.
    반환값: 삭제된 항목 수.
    """
    from autostock.trading.kis_client import get_all_holdings

    db_rows = db.fetch_held_positions()
    if not db_rows:
        return 0

    try:
        kis_holdings = get_all_holdings()
        kis_tickers = {h["ticker"] for h in kis_holdings}
    except Exception as e:
        log.error("purge_phantom_positions: KIS 잔고 조회 실패 — 정리 스킵: %s", e)
        return 0

    removed = 0
    for r in db_rows:
        ticker = r["ticker"]
        if ticker not in kis_tickers:
            log.warning("[%s] 유령 포지션 즉시 정리 (KIS 잔고 없음)", ticker)
            db.delete_held_position(ticker)
            removed += 1

    if removed:
        log.info("purge_phantom_positions: %d개 유령 포지션 제거 완료", removed)
        from autostock.hitl import telegram_bot as _bot
        _bot.schedule_message(f"🧹 유령 포지션 {removed}개 정리 완료 (KIS 잔고 불일치)")
    return removed


def load_held_positions() -> list["TrailingPosition"]:
    """
    Supabase held_positions에서 이전 포지션 복구.
    KIS 실제 잔고와 대조해 유령 포지션 제거.
    """
    from autostock.trading.kis_client import get_all_holdings
    from autostock.trading import kis_ws_client as _ws

    db_rows = db.fetch_held_positions()
    try:
        kis_holdings = get_all_holdings()
        kis_map = {h["ticker"]: h for h in kis_holdings}
    except Exception as e:
        log.error("KIS 잔고 조회 실패 — DB 데이터만으로 복구: %s", e)
        kis_map = None

    result = []
    monitored = set()

    for r in db_rows:
        ticker = r["ticker"]

        if kis_map is not None:
            if ticker not in kis_map:
                if r.get("phase") == 'pending_sell':
                    log.info("[%s] pending_sell 포지션 매도 확인 — 자동 정리", ticker)
                else:
                    log.warning("[%s] 유령 포지션 제거: KIS 잔고에 없음", ticker)
                db.delete_held_position(ticker)
                continue
            kis_h = kis_map[ticker]
            if int(r["qty"]) != kis_h["qty"]:
                log.info("[%s] 수량 동기화: DB %d → KIS %d", ticker, r["qty"], kis_h["qty"])
                r["qty"] = kis_h["qty"]

        entry = float(r.get("entry_price") or r["avg_price"])
        result.append(TrailingPosition(
            ticker=      ticker,
            name=        r.get("name", ""),
            qty=         int(r["qty"]),
            avg_price=   float(r["avg_price"]),
            entry_price= entry,
            stop_price=  float(r["stop_price"]),
            peak_price=  float(r["peak_price"]),
            stop_pct=    float(r.get("stop_pct") or 3.0),
            phase=       r.get("phase", "stop"),
            entry_date=  r.get("entry_date", datetime.now().strftime('%Y-%m-%d')),
        ))
        _ws.subscribe_ticker(ticker)
        monitored.add(ticker)

    # KIS에 있지만 DB에 없는 종목 자동 추가
    if kis_map is not None:
        from autostock.trading.risk import compute_stop_pct
        for ticker, kis_h in kis_map.items():
            if ticker not in monitored:
                log.info("[%s] 미등록 포지션 자동 추가", ticker)
                entry = kis_h["avg_price"]
                cur = get_current_price(ticker)
                peak = max(entry, cur)
                stop_pct = compute_stop_pct(ticker)
                gain_pct = (cur / entry - 1) * 100 if entry > 0 and cur > 0 else 0.0
                if gain_pct >= PROFIT_TRIGGER_PCT:
                    phase = 'trail'
                    stop = _trail_stop(peak)
                elif gain_pct >= BREAKEVEN_TRIGGER_PCT:
                    phase = 'even'
                    stop = int(entry)
                else:
                    phase = 'stop'
                    stop = _fixed_stop(entry, stop_pct)
                pos = TrailingPosition(
                    ticker=      ticker,
                    name=        kis_h["name"],
                    qty=         kis_h["qty"],
                    avg_price=   entry,
                    entry_price= entry,
                    stop_price=  stop,
                    peak_price=  peak,
                    stop_pct=    stop_pct,
                    phase=       phase,
                    entry_date=  datetime.now().strftime('%Y-%m-%d'),
                )
                result.append(pos)
                _ws.subscribe_ticker(ticker)
                db.save_held_position(pos.to_dict())

    if result:
        log.info("현물 포지션 %d개 감시 시작: %s", len(result), [f"{p.name}({p.ticker})" for p in result])
    return result
