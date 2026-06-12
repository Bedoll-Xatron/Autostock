"""APScheduler 일일 작업 — 매매 파이프라인 전체 실행."""
import asyncio
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from autostock import config
from autostock.db import supabase as db
from autostock.market.kr_holidays import is_trading_day, is_market_hours, MARKET_OPEN
from autostock.research.graph import build_graph
from autostock.research.runner import get_thread_id, run_until_interrupt, resume_graph
from autostock.hitl import hitl_state, telegram_bot as bot_ui
from autostock.trading.executor import execute_decisions
from autostock.trading.trailing_stop import TrailingPosition, add_positions
from autostock.reporting.daily_kpi import run_daily_kpi_report
from autostock.scheduler.morning_refresh import refresh_watchlist_premarket
from autostock.logger import get_logger

log = get_logger(__name__)

_graph = None
_checkpointer = None


def get_graph():
    global _graph, _checkpointer
    if _graph is None:
        _graph, _checkpointer = build_graph()
        log.info("LangGraph 초기화 완료")
    return _graph


async def run_daily_pipeline(
    custom_watchlist: list[dict] | None = None,
    thread_suffix: str = "",
    bypass_bear_regime: bool = False,
) -> None:
    """
    일일 자동매매 파이프라인:
    1. Supabase에서 워치리스트 + 시장 데이터 로드 (custom_watchlist 전달 시 그것을 사용)
    2. LangGraph 실행 → interrupt 지점까지
    3. Telegram HITL 메시지 전송
    4. 응답 대기 (타임아웃: HITL_TIMEOUT_MINUTES)
    5. resume_graph() → 최종 결정
    6. 매매 주문 실행 + Supabase 저장
    7. Telegram 결과 알림
    bypass_bear_regime=True: BEAR 국면에서도 HITL 전송 (오후 급부각 전용)
    """
    if not is_trading_day():
        log.info("오늘은 공휴일/휴장일 — 파이프라인 건너뜀")
        return

    label = "오후" if thread_suffix else "오전"
    log.info("=== %s 파이프라인 시작 (suffix=%r) ===", label, thread_suffix)

    thread_id = get_thread_id(thread_suffix)
    graph = get_graph()

    # ── 1. 데이터 로드 ────────────────────────────────────
    if not thread_suffix:
        log.info("아침 시황 데이터 실시간 갱신 시작 (n8n 엔진 Python 이식본)...")
        try:
            from autostock.market.us_market import run_us_market_update
            run_us_market_update()
        except Exception as e:
            log.error("아침 시황 업데이트 실패 (기존 데이터 사용): %s", e)

    watchlist = custom_watchlist if custom_watchlist is not None else db.fetch_watchlist()
    if not watchlist:
        log.warning("워치리스트가 비어있습니다. 파이프라인 중단.")
        return

    # 프리마켓 갭업 스킵 필터 적용 (W6) — custom_watchlist(오후 급부각)는 제외
    if custom_watchlist is None:
        premarket_skips = db.fetch_premarket_skips_today()
        if premarket_skips:
            before = len(watchlist)
            watchlist = [w for w in watchlist if w["ticker"] not in premarket_skips]
            log.info("프리마켓 필터 적용: %d → %d종목 (%d 제외)",
                     before, len(watchlist), before - len(watchlist))
        if not watchlist:
            log.warning("프리마켓 필터 후 워치리스트 비어있음 — 파이프라인 중단")
            return

    market_data = db.fetch_latest_market_data()
    if not market_data:
        log.warning("시장 데이터 없음 — 기본값으로 진행")
        from datetime import date
        market_data = {
            "date": date.today().isoformat(),
            "fear_greed_score": 50, "fear_greed_rating": "Neutral",
            "vix": 20.0, "vix_movement": "Stable",
            "kospi": 0.0, "kospi_movement": "Stable", "condition": "NORMAL",
        }

    condition = market_data.get("condition", "NORMAL")
    log.info("%s 매매 파이프라인 진입 (오늘 시황: %s)", label, condition)
    
    # ── 거시 경제(매크로) 필터: DANGER 시 관망 ──
    if condition == "DANGER":
        log.warning("거시 지표 DANGER (위험) 감지 — 오늘 신규 매수를 중지하고 현금을 관망합니다.")
        bot_ui.schedule_message("🚨 **매크로 필터 작동**: 오늘 시황이 DANGER(위험) 수준입니다. 신규 종목 분석 및 매수를 하루 쉬어갑니다.")
        return

    # ── 시장 국면 감지 (KODEX200 MA50/MA200) ─────────────
    from autostock.market.market_regime import detect_regime, RegimeLevel
    regime_level, position_scale = detect_regime()
    if regime_level == RegimeLevel.BEAR:
        if bypass_bear_regime:
            log.info("시장 국면 BEAR이지만 오후 급부각 파이프라인 — BEAR 필터 건너뜀")
        else:
            log.warning("시장 국면 BEAR — 신규 매수를 중지합니다.")
            bot_ui.schedule_message("🐻 **시장 국면 BEAR**: KODEX200 추세 약세. 신규 매수를 하루 쉬어갑니다.")
            return
    if regime_level == RegimeLevel.CAUTION:
        log.info("시장 국면 CAUTION — 포지션 규모 70%%로 축소")
        bot_ui.schedule_message("⚠️ **시장 국면 CAUTION**: KODEX200 주의 구간. 포지션 규모를 70%%로 축소하여 진행합니다.")

    # ── 서킷 브레이커 (W5) ──────────────────────────────
    from autostock.trading.circuit_breaker import check_circuit_breaker
    paused, reason = check_circuit_breaker()
    if paused:
        log.warning("서킷 브레이커 발동 — 신규 매수 중단: %s", reason)
        bot_ui.schedule_message(f"🚦 *서킷 브레이커* 발동 — {reason}\n신규 매수 중단")
        return

    # ── 2. 그래프 실행 ────────────────────────────────────
    interrupted_state = await run_until_interrupt(graph, market_data, watchlist, thread_suffix)
    if interrupted_state is None:
        log.error("그래프가 interrupt 없이 완료됨 — 비정상 종료")
        return

    final_decisions = interrupted_state.get("final_decisions", [])
    if not final_decisions:
        log.warning("final_decisions 없음 — 파이프라인 중단")
        return

    # ── 3. HITL 이벤트 등록 + Telegram 전송 ──────────────
    event = hitl_state.register(thread_id)
    ticker_map = {w["ticker"]: w["name"] for w in watchlist}
    
    # Telegram에 보여주기 위해 AI 권장 수량 사전 계산
    from autostock.trading.executor import calc_order_qty
    from autostock.trading.kis_client import get_available_cash
    
    cash = get_available_cash() or 0.0
    bull_reports = interrupted_state.get("bull_reports", {})
    bear_reports = interrupted_state.get("bear_reports", {})
    decisions_dicts = []
    for d in final_decisions:
        dd = d.model_dump()
        dd["ai_qty"] = calc_order_qty(d, cash, position_scale)
        bull = bull_reports.get(d.ticker)
        bear = bear_reports.get(d.ticker)
        dd["bull_score"] = bull.bull_score if bull else None
        dd["bear_score"] = bear.bear_score if bear else None
        decisions_dicts.append(dd)

    effective_condition = market_data.get("condition", "UNKNOWN")
    if bypass_bear_regime and regime_level == RegimeLevel.BEAR:
        effective_condition = "BEAR"
    bot_ui.schedule_hitl_message(
        decisions_dicts,
        thread_id,
        ticker_map,
        market_condition=effective_condition,
    )

    # ── 4. 응답 대기 (모의투자 시 자동 승인) ────────────────
    if config.KIS_SIMULATED_MODE:
        auto_qty = {
            dd["ticker"]: dd["ai_qty"]
            for dd in decisions_dicts
            if dd.get("action") == "BUY" and dd.get("ai_qty", 0) > 0
        }
        hitl_state.resolve(thread_id, "approved", auto_qty)
        log.info("[모의투자] HITL 자동 승인: %s", auto_qty)
        bot_ui.schedule_message(
            "🤖 *[모의투자 자동승인]* AI 결정 즉시 실행\n"
            + (f"매수: {', '.join(auto_qty.keys())}" if auto_qty else "전 종목 HOLD — 매수 없음")
        )
    else:
        timeout_sec = config.HITL_TIMEOUT_MINUTES * 60
        log.info("HITL 대기 중 (타임아웃: %d분)", config.HITL_TIMEOUT_MINUTES)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            log.warning("HITL 타임아웃 — 자동 거절 처리")
            hitl_state.resolve(thread_id, "rejected", {})

    result = hitl_state.get_result(thread_id)
    if result is None:
        log.error("HITL 결과 없음 — 파이프라인 중단")
        return

    status = result["status"]
    approved_qty = result.get("approved_qty", {})
    log.info("HITL 결과: %s approved_qty=%s", status, approved_qty)

    # ── 5. 그래프 재개 ────────────────────────────────────
    await resume_graph(graph, status, approved_qty, thread_suffix)

    # ── 5.5. 장 시작 대기 (09:00 KST 이전 승인 시) ──────────────
    if status == "approved":
        from datetime import datetime as _dt
        _kst = pytz.timezone("Asia/Seoul")
        _now = _dt.now(_kst)
        _open = _now.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0)
        if _now < _open:
            _wait = (_open - _now).total_seconds()
            log.info("장 시작 전 (현재 %s KST) — %.0f초 대기", _now.strftime("%H:%M:%S"), _wait)
            bot_ui.schedule_message(
                f"⏳ 장 시작 전 승인 — {MARKET_OPEN.hour:02d}:{MARKET_OPEN.minute:02d} KST 이후 주문 실행\n"
                f"({int(_wait // 60)}분 {int(_wait % 60)}초 대기)"
            )
            await asyncio.sleep(_wait)

    # ── 6. 매매 실행 ──────────────────────────────────────
    exec_results = await execute_decisions(final_decisions, status, approved_qty, state=interrupted_state, position_scale=position_scale)

    # ── 6-1. 트레일링 손절 감시 시작 (매수 성공 종목만) ───────
    if status == "approved":
        from autostock.trading.trailing_stop import _fixed_stop
        from autostock.trading.risk import compute_stop_pct
        decision_map = {d.ticker: d for d in final_decisions}
        trailing_positions = []
        for r in exec_results:
            if r["action"] != "BUY" or r["qty"] <= 0 or r["ticker"] not in decision_map:
                continue
            if r.get("order_result", {}).get("info_only"):
                continue
            d = decision_map[r["ticker"]]
            entry_price = r.get("fill_price") or d.price_reference or d.stop_loss_price or 0.0
            stop_pct = compute_stop_pct(r["ticker"])
            trailing_positions.append(
                TrailingPosition(
                    ticker=r["ticker"],
                    name=ticker_map.get(r["ticker"], r["ticker"]),
                    qty=r["qty"],
                    avg_price=entry_price,
                    entry_price=entry_price,
                    stop_price=_fixed_stop(entry_price, stop_pct),
                    peak_price=entry_price,
                    stop_pct=stop_pct,
                )
            )
        if trailing_positions:
            add_positions(trailing_positions)

    # ── 7. Telegram 결과 알림 ─────────────────────────────
    bot_ui.schedule_result_notification(exec_results, status)

    hitl_state.cleanup(thread_id)
    log.info("=== %s 파이프라인 완료 ===", label)


async def run_afternoon_scanner_job() -> None:
    """
    15:00 KST — 오후 적응형 파이프라인:
    (공휴일/휴장일 자동 스킵)
    1. run_afternoon.py --output-json → 오전 시그널 요약 + 급부각 워치리스트
    2. 오전 시그널 요약 텔레그램 전송
    3. LangGraph AI 분석 (오전과 동일) → HITL → KIS 주문 → Supabase 저장
    """
    if not is_trading_day():
        log.info("오늘은 공휴일/휴장일 — 오후 스캔 건너뜀")
        return

    import json as _json
    log.info("=== 오후 파이프라인 시작 ===")

    # 1. subprocess --output-json 으로 급부각 워치리스트 수신 (euc-kr 스크래핑 격리)
    script = config.AFTERNOON_SCANNER_SCRIPT
    proc = await asyncio.create_subprocess_exec(
        sys.executable, script, "--output-json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace")
        log.error("오후 스캔 실패 (exit=%d): %s", proc.returncode, err)
        bot_ui.schedule_message("❌ 오후 시그널 스캔 실패 — 로그를 확인하세요.")
        return

    try:
        payload = _json.loads((stdout or b"").decode("utf-8", errors="replace"))
    except Exception as e:
        log.error("오후 스캔 JSON 파싱 실패: %s", e)
        bot_ui.schedule_message("❌ 오후 시그널 JSON 파싱 실패.")
        return

    morning_msg: str = payload.get("morning_msg", "")
    surge_watchlist: list[dict] = payload.get("watchlist", [])

    # 2. 오전 시그널 상태 요약을 텔레그램으로 전송
    if morning_msg:
        bot_ui.schedule_message(morning_msg)

    if not surge_watchlist:
        log.warning("급부각 종목 없음 — 오후 분석 건너뜀")
        bot_ui.schedule_message("📭 오후 급부각 종목 없음 — 추가 매수 없이 마감합니다.")
        return

    log.info("오후 급부각 %d개 → LangGraph 파이프라인 진입", len(surge_watchlist))

    # 3. 오전과 동일한 LangGraph 파이프라인 (thread_suffix="_afternoon" 으로 체크포인트 분리)
    # bypass_bear_regime=True: 급부각 종목은 약세장에서도 강세를 보이는 종목이므로 BEAR 필터 제외
    await run_daily_pipeline(
        custom_watchlist=surge_watchlist,
        thread_suffix="_afternoon",
        bypass_bear_regime=True,
    )

    log.info("=== 오후 파이프라인 완료 ===")


async def run_marketflow_job() -> None:
    """
    16:00 KST — MarketFlow 전략 스캔 실행 후 watch_list 갱신.
    1. daily_update.py --no-telegram 실행 (비동기 subprocess)
       └─ 내부에서 mf_* Supabase 동기화 + watch_list 갱신까지 처리
    2. Telegram 알림
    """
    if not is_trading_day():
        log.info("오늘은 공휴일/휴장일 — MarketFlow 건너뜀")
        return

    log.info("=== MarketFlow 스캔 시작 ===")
    bot_ui.schedule_message("🔍 MarketFlow 전략 스캔 시작...")

    # 1. daily_update.py 실행
    script = config.MARKETFLOW_SCRIPT
    proc = await asyncio.create_subprocess_exec(
        sys.executable, script, "--no-telegram",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        log.error("MarketFlow 스캔 실패 (exit=%d)\n%s", proc.returncode,
                  (stdout or b"").decode("utf-8", errors="replace"))
        bot_ui.schedule_message("❌ MarketFlow 스캔 실패 — 로그를 확인하세요.")
        return
    log.info("MarketFlow 스캔 완료")

    # 2. Telegram 알림 (Supabase 동기화 + watch_list 갱신은 daily_update.py 내에서 처리)
    bot_ui.schedule_message("✅ MarketFlow 스캔 완료 (내일 08:40 분석 예정)")
    log.info("=== MarketFlow 완료 ===")


async def _run_performance_tracker_job() -> None:
    """17:00 KST — 과거 BUY 결정 d5/d10 수익률 계산."""
    if not is_trading_day():
        log.info("오늘은 공휴일/휴장일 — 성과 추적 건너뜀")
        return

    log.info("=== 성과 추적 시작 ===")
    try:
        from autostock.trading.performance_tracker import run_performance_tracker
        results = run_performance_tracker()
        if results:
            summary = ", ".join(
                f"{r['ticker']} d5={r.get('d5_return', 'N/A')}%"
                for r in results[:5]
            )
            bot_ui.schedule_message(f"📊 **성과 추적 완료** ({len(results)}건): {summary}")
        else:
            log.info("성과 추적: 대상 없음")
    except Exception as e:
        log.error("성과 추적 실패: %s", e)
    log.info("=== 성과 추적 완료 ===")


def create_scheduler() -> AsyncIOScheduler:
    """APScheduler 생성 및 일일 작업 등록."""
    kst = pytz.timezone("Asia/Seoul")
    scheduler = AsyncIOScheduler(timezone=kst)

    # 08:00 프리마켓 워치리스트 갭업 필터 (W6)
    scheduler.add_job(
        refresh_watchlist_premarket,
        trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=kst),
        id="morning_refresh",
        name="08:00 프리마켓 갭업 워치리스트 필터",
        replace_existing=True,
        misfire_grace_time=1800,
    )
    log.info("스케줄 등록 [프리마켓]: 매일 08:00 KST")

    # 다중 파이프라인 스케줄 등록
    for i, (hour, minute) in enumerate(config.PIPELINE_SCHEDULES):
        job_id = f"daily_pipeline_{hour:02d}{minute:02d}"
        scheduler.add_job(
            run_daily_pipeline,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=hour,
                minute=minute,
                timezone=kst,
            ),
            id=job_id,
            name=f"자동매매 파이프라인 ({hour:02d}:{minute:02d})",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        log.info(
            "스케줄 등록 [매매]: 매일 %02d:%02d KST (ID: %s)",
            hour, minute, job_id
        )

    # 오후 시그널 스캐너 (기본 15:00 KST)
    scheduler.add_job(
        run_afternoon_scanner_job,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=config.AFTERNOON_SCANNER_HOUR,
            minute=config.AFTERNOON_SCANNER_MINUTE,
            timezone=kst,
        ),
        id="afternoon_scanner",
        name="오후 시그널 스캐너 (재검증 + 급부각)",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    log.info(
        "스케줄 등록 [오후스캔]: 매일 %02d:%02d KST",
        config.AFTERNOON_SCANNER_HOUR, config.AFTERNOON_SCANNER_MINUTE,
    )

    # 오후 MarketFlow 스캔 + watchlist 갱신 (기본 16:00 KST)
    scheduler.add_job(
        run_marketflow_job,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=config.MARKETFLOW_HOUR,
            minute=config.MARKETFLOW_MINUTE,
            timezone=kst,
        ),
        id="marketflow_scan",
        name="MarketFlow 전략 스캔 + watchlist 갱신",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    log.info(
        "스케줄 등록 [스캔]: 매일 %02d:%02d KST",
        config.MARKETFLOW_HOUR, config.MARKETFLOW_MINUTE,
    )

    # 성과 추적 (17:00 KST — 장 마감 후)
    scheduler.add_job(
        _run_performance_tracker_job,
        trigger=CronTrigger(day_of_week="mon-fri", hour=17, minute=0, timezone=kst),
        id="performance_tracker",
        name="매매 성과 추적 (d5/d10 수익률)",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    log.info("스케줄 등록 [성과추적]: 매일 17:00 KST")

    # 일일 KPI 리포트 (17:30 KST)
    scheduler.add_job(
        run_daily_kpi_report,
        trigger=CronTrigger(day_of_week="mon-fri", hour=17, minute=30, timezone=kst),
        id="daily_kpi_report",
        name="일일 KPI 리포트 (갭업 차단/손절률)",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    log.info("스케줄 등록 [KPI]: 매일 17:30 KST")

    return scheduler
