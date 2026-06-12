"""Telegram Bot — HITL 승인/거절, 종목별 수량 입력, KIS 모드 토글."""
import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

from autostock import config
from autostock.hitl import hitl_state
from autostock.logger import get_logger

log = get_logger(__name__)

# 런타임 설정 (Telegram 명령어로 변경 가능)
_runtime: dict = {
    "kis_simulated": config.KIS_SIMULATED_MODE,
    "review_count": config.REVIEW_COUNT,
}

def get_kis_simulated() -> bool:
    return _runtime["kis_simulated"]

def get_review_count() -> int:
    return _runtime["review_count"]

_app: Optional[Application] = None
_bot_loop: Optional[asyncio.AbstractEventLoop] = None

# 메시지 쓰로틀 — 파일 기반으로 서버 재시작 후에도 유지
_THROTTLE_PATH = Path(__file__).parents[2] / "data" / "msg_throttle.json"
MSG_THROTTLE_SEC = 4 * 3600  # 동일 키 4시간 이내 재발송 차단


def _throttle_load() -> dict[str, float]:
    try:
        if _THROTTLE_PATH.exists():
            return json.loads(_THROTTLE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _throttle_save(data: dict[str, float]) -> None:
    try:
        _THROTTLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _THROTTLE_PATH.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        log.debug("throttle 파일 저장 실패: %s", e)


def _throttle_check(key: str) -> bool:
    """True 반환 시 발송 차단 (쿨다운 이내)."""
    data = _throttle_load()
    now = time.time()
    if now - data.get(key, 0) < MSG_THROTTLE_SEC:
        return True
    data[key] = now
    _throttle_save(data)
    return False

# 세션별 HITL 상태 (thread_id → session info) — morning/afternoon 충돌 방지
_hitl_sessions: dict[str, dict] = {}
# 수량 입력 대기 중인 채팅 (chat_id → thread_id)
_pending_input: dict[int, str] = {}


# ── Telegram Bot 커맨드 ─────────────────────────────────────

async def cmd_kis_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/kis_mode — KIS 모의/실거래 토글"""
    _runtime["kis_simulated"] = not _runtime["kis_simulated"]
    mode = "모의투자" if _runtime["kis_simulated"] else "실거래"
    await update.message.reply_text(
        f"🔄 KIS 모드 변경: **{mode}** ({'시뮬레이션' if _runtime['kis_simulated'] else '⚠️ 실제 주문'})",
        parse_mode="Markdown"
    )
    log.info("KIS 모드 변경: simulated=%s", _runtime["kis_simulated"])

async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/run — 매매 파이프라인 즉시 강제 실행"""
    await update.message.reply_text("🚀 매매 파이프라인 수동 실행을 바로 시작합니다!")
    from autostock.scheduler.jobs import run_daily_pipeline
    if _bot_loop:
        _bot_loop.create_task(run_daily_pipeline())

async def cmd_set_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/set_review N — AI 검토 횟수 변경 (3~10)"""
    if not context.args:
        await update.message.reply_text("사용법: /set_review 3")
        return
    try:
        count = int(context.args[0])
        count = max(3, min(10, count))
        _runtime["review_count"] = count
        await update.message.reply_text(f"🔧 AI 검토 횟수 변경: **{count}**회", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("숫자를 입력해주세요.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status — 현재 설정 상태 조회"""
    mode = "모의투자" if _runtime["kis_simulated"] else "⚠️ 실거래"
    msg = (
        f"⚙️ *현재 설정*\n"
        f"• KIS 모드: {mode}\n"
        f"• AI 검토 횟수: {_runtime['review_count']}회\n"
        f"• HITL 타임아웃: {config.HITL_TIMEOUT_MINUTES}분\n"
        f"• 스케줄: {config.PIPELINE_SCHEDULES_STR}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/restart — 서버 프로세스 재시작 (PM2 관리 필요)"""
    await update.message.reply_text(
        "🔄 서버 재시작을 시작합니다. 포트 반납 후 PM2가 재시작합니다.\n"
        "10~15초 후 `/status`로 확인해 주세요."
    )
    log.info("Telegram 명령어에 의한 서버 재시작 — 3초 후 종료")
    # 응답 전송 완료 대기 후 종료 (즉시 종료 시 포트 미반납 문제 방지)
    await asyncio.sleep(3)
    import os, signal
    os.kill(os.getpid(), signal.SIGTERM)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help — 도움말 및 명령어 안내"""
    msg = (
        "🤖 *SKY-AutoStock 명령어 안내*\n\n"
        "• `/run`: 매매 파이프라인 (스크리닝~분석~HITL) 즉시 실행\n"
        "• `/test_hitl`: HITL 수량 입력 흐름 샘플 테스트\n"
        "• `/status`: 현재 설정 및 서버 상태 확인\n"
        "• `/restart`: 서버 프로세스 강제 재시작 (상태 초기화)\n"
        "• `/kis_mode`: 모의투자 / 실거래 모드 토글\n"
        "• `/set_review N`: AI 분석 검토 횟수 설정 (3~10)\n"
        "• `/help`: 이 도움말 메시지 표시\n\n"
        "💡 *Tip*: 만약 명령어를 보냈는데 응답이 없다면, 서버 프로세스(PM2) 확인이 필요할 수 있습니다."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_test_hitl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/test_hitl — HITL 수량 입력 흐름 샘플 테스트 (가상 종목 3개)"""
    if not update.message:
        return
    import uuid
    thread_id = f"test-{uuid.uuid4().hex[:8]}"

    sample_decisions = [
        {
            "ticker": "005930", "action": "BUY",
            "price_reference": 73400.0, "stop_loss_price": 70000.0,
            "confidence": 8.5, "ai_qty": 7,
            "final_reason": "반도체 업황 회복 기대감 + 외국인 순매수 지속.",
        },
        {
            "ticker": "035720", "action": "BUY",
            "price_reference": 54200.0, "stop_loss_price": 51500.0,
            "confidence": 7.2, "ai_qty": 4,
            "final_reason": "카카오뱅크 자회사 성장 + 광고 매출 회복세.",
        },
        {
            "ticker": "000660", "action": "HOLD",
            "price_reference": 185000.0, "stop_loss_price": 176000.0,
            "confidence": 6.1, "ai_qty": 0,
            "final_reason": "단기 고점 부근 — 추가 상승 여력 제한적.",
        },
    ]
    ticker_map = {"005930": "삼성전자", "035720": "카카오", "000660": "SK하이닉스"}

    # hitl_state에 더미 이벤트 등록 (아무도 await하지 않아도 resolve() 호출은 안전하게 동작)
    hitl_state.register(thread_id)

    await update.message.reply_text(f"[TEST] thread_id={thread_id} 샘플 HITL 시작")
    await _send_hitl_message_async(sample_decisions, thread_id, ticker_map, market_condition="NORMAL")


# ── 텍스트 메시지 핸들러 (종목별 수량 입력) ────────────────────

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text = update.message.text.strip()

    thread_id = _pending_input.get(chat_id)
    if thread_id is None:
        return

    session = _hitl_sessions.get(thread_id)
    if not session or not session.get("pending_ticker"):
        _pending_input.pop(chat_id, None)
        return

    ticker = session["pending_ticker"]
    name = next((n for t, n in session["buy_tickers"] if t == ticker), ticker)

    try:
        qty = int(text)
        if qty < 0:
            await update.message.reply_text("⚠️ 0 이상의 숫자를 입력해주세요.")
            return
    except ValueError:
        await update.message.reply_text("⚠️ 숫자만 입력해주세요.")
        return

    session["approved_qty"][ticker] = qty
    session["pending_ticker"] = None
    _pending_input.pop(chat_id, None)

    if qty == 0:
        await update.message.reply_text(
            f"✅ <b>{name} ({ticker})</b> — 제외(0주)로 설정되었습니다.", parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            f"✅ <b>{name} ({ticker})</b> — <b>{qty}주</b>로 설정되었습니다.", parse_mode="HTML"
        )

    await _send_qty_status(context, chat_id, thread_id, session)


async def _send_qty_status(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    thread_id: str,
    session: dict,
) -> None:
    """수량 설정 현황 메시지 + 다음 버튼 전송."""
    has_any_manual = bool(session["approved_qty"])
    lines = ["📋 <b>수량 설정 현황</b>"]
    for t, n in session["buy_tickers"]:
        if t in session["approved_qty"]:
            q = session["approved_qty"][t]
            mark = "✅" if q > 0 else "🚫"
            lines.append(f"  {mark} {n} ({t}): {q}주")
        else:
            ai_q = session["ai_qty"].get(t, 0)
            if has_any_manual:
                lines.append(f"  ❌ {n} ({t}): 미선택 → 취소 (AI: {ai_q}주)")
            else:
                lines.append(f"  ⬜ {n} ({t}): 미선택 (AI: {ai_q}주)")

    unset = [(t, n) for t, n in session["buy_tickers"] if t not in session["approved_qty"]]

    keyboard = []
    if unset:
        if has_any_manual:
            lines.append("\n⚠️ 미선택 종목은 <b>취소</b>됩니다. 추가 입력하거나 최종 승인하세요.")
            approve_label = "✅ 최종 승인 (미선택 종목 취소)"
        else:
            lines.append("\n종목 버튼을 눌러 수량을 입력하거나, 바로 승인하면 AI 수량으로 전체 매수합니다.")
            approve_label = "✅ 최종 승인 (AI 수량 전체 적용)"
        for t, n in unset:
            ai_q = session["ai_qty"].get(t, 0)
            keyboard.append([InlineKeyboardButton(
                f"✏️ {n} ({t}) — AI:{ai_q}주",
                callback_data=f"qty:{thread_id}:{t}"
            )])
        keyboard.append([InlineKeyboardButton(approve_label, callback_data=f"final_approve:{thread_id}")])
    else:
        lines.append("\n✅ 모든 종목의 수량이 설정되었습니다. 최종 승인을 눌러주세요.")
        keyboard.append([InlineKeyboardButton(
            "✅ 최종 승인",
            callback_data=f"final_approve:{thread_id}"
        )])

    keyboard.append([InlineKeyboardButton("❌ 거절", callback_data=f"reject:{thread_id}")])

    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── 버튼 클릭(Callback) 핸들러 ────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    # callback_data 형식: "action:thread_id" 또는 "qty:thread_id:ticker"
    parts = data.split(":", 2)
    action = parts[0]

    if action == "approve" and len(parts) == 2:
        thread_id = parts[1]
        # AI 권장 수량 그대로 사용 (approved_qty={} → executor가 ai_qty 사용)
        hitl_state.resolve(thread_id, "approved", {})
        await query.edit_message_text(text=f"{query.message.text}\n\n[처리완료] ✅ 승인됨 (AI 수량)")
        _hitl_sessions.pop(thread_id, None)

    elif action == "reject" and len(parts) == 2:
        thread_id = parts[1]
        hitl_state.resolve(thread_id, "rejected", {})
        await query.edit_message_text(text=f"{query.message.text}\n\n[처리완료] ❌ 거절됨")
        _hitl_sessions.pop(thread_id, None)

    elif action == "qty" and len(parts) == 3:
        thread_id = parts[1]
        ticker = parts[2]
        session = _hitl_sessions.get(thread_id)
        if not session:
            await query.answer("세션이 만료되었습니다.", show_alert=True)
            return

        session["pending_ticker"] = ticker
        chat_id = query.message.chat_id
        _pending_input[chat_id] = thread_id

        name = next((n for t, n in session["buy_tickers"] if t == ticker), ticker)
        ai_qty = session["ai_qty"].get(ticker, 0)
        current = session["approved_qty"].get(ticker)
        hint = f" (현재: {current}주)" if current is not None else f" (AI 권장: {ai_qty}주)"

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✏️ <b>{name} ({ticker})</b>의 수량을 입력해주세요.{hint}\n(0 입력 시 해당 종목 제외)",
            parse_mode="HTML",
        )

    elif action == "final_approve" and len(parts) == 2:
        thread_id = parts[1]
        session = _hitl_sessions.get(thread_id)
        if not session:
            await query.answer("세션이 만료되었습니다.", show_alert=True)
            return

        manually_set = session["approved_qty"]
        if not manually_set:
            # 아무 종목도 선택 안 했으면 AI 수량 전체 적용
            approved_qty = {t: q for t, q in session["ai_qty"].items() if q > 0}
            note = "✅ 최종 승인됨 (AI 수량 전체 적용)"
        else:
            # 수동 입력한 종목만 매수, 나머지 취소
            approved_qty = {t: q for t, q in manually_set.items() if q > 0}
            note = "✅ 최종 승인됨 (선택 종목만 매수, 미선택 취소)"

        hitl_state.resolve(thread_id, "approved", approved_qty)
        await query.edit_message_text(text=f"{query.message.text}\n\n[처리완료] {note}")
        _hitl_sessions.pop(thread_id, None)


# ── 외부 API (FastAPI/Scheduler에서 호출) ──────────────────────

async def _send_message_async(text: str):
    if not _app or not config.TELEGRAM_CHAT_ID:
        return
    try:
        await _app.bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=text, parse_mode="HTML")
    except Exception as e:
        log.error("Telegram 전송 에러: %s", e)

def schedule_message(msg: str, throttle_key: str | None = None) -> None:
    """텔레그램 메시지 발송.

    throttle_key 지정 시 MSG_THROTTLE_SEC(4h) 이내 동일 키 중복 발송을 차단.
    파일 기반 쓰로틀이라 서버 재시작 후에도 유지됨.
    """
    if throttle_key and _throttle_check(throttle_key):
        log.debug("메시지 쓰로틀 스킵 (%s)", throttle_key)
        return

    if _bot_loop and _bot_loop.is_running():
        asyncio.run_coroutine_threadsafe(_send_message_async(msg), _bot_loop)
    else:
        log.warning("Telegram loop not running: %s", msg)


async def _send_hitl_message_async(
    final_decisions: list[dict],
    thread_id: str,
    ticker_map: dict,
    market_condition: str = "UNKNOWN",
) -> None:
    if not _app or not config.TELEGRAM_CHAT_ID:
        return

    buy_decisions = [d for d in final_decisions if d["action"] == "BUY"]

    # 세션 초기화 (thread_id 단위이므로 오전/오후 충돌 없음)
    _hitl_sessions[thread_id] = {
        "chat_id": int(config.TELEGRAM_CHAT_ID),
        "buy_tickers": [(d["ticker"], ticker_map.get(d["ticker"], d["ticker"])) for d in buy_decisions],
        "ai_qty": {d["ticker"]: d.get("ai_qty", 0) for d in buy_decisions},
        "approved_qty": {},
        "pending_ticker": None,
    }

    emoji_map = {"DANGER": "🔥", "CAUTION": "⚠️", "NORMAL": "🟢", "BEAR": "🐻"}
    emoji = emoji_map.get(market_condition, "💬")

    msg = f"{emoji} <b>오늘의 거시 시황</b>: <code>{market_condition}</code>\n"
    if market_condition == "DANGER":
        msg += "👉 <i>역발상 매매 찬스 구간입니다! 분석 결과를 확인해주세요.</i>\n\n"
    elif market_condition == "BEAR":
        msg += "👉 <i>⚠️ 시장 약세 국면입니다. 아래 종목은 약세장에서도 급부각된 강세 종목입니다.</i>\n\n"
    else:
        msg += "👉 <i>가이드 시황은 아니지만, AI가 심층 발굴한 결과를 공유합니다.</i>\n\n"

    for d in final_decisions:
        act_emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(d["action"], "⚪")
        name = ticker_map.get(d["ticker"], d["ticker"])
        price = d["price_reference"]
        sl = d["stop_loss_price"]
        conf = d["confidence"]
        qty = d.get("ai_qty", 0)
        reason = d["final_reason"]

        bull_score = d.get("bull_score")
        bear_score = d.get("bear_score")
        score_str = (
            f" | 🐂{bull_score:.1f} 🐻{bear_score:.1f}"
            if bull_score is not None and bear_score is not None
            else ""
        )
        msg += f"{act_emoji} <b>{name} ({d['ticker']}) - {d['action']}</b>\n"
        msg += f"▪ 기준가: {price:,.0f} | 손절: {sl:,.0f} | 신뢰도: {conf}{score_str}\n"
        if d["action"] == "BUY":
            msg += f"▪ AI권장수량: <b>{qty}주</b>\n"
        msg += f"💬 이유: {reason}\n\n"

    if buy_decisions:
        msg += (
            "🛠 <b>수동 수량 설정</b>\n"
            "• 종목 버튼을 눌러 수량 입력 → <b>입력한 종목만 매수</b>, 나머지 자동 <b>취소</b>\n"
            "• 아무 버튼도 누르지 않고 승인 → <b>AI 수량 전체 적용</b>"
        )
        keyboard = []
        for d in buy_decisions:
            t = d["ticker"]
            n = ticker_map.get(t, t)
            ai_q = d.get("ai_qty", 0)
            keyboard.append([InlineKeyboardButton(
                f"✏️ {n} ({t}) — AI:{ai_q}주",
                callback_data=f"qty:{thread_id}:{t}",
            )])
        keyboard.append([InlineKeyboardButton("✅ AI 수량으로 전체 승인", callback_data=f"approve:{thread_id}")])
        keyboard.append([InlineKeyboardButton("❌ 거절", callback_data=f"reject:{thread_id}")])
    else:
        keyboard = [
            [InlineKeyboardButton("✅ 승인", callback_data=f"approve:{thread_id}")],
            [InlineKeyboardButton("❌ 거절", callback_data=f"reject:{thread_id}")],
        ]

    try:
        await _app.bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        log.error("Telegram HITL 전송 에러: %s", e)


def schedule_hitl_message(
    final_decisions: list[dict],
    thread_id: str,
    ticker_map: dict,
    market_condition: str = "UNKNOWN",
) -> None:
    if _bot_loop and _bot_loop.is_running():
        asyncio.run_coroutine_threadsafe(
            _send_hitl_message_async(final_decisions, thread_id, ticker_map, market_condition),
            _bot_loop,
        )


async def _send_result_async(exec_results: list[dict], hitl_result: str) -> None:
    if not _app or not config.TELEGRAM_CHAT_ID:
        return

    if hitl_result == "rejected":
        await _app.bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text="❌ 매매가 취소(거절)되었습니다.")
        return

    msg = "<b>[매매 주문 실행 결과]</b>\n\n"
    for r in exec_results:
        act = r["action"]
        emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(act, "⚪")
        if r["qty"] > 0:
            order_result = r.get("order_result") or {}
            filled = order_result.get("filled", False) if isinstance(order_result, dict) else False
            fill_price = r.get("fill_price", 0)
            status = "✅체결" if filled else "❌미체결"
            msg += f"{emoji} {act} {r['ticker']} | {r['qty']}주 | {status}\n"
            if fill_price:
                msg += f"└ 체결가: {fill_price:,.0f}원\n"

    if not exec_results or all(r["qty"] == 0 for r in exec_results):
        msg += "통과된 매매가 없거나 주문 수량이 0입니다."

    try:
        await _app.bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=msg, parse_mode="HTML")
    except Exception as e:
        log.error("Telegram 결과 전송 에러: %s", e)


def schedule_result_notification(exec_results: list[dict], hitl_result: str) -> None:
    if _bot_loop and _bot_loop.is_running():
        asyncio.run_coroutine_threadsafe(_send_result_async(exec_results, hitl_result), _bot_loop)


# ── 라이프사이클 및 이벤트 루프 ──────────────────────────────

def _kill_previous_session() -> None:
    """이전 getUpdates long-poll 강제 종료."""
    import httpx as _httpx
    try:
        _httpx.get(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": -1, "timeout": 0, "limit": 1},
            timeout=10,
        )
        log.info("Telegram 이전 polling 세션 종료 완료")
    except Exception as e:
        log.warning("Telegram 세션 초기화 실패 (무시): %s", e)


def _build_app() -> "Application":
    import telegram as _tg
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("kis_mode", cmd_kis_mode))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("set_review", cmd_set_review))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("test_hitl", cmd_test_hitl))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    async def _on_error(update, context):
        log.error("Telegram 에러: %s", context.error)

    app.add_error_handler(_on_error)
    return app


def _start_loop():
    global _bot_loop, _app

    log.info("Telegram Bot 스레드 초기화 중...")
    if not config.TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN이 설정되지 않아 텔레그램 기능을 비활성화합니다.")
        return

    _kill_previous_session()
    time.sleep(2)

    new_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(new_loop)
    _bot_loop = new_loop

    _app = _build_app()

    log.info("Telegram Bot 시작 (Polling)")
    _app.run_polling(
        close_loop=True,
        drop_pending_updates=True,
        timeout=10,
    )
    log.info("Telegram polling 종료")


def start_bot_background():
    t = threading.Thread(target=_start_loop, daemon=True, name="TelegramBotThread")
    t.start()
    return t
