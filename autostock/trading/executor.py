"""매매 주문 실행 — 수량 계산 및 KIS 주문."""
import os

from autostock import config
from autostock.models import FinalDecision
from autostock.trading.kis_client import get_available_cash, get_holding_qty, place_order, get_current_price
from autostock.trading.limit_order import limit_buy_with_timeout
from autostock.trading.entry_gates import check_entry_gates
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


_R_MULTIPLIER_TIERS: list[tuple[float, float]] = [
    (9.0, 1.5),
    (7.0, 1.0),
    (5.0, 0.5),
]


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
    R 기반 매수 수량 계산.

    risk_amount = (available_cash * R_RATIO) * r_multiplier * position_scale
    position_size = risk_amount / stop_loss_pct
    max = available_cash * 50%
    """
    if decision.action != "BUY":
        return 0

    r_multiplier = 0.0
    for threshold, mult in _R_MULTIPLIER_TIERS:
        if decision.confidence >= threshold:
            r_multiplier = mult
            break

    if r_multiplier == 0:
        log.info("calc_order_qty: %s 신뢰도 미달(%.1f) — 매매 보류", decision.ticker, decision.confidence)
        return 0

    entry = decision.price_reference or 0.0
    stop = decision.stop_loss_price or 0.0
    if entry > 0 and 0 < stop < entry:
        stop_loss_pct = (entry - stop) / entry
    else:
        stop_loss_pct = config.STOP_LOSS_PCT  # config.py 단일 정의 (기본 8%)

    risk_amount = available_cash * config.R_RATIO * r_multiplier * position_scale
    position_size = risk_amount / stop_loss_pct
    position_size = min(position_size, available_cash * SINGLE_POSITION_CAP_PCT)

    if entry <= 0:
        return 0

    qty = int(position_size / entry)
    log.info(
        "calc_order_qty: %s confidence=%.1f r_mult=%.1fx scale=%.1f stop=%.1f%% size=%.0f qty=%d",
        decision.ticker, decision.confidence, r_multiplier, position_scale,
        stop_loss_pct * 100, position_size, qty,
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

    for d in decisions:
        qty = 0
        fill_price = 0
        order_result = None
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

            if qty > 0:
                try:
                    if d.action == "BUY":
                        # 정보 전용 모드: 실제 매수 금지
                        order_result = {"filled": False, "info_only": True, "message": "매수 추천 알림 (실행 안함)"}
                        log.info("execute_decisions: %s BUY 추천됨 (시스템 매수 차단)", d.ticker)
                    else:
                        # 정보 전용 모드: 실제 매도 금지
                        order_result = {"info_only": True, "message": "매도 추천 알림 (실행 안함)"}
                        log.info("execute_decisions: %s SELL 추천됨 (시스템 매도 차단)", d.ticker)
                except Exception as e:
                    log.error("execute_decisions: %s order mock failed — %s", d.ticker, e)
                    order_result = {"error": str(e)}
        if d.action == "BUY" and qty > 0:
            _sec = w_info.get("sector", "Unknown")
            _sector_count[_sec] = _sector_count.get(_sec, 0) + 1
            _new_count += 1

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
