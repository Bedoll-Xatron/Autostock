"""매매 주문 실행 — 수량 계산 및 KIS 주문."""
import asyncio
import os

from autostock import config
from autostock.models import FinalDecision
from autostock.trading.kis_client import get_available_cash, get_holding_qty, get_current_price, market_sell
from autostock.trading.limit_order import limit_buy_with_timeout, limit_buy_with_pullback
from autostock.trading.entry_gates import check_entry_gates
from autostock.trading.risk import compute_stop_pct
from autostock.db import supabase as db
from autostock.hitl import telegram_bot as bot_ui
from autostock.logger import get_logger

log = get_logger(__name__)

GAP_UP_BLOCK_PCT = 3.0  # 전일 대비 갭업 3% 이상이면 진입 보류

# 실거래(KIS_SIMULATED_MODE=false) 전환 시 자동 강화 (09_점수임계값_검토_가이드 §6.2)
SINGLE_POSITION_CAP_PCT = 0.10 if not config.KIS_SIMULATED_MODE else 0.15
SECTOR_LIMIT            = 2
DAILY_NEW_ENTRY_LIMIT   = 2    if not config.KIS_SIMULATED_MODE else 3
TOTAL_HOLDING_LIMIT     = 5    if not config.KIS_SIMULATED_MODE else 7


def _check_position_limits(
    ticker: str,
    sector: str,
    sector_count: dict[str, int],
    new_count: int,
    total_holding: int,
) -> str | None:
    """섹터/일일/총 보유 한도 검사. 위반 시 blocked_reason 문자열 반환, 통과 시 None."""
    if sector_count.get(sector, 0) >= SECTOR_LIMIT:
        log.info("[%s] 섹터 %s 한도(%d) 초과 — 보류", ticker, sector, SECTOR_LIMIT)
        return "SECTOR_LIMIT"
    if new_count >= DAILY_NEW_ENTRY_LIMIT:
        log.info("[%s] 일일 신규 진입 한도(%d) 초과 — 보류", ticker, DAILY_NEW_ENTRY_LIMIT)
        return "DAILY_LIMIT"
    if total_holding + new_count >= TOTAL_HOLDING_LIMIT:
        log.info("[%s] 총 보유 한도(%d) 초과 — 보류", ticker, TOTAL_HOLDING_LIMIT)
        return "HOLDING_LIMIT"
    return None


def calc_order_qty(
    decision: FinalDecision,
    available_cash: float,
    position_scale: float = 1.0,
) -> int:
    """
    변동성(ATR) 단독 포지션 사이징 — LLM confidence는 사이징에 미반영.

    매수 여부는 supervisor의 buy_threshold가 게이트하고, 크기는 오직 리스크로 결정:
        risk_amount   = available_cash * R_RATIO(0.5%) * position_scale
        stop_loss_pct = ATR 기반(compute_stop_pct, 종목별 2.5~6%)
        position_size = risk_amount / stop_loss_pct   (단일 포지션 캡 적용)
    """
    if decision.action != "BUY":
        return 0

    entry = decision.price_reference or 0.0
    if entry <= 0 or available_cash <= 0:
        return 0

    # ATR 기반 손절폭 (트레일링 초기 손절과 동일 기준으로 일관성 확보)
    stop_loss_pct = compute_stop_pct(decision.ticker) / 100.0
    if stop_loss_pct <= 0:
        stop_loss_pct = config.STOP_LOSS_PCT

    # 고정 리스크(0.5R). 검증되지 않은 LLM 신뢰도로 베팅 크기를 키우지 않는다.
    risk_amount = available_cash * config.R_RATIO * position_scale
    position_size = risk_amount / stop_loss_pct
    position_size = min(position_size, available_cash * SINGLE_POSITION_CAP_PCT)

    qty = int(position_size / entry)
    log.info(
        "calc_order_qty: %s ATR손절=%.1f%% scale=%.1f size=%.0f qty=%d (LLM신뢰도 미반영)",
        decision.ticker, stop_loss_pct * 100, position_scale, position_size, qty,
    )
    return max(0, qty)


async def execute_decisions(
    decisions: list[FinalDecision],
    hitl_result: str,
    approved_qty: dict[str, int],
    state: dict | None = None,
    position_scale: float = 1.0,
) -> list[dict]:
    """
    승인된 경우 매매 주문 실행. 결과를 Supabase에 저장.

    Args:
        decisions: 최종 결정 리스트.
        hitl_result: "approved" 또는 "rejected".
        approved_qty: Telegram에서 입력한 수량 (없으면 AI 계산).
        state: LangGraph TradingState (리포트 데이터 추출용, 선택적).

    Returns:
        실행 결과 리스트.
    """
    if os.getenv("TRADING_PAUSED", "false").lower() == "true":
        log.warning("TRADING_PAUSED=true — 모든 매매 정지")
        bot_ui.schedule_message("🚫 *TRADING_PAUSED=true* — 오늘 매매 전체 정지됨")
        return []

    results = []
    available_cash = (get_available_cash() or 0.0) if hitl_result == "approved" else 0.0
    watchlist = db.fetch_watchlist()
    w_map = {w["ticker"]: w for w in watchlist}

    # state에서 리포트 추출 (없으면 빈 dict)
    tech_reports = (state or {}).get("technical_reports", {})
    fund_reports = (state or {}).get("fundamental_reports", {})
    sent_reports = (state or {}).get("sentiment_reports", {})
    bull_reports = (state or {}).get("bull_reports", {})
    bear_reports = (state or {}).get("bear_reports", {})

    # ── 섹터/보유 한도 상태 초기화 (W5) ──────────────────
    existing_holdings = db.fetch_held_positions() if hitl_result == "approved" else []
    _sector_count: dict[str, int] = {}
    for _h in existing_holdings:
        _sec = w_map.get(_h["ticker"], {}).get("sector", "Unknown")
        _sector_count[_sec] = _sector_count.get(_sec, 0) + 1
    _total_holding = len(existing_holdings)
    # 오전/오후 파이프라인 간 일일 한도 연속성 — 오늘 이미 진입한 건수로 초기화
    _new_count = db.fetch_today_buy_count() if hitl_result == "approved" else 0
    if _new_count > 0:
        log.info("일일 신규 진입 누적: 오늘 이미 %d건 체결 — 잔여 한도 %d",
                 _new_count, max(0, DAILY_NEW_ENTRY_LIMIT - _new_count))

    # ── Pass 1: 게이트·수량 계획 (주문 실행 없음, 순차) ─────────
    # 매수 주문은 눌림 대기로 종목당 최대 30분 블로킹될 수 있어, 계획을 먼저
    # 세운 뒤 Pass 2에서 병렬 실행한다. (순차 실행 시 첫 종목 이후가 09:45 창을
    # 놓치는 기아 버그 방지)
    plans: list[dict] = []
    for d in decisions:
        qty = 0
        blocked_reason = None
        gap_pct = 0.0
        open_price = 0.0

        w_info = w_map.get(d.ticker, {})

        if hitl_result == "approved" and d.action in ("BUY", "SELL"):
            if d.action == "BUY":
                # 갭업 진입 차단 (GAP_UP_BLOCK_ENABLED=false 로 비활성화 가능)
                if os.getenv("GAP_UP_BLOCK_ENABLED", "true").lower() != "false":
                    prev_close = w_info.get("prev_close") or 0
                    try:
                        open_price = get_current_price(d.ticker)
                    except Exception as e:
                        log.warning("[%s] 현재가 조회 실패 — 갭 검증 우회: %s", d.ticker, e)
                    if prev_close > 0 and open_price > 0:
                        gap_pct = (open_price / prev_close - 1) * 100
                        if gap_pct >= GAP_UP_BLOCK_PCT:
                            blocked_reason = "GAP_UP"
                            log.info(
                                "[%s] 갭업 차단: 전일 %.0f → 현재 %.0f (%.1f%%)",
                                d.ticker, prev_close, open_price, gap_pct,
                            )
                            bot_ui.schedule_message(
                                f"⏸ *{w_info.get('name', d.ticker)}* 갭업 진입 보류\n"
                                f"전일 {prev_close:,.0f} → 시초 {open_price:,.0f} ({gap_pct:+.1f}%)"
                            )

                # ── 체결 직전 안전 게이트 (W4) ──────────────────
                if blocked_reason is None:
                    change_pct_val = w_info.get("change_pct") or 0.0
                    cur_price_val = open_price if open_price > 0 else (d.price_reference or 0.0)
                    gate_block = check_entry_gates(d.ticker, change_pct_val, cur_price_val)
                    if gate_block:
                        blocked_reason = gate_block
                        bot_ui.schedule_message(
                            f"⛔ <b>{w_info.get('name', d.ticker)}</b> 체결 직전 게이트 차단\n"
                            f"사유: {gate_block}"
                        )

                # ── 섹터 + 보유 한도 검증 (W5) ──────────────────
                if blocked_reason is None:
                    _entry_sec = w_info.get("sector", "Unknown")
                    blocked_reason = _check_position_limits(
                        d.ticker, _entry_sec, _sector_count, _new_count, _total_holding
                    )

                if blocked_reason is None:
                    if approved_qty:
                        # 딕셔너리에 있는 종목만 매수, 없으면 취소(0)
                        qty = approved_qty.get(d.ticker, 0)
                    else:
                        # 빈 dict = 구버전 일괄승인 버튼 → AI 수량 계산
                        qty = calc_order_qty(d, available_cash, position_scale)
            elif d.action == "SELL":
                qty = get_holding_qty(d.ticker)

        # 매수 시도 종목은 후속 결정의 한도 게이트를 위해 계획 시점에 카운트
        if d.action == "BUY" and qty > 0:
            _sec = w_info.get("sector", "Unknown")
            _sector_count[_sec] = _sector_count.get(_sec, 0) + 1
            _new_count += 1

        plans.append({
            "d": d,
            "w_info": w_info,
            "qty": qty,
            "fill_price": 0,
            "order_result": None,
            "blocked_reason": blocked_reason,
            "gap_pct": gap_pct,
            "open_price": open_price,
        })

    # ── Pass 2: 매수 주문 병렬 실행 (눌림 대기 기아 방지) ────────
    entry_mode = os.getenv("ENTRY_MODE", "pullback")
    buy_fn = limit_buy_with_pullback if entry_mode == "pullback" else limit_buy_with_timeout
    buy_plans = [p for p in plans if p["d"].action == "BUY" and p["qty"] > 0]
    if buy_plans:
        log.info("매수 주문 %d종목 병렬 실행 (mode=%s)", len(buy_plans), entry_mode)
        buy_results = await asyncio.gather(
            *(buy_fn(p["d"].ticker, p["qty"], p["d"].price_reference) for p in buy_plans),
            return_exceptions=True,
        )
        for p, r in zip(buy_plans, buy_results):
            if isinstance(r, Exception):
                log.error("[%s] 매수 주문 예외: %s", p["d"].ticker, r)
                p["order_result"] = {"filled": False, "error": str(r)}
                p["qty"] = 0
            else:
                filled_qty, fill_price = r
                # 실제 체결 수량으로 반영 (부분 체결 잔량 미추적 방지)
                p["qty"] = filled_qty
                p["order_result"] = {"filled": filled_qty > 0}
                if filled_qty > 0:
                    p["fill_price"] = fill_price

    # ── Pass 2b: 매도 주문 (시장가, 즉시) ───────────────────────
    for p in plans:
        if p["d"].action == "SELL" and p["qty"] > 0:
            try:
                market_sell(p["d"].ticker, p["qty"])
                p["order_result"] = {"filled": True}
            except Exception as e:
                log.error("[%s] SELL 실패: %s", p["d"].ticker, e)
                p["order_result"] = {"filled": False, "error": str(e)}
                p["qty"] = 0

    # ── Pass 3: Supabase 저장 + 결과 집계 ───────────────────────
    for p in plans:
        d = p["d"]
        w_info = p["w_info"]
        qty = p["qty"]
        fill_price = p["fill_price"]
        order_result = p["order_result"]
        blocked_reason = p["blocked_reason"]
        gap_pct = p["gap_pct"]
        open_price = p["open_price"]

        tech  = _model_to_dict(tech_reports.get(d.ticker))
        fund  = _model_to_dict(fund_reports.get(d.ticker))
        sent  = _model_to_dict(sent_reports.get(d.ticker))
        bull  = _model_to_dict(bull_reports.get(d.ticker))
        bear  = _model_to_dict(bear_reports.get(d.ticker))

        db.save_decision({
            # 기본 결정
            "ticker":         d.ticker,
            "name":           w_info.get("name", "Unknown"),
            "sector":         w_info.get("sector", "Unknown"),
            "action":         d.action,
            "price_reference": d.price_reference,
            "stop_loss_price": d.stop_loss_price,
            "confidence":     d.confidence,
            "order_qty":      qty,
            "hitl_result":    hitl_result,
            "final_reason":   d.final_reason,
            # 디베이트 요약
            "bull_summary":   d.bull_summary,
            "bear_summary":   d.bear_summary,
            # Bull / Bear 점수
            "bull_score":     bull.get("bull_score"),
            "bear_score":     bear.get("bear_score"),
            # 기술적 분석
            "rsi":            tech.get("rsi"),
            "trend":          tech.get("trend"),
            "macd_signal":    tech.get("macd"),
            # 기본적 분석
            "per":            fund.get("per"),
            "pbr":            fund.get("pbr"),
            "roe":            fund.get("roe"),
            "valuation":      fund.get("valuation"),
            # 감성 분석
            "news_score":     sent.get("news_score"),
            "foreign_net":    sent.get("foreign_net"),
            "inst_net":       sent.get("inst_net"),
            # MarketFlow 시그널
            "signal_score":   w_info.get("signal_score"),
            "strategies":     w_info.get("strategies"),
            "strategy_count": w_info.get("strategy_count"),
            "signal_strength": w_info.get("signal_strength"),
            "theme":          w_info.get("theme"),
            # 갭업 추적 (W1)
            "prev_close":     w_info.get("prev_close"),
            "open_price":     open_price if open_price > 0 else None,
            "gap_pct":        round(gap_pct, 2) if gap_pct != 0 else None,
            "entry_slippage_pct": (
                round((fill_price / open_price - 1) * 100, 2)
                if fill_price > 0 and open_price > 0 else None
            ),
            "blocked_reason": blocked_reason,
        })

        results.append({
            "ticker": d.ticker,
            "action": d.action,
            "qty": qty,
            "fill_price": fill_price,
            "hitl_result": hitl_result,
            "order_result": order_result,
        })

    return results


def _model_to_dict(obj) -> dict:
    """Pydantic model → dict, None이면 빈 dict."""
    if obj is None:
        return {}
    return obj.model_dump() if hasattr(obj, "model_dump") else dict(obj)
