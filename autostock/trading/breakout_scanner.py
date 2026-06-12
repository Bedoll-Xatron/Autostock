"""실시간 급등주 스캐너 — 30초마다 돌파매매 후보를 감시."""
import asyncio
from datetime import datetime, date

import pytz

from autostock import config
from autostock.db import supabase as db
from autostock.market.kr_holidays import is_market_hours
from autostock.trading.kis_client import get_quote_detail, get_available_cash, get_volume_rank, place_order
from autostock.trading.trailing_stop import TrailingPosition, add_positions, _fixed_stop, liberate_capital
from autostock.trading.executor import calc_order_qty, execute_decisions
from autostock.hitl import hitl_state, telegram_bot as bot_ui
from autostock.logger import get_logger

log = get_logger(__name__)

KST = pytz.timezone("Asia/Seoul")
POLL_INTERVAL = 30  # 초

# 오늘 이미 분석한 종목 (과도한 중복 분석 방지)
_scanned_today: set[str] = set()
_last_scanned_date: str = ""


def _check_and_reset_cache():
    global _scanned_today, _last_scanned_date
    today_str = datetime.now(KST).strftime('%Y-%m-%d')
    if _last_scanned_date != today_str:
        _scanned_today.clear()
        _last_scanned_date = today_str


async def run_fast_track_pipeline(ticker: str, name: str, current_price: float):
    """급등 종목 발견 시 Fast Track AI 분석 및 조건 충족 시 30% 비중 시장가 매수."""
    from autostock.research.fast_track_graph import build_fast_track_graph
    
    log.info("🚀 [Fast Track] %s(%s) 급등 감지 (현재가 %s) — AI 분석 시작", name, ticker, current_price)
    bot_ui.schedule_message(f"🚀 <b>급등 감지</b>: {name}({ticker})\n현재가: {current_price:,.0f} (+5% 돌파)\n🧠 AI 초고속 분석을 시작합니다...")

    graph, checkpointer = build_fast_track_graph()
    
    market_data = db.fetch_latest_market_data() or {}
    watchlist = [{"ticker": ticker, "name": name, "price_reference": current_price}]
    
    thread_id = f"fast_track_{ticker}_{datetime.now(KST).strftime('%H%M%S')}"
    config = {"configurable": {"thread_id": thread_id}}
    
    state = {
        "market_data": market_data,
        "watchlist": watchlist,
        "selected_tickers": [ticker],
        "final_decisions": [],
    }
    
    # 1. 그래프 실행
    try:
        final_state = await graph.ainvoke(state, config)
    except Exception as e:
        log.error("Fast Track 그래프 실행 실패: %s", e)
        return

    decisions = final_state.get("final_decisions", [])
    if not decisions:
        log.warning("Fast Track: AI 결정을 내리지 못함.")
        return

    d = decisions[0]
    
    # 2. 결정 확인 및 자동 매수 실행
    if d.action == "BUY" and d.confidence >= 8.0:
        log.info("✅ Fast Track AI 승인: %s BUY (신뢰도 %.1f)", ticker, d.confidence)
        
        # 가용 예수금의 30%
        cash = await asyncio.to_thread(get_available_cash)
        if cash is None:
            log.warning("[%s] 잔고 조회 실패 (API 오류) — 매수 스킵", ticker)
            return
        target_amount = cash * 0.3
        qty = int(target_amount // current_price)

        if qty <= 0:
            log.warning("[%s] 가용 잔고 부족 (%.0f원) — 자본 확보 손절 시작", ticker, cash)
            await liberate_capital(target_ratio=0.30)
            bot_ui.schedule_message(
                f"❌ <b>매수 보류</b>: {name}({ticker})\n"
                f"잔고 부족 (현재 {cash:,.0f}원) — 자본 확보 손절 진행 중\n"
                f"매도 완료 후 다음 기회에 매수"
            )
            return

        try:
            # 시장가 매수
            place_order(ticker, "BUY", qty, 0)
            
            # 매수 성공 시 Trailing Stop 감시 큐에 추가 및 DB 저장
            pos = TrailingPosition(
                ticker=ticker,
                name=name,
                qty=qty,
                avg_price=current_price,  # 시장가 체결이라 정확하지 않을 수 있으나 현재가로 임시 기록
                entry_price=current_price,
                stop_price=_fixed_stop(current_price),
                peak_price=current_price,
                phase='stop',
            )
            add_positions([pos])
            db.save_held_position({
                "ticker": pos.ticker,
                "name": pos.name,
                "qty": pos.qty,
                "avg_price": pos.avg_price,
                "entry_price": pos.entry_price,
                "stop_price": pos.stop_price,
                "peak_price": pos.peak_price,
                "phase": pos.phase,
                "entry_date": pos.entry_date,
            })
            
            # 성공 메시지
            msg = (
                f"✅ <b>[Fast Track 자동 매수 완료]</b>\n"
                f"🟢 <b>{name} ({ticker})</b>\n"
                f"▪ AI 신뢰도: {d.confidence}점\n"
                f"▪ 이유: {d.final_reason}\n"
                f"▪ 매수 수량: {qty}주 (예수금의 30% 배정)\n"
                f"▪ 감시가: {current_price:,.0f}원 (자동 트레일링 스탑 적용됨)"
            )
            bot_ui.schedule_message(msg)
            
        except Exception as e:
            log.error("Fast Track 자동매수 주문 실패: %s", e)
            bot_ui.schedule_message(f"❌ <b>주문 실패</b>: {name}({ticker})\n이유: {e}")
    else:
        # BUY가 아니거나 신뢰도가 부족한 경우 관망
        log.info("❌ Fast Track AI 거절: %s %s (신뢰도 %.1f)", ticker, d.action, d.confidence)
        bot_ui.schedule_message(
            f"👀 <b>AI 관망(HOLD) 결정</b>: {name}({ticker})\n"
            f"▪ 신뢰도: {d.confidence}점\n"
            f"▪ 이유: {d.final_reason}"
        )


async def watch_breakouts() -> None:
    """
    백그라운드에서 실시간 거래량 상위 종목들을 30초마다 감시.
    시가 대비 +5% 이상 상승 시 Fast Track 파이프라인 트리거.
    """
    log.info("급등주 돌파매매(Fast Track) 스캐너 시작")
    
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        
        if not is_market_hours():
            continue
            
        _check_and_reset_cache()
        
        try:
            ranking_list = get_volume_rank(limit=30)
            if not ranking_list:
                continue
                
            for w in ranking_list:
                ticker = w["ticker"]
                name = w["name"]
                
                # 오늘 이미 분석했다면 스킵
                if ticker in _scanned_today:
                    continue
                    
                quote = get_quote_detail(ticker)
                price = quote.get("price", 0)
                open_price = quote.get("open", 0)
                
                if open_price > 0 and price >= open_price * 1.05:
                    _scanned_today.add(ticker)
                    # 별도의 태스크로 실행하여 스캐너 루프 블로킹 방지
                    asyncio.create_task(run_fast_track_pipeline(ticker, name, price))
                    
        except Exception as e:
            log.error("Breakout 스캐너 루프 에러: %s", e)
