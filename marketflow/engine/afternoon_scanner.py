"""오후 15:00 장중 적응형 시그널 스캐너."""

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


# ── 데이터 타입 ────────────────────────────────────────────────────────────────

@dataclass
class MarketRegime:
    kospi_pct: float
    label: str   # "강세" / "중립" / "약세"
    action: str


@dataclass
class MorningSignal:
    code: str
    name: str
    grade: str
    entry_price: int
    stop_price: int
    target_price: int
    original_change_pct: float
    current_price: int = 0
    current_change_pct: float = 0.0
    status: str = "UNKNOWN"  # CONFIRMED / CHASING / CANCELLED / UNKNOWN


@dataclass
class IntradaySurge:
    code: str
    name: str
    market: str
    current_price: int
    change_pct: float
    trading_value: int  # 원 단위


# ── 시장 국면 판단 ─────────────────────────────────────────────────────────────

def get_market_regime() -> MarketRegime:
    """KOSPI 현재 등락률로 시장 국면을 판단한다."""
    url = "https://m.stock.naver.com/api/index/KOSPI/basic"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        pct = float(data.get("fluctuationsRatio", 0))
    except Exception as e:
        log.warning("KOSPI 시세 조회 실패: %s", e)
        pct = 0.0

    if pct >= 0.5:
        return MarketRegime(pct, "강세", "오전 시그널 전체 유지 + 신규 탐색")
    if pct >= -0.5:
        return MarketRegime(pct, "중립", "S/A 등급만 유지, B등급 재검토")
    return MarketRegime(pct, "약세", "전량 보류 — 현금 대기 권고")


# ── 개별 종목 현재가 조회 ──────────────────────────────────────────────────────

def get_current_quote(code: str) -> Tuple[int, float]:
    """현재가와 등락률을 반환한다. 실패 시 (0, 0.0)."""
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        price = int(str(data.get("closePrice", "0")).replace(",", "") or 0)
        pct = float(data.get("fluctuationsRatio", 0))
        return price, pct
    except Exception as e:
        log.debug("현재가 조회 실패 %s: %s", code, e)
        return 0, 0.0


# ── 오전 시그널 로드 및 재검증 ────────────────────────────────────────────────

def load_morning_signals() -> List[MorningSignal]:
    """jongga_v2_latest.json에서 오전 시그널을 로드한다."""
    data_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "jongga_v2_latest.json"
    )
    if not os.path.exists(data_path):
        log.warning("오전 시그널 파일 없음: %s", data_path)
        return []

    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    result = []
    for sig in data.get("signals", []):
        result.append(MorningSignal(
            code=sig["stock_code"],
            name=sig["stock_name"],
            grade=sig["grade"],
            entry_price=sig["entry_price"],
            stop_price=sig["stop_price"],
            target_price=sig["target_price"],
            original_change_pct=sig.get("change_pct", 0.0),
        ))
    return result


def recheck_morning_signals(
    signals: List[MorningSignal], regime: MarketRegime
) -> List[MorningSignal]:
    """각 오전 시그널의 현재 상태를 재검증한다."""
    for sig in signals:
        price, pct = get_current_quote(sig.code)
        sig.current_price = price
        sig.current_change_pct = pct

        if price == 0:
            sig.status = "UNKNOWN"
            continue

        # 손절가 이하 → 즉시 취소
        if price <= sig.stop_price:
            sig.status = "CANCELLED"
            continue

        # 목표가까지 상승분의 80% 이상 달성 → 추격 위험
        upside_total = sig.target_price - sig.entry_price
        if upside_total > 0:
            upside_done = (price - sig.entry_price) / upside_total
            if upside_done >= 0.8:
                sig.status = "CHASING"
                continue

        # 약세 국면 → 전량 보류
        if regime.label == "약세":
            sig.status = "CANCELLED"
            continue

        # 중립 국면에서 B등급 → 재검토(취소)
        if regime.label == "중립" and sig.grade == "B":
            sig.status = "CANCELLED"
            continue

        sig.status = "CONFIRMED"

    return signals


# ── 장중 신규 급부각 종목 스캔 ────────────────────────────────────────────────

def _fetch_intraday_hot(market: str, top_n: int = 30) -> List[IntradaySurge]:
    """sise_quant.naver에서 현재 거래대금 상위 종목을 스크랩한다."""
    sosok = "0" if market.upper() == "KOSPI" else "1"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://finance.naver.com/",
    }
    url = f"https://finance.naver.com/sise/sise_quant.naver?sosok={sosok}&page=1"
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        resp.raise_for_status()
        resp.encoding = "euc-kr"
    except Exception as e:
        log.error("[%s] 장중 핫 종목 수집 실패: %s", market, e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="type_2")
    if not table:
        return []

    results = []
    for row in table.find_all("tr"):
        cols = row.find_all("td")
        if len(cols) < 7:
            continue
        a_tag = cols[1].find("a")
        if not a_tag:
            continue
        m = re.search(r"code=(\d{6})", a_tag.get("href", ""))
        if not m:
            continue
        code = m.group(1)
        name = a_tag.get_text(strip=True)

        close = int(cols[2].get_text(strip=True).replace(",", "") or 0)
        if close == 0:
            continue

        rate_span = cols[4].find("span")
        if rate_span:
            is_neg = "blue02" in " ".join(rate_span.get("class", []))
            rate_txt = rate_span.get_text(strip=True)
        else:
            rate_txt = cols[4].get_text(strip=True)
            is_neg = rate_txt.startswith("-")
        rate_clean = rate_txt.replace(",", "").replace("%", "").lstrip("▲▼+-").strip()
        try:
            change_pct = -float(rate_clean) if is_neg else float(rate_clean)
        except ValueError:
            change_pct = 0.0

        trading_value = int(cols[6].get_text(strip=True).replace(",", "") or 0) * 1_000_000

        results.append(IntradaySurge(
            code=code, name=name, market=market.upper(),
            current_price=close, change_pct=change_pct,
            trading_value=trading_value,
        ))
        if len(results) >= top_n:
            break

    return results


def scan_intraday_surge(
    morning_codes: Set[str],
    min_change_pct: float = 1.5,
    min_trading_value: int = 50_000_000_000,
) -> List[IntradaySurge]:
    """오전 시그널에 없었던 신규 장중 급부각 종목을 반환한다."""
    hot = _fetch_intraday_hot("KOSPI") + _fetch_intraday_hot("KOSDAQ")
    candidates = [
        s for s in hot
        if s.code not in morning_codes
        and s.change_pct >= min_change_pct
        and s.trading_value >= min_trading_value
    ]
    # 등락률 내림차순
    candidates.sort(key=lambda x: x.change_pct, reverse=True)
    return candidates


# ── 텔레그램 메시지 구성 ──────────────────────────────────────────────────────

def _regime_emoji(label: str) -> str:
    return {"강세": "🟢", "중립": "🟡", "약세": "🔴"}.get(label, "⚪")


def build_afternoon_message(
    regime: MarketRegime,
    morning_signals: List[MorningSignal],
    surges: List[IntradaySurge],
) -> str:
    now_str = datetime.now().strftime("%H:%M")
    lines = [
        f"📊 *오후 시그널 업데이트* ({now_str})",
        f"{_regime_emoji(regime.label)} 시장 국면: *{regime.label}* ({regime.kospi_pct:+.2f}%)",
        f"  └ {regime.action}",
        "",
    ]

    confirmed = [s for s in morning_signals if s.status == "CONFIRMED"]
    chasing = [s for s in morning_signals if s.status == "CHASING"]
    cancelled = [s for s in morning_signals if s.status == "CANCELLED"]

    # 유지 시그널
    if confirmed:
        lines.append(f"✅ *오전 시그널 유지* ({len(confirmed)}개)")
        for s in confirmed:
            gap_pct = (s.current_price - s.entry_price) / s.entry_price * 100 if s.entry_price else 0
            gap_str = f"매수가 대비 {gap_pct:+.1f}%"
            lines.append(
                f"  📌 {s.grade}등급 *{s.name}* | "
                f"현재 {s.current_price:,}원 ({s.current_change_pct:+.1f}%) | {gap_str}"
            )
        lines.append("")

    # 추격 위험
    if chasing:
        lines.append(f"⚠️ *추격 위험* ({len(chasing)}개) — 이미 크게 올라 진입 부적합")
        for s in chasing:
            lines.append(f"  📌 {s.name} | 현재 {s.current_price:,}원 ({s.current_change_pct:+.1f}%)")
        lines.append("")

    # 취소 시그널
    if cancelled:
        lines.append(f"✖ *취소된 시그널* ({len(cancelled)}개)")
        for s in cancelled:
            if s.current_price <= s.stop_price:
                reason = "손절가 이탈"
            elif regime.label == "약세":
                reason = "시장 약세"
            else:
                reason = "국면 약화"
            c_str = f"{s.current_change_pct:+.1f}%" if s.current_price > 0 else "조회 실패"
            lines.append(f"  ✖ {s.name} | {c_str} | 사유: {reason}")
        lines.append("")

    # 신규 장중 급부각
    if surges:
        lines.append(f"🔥 *신규 장중 급부각* ({len(surges)}개)")
        for s in surges:
            tv_str = f"{s.trading_value / 100_000_000:,.0f}억"
            lines.append(
                f"  🚀 *{s.name}* ({s.market}) | "
                f"{s.current_price:,}원 ({s.change_pct:+.1f}%) | 거래대금 {tv_str}"
            )
        lines.append("")
        lines.append("  ⚠️ 신규 종목은 뉴스·차트 직접 확인 후 판단하세요")
        lines.append("")

    if not confirmed and not surges:
        lines.append("📭 오후 기준 유효한 시그널 없음 — 현금 대기 권고")
        lines.append("")

    lines.append("⚠️ 투자 참고용이며, 매매의 책임은 본인에게 있습니다.")
    return "\n".join(lines)


# ── 메인 파이프라인 ───────────────────────────────────────────────────────────

def get_surge_watchlist(
    top_n: int = 10,
    min_change_pct: float = 3.0,
    min_trading_value: int = 50_000_000_000,
) -> Tuple[List[dict], str]:
    """
    오후 급부각 종목을 watchlist 형식으로 반환.
    Returns: (watchlist_dicts, 오전_상태_메시지)
    """
    regime = get_market_regime()
    morning_signals = load_morning_signals()
    morning_signals = recheck_morning_signals(morning_signals, regime)

    # 오전 시그널 간략 상태 메시지 (손절가 이탈 / 추격위험 알림)
    now_str = datetime.now().strftime("%H:%M")
    regime_emoji = _regime_emoji(regime.label)
    lines = [
        f"📊 오후 시그널 업데이트 ({now_str})",
        f"{regime_emoji} 시장 국면: {regime.label} ({regime.kospi_pct:+.2f}%)",
    ]
    cancelled = [s for s in morning_signals if s.status == "CANCELLED"]
    chasing = [s for s in morning_signals if s.status == "CHASING"]
    if cancelled:
        lines.append(f"✖ 손절가 이탈 {len(cancelled)}개 — 포지션 정리 권고:")
        for s in cancelled:
            lines.append(f"  └ {s.name} ({s.current_change_pct:+.1f}%)")
    if chasing:
        lines.append(f"⚠️ 추격 위험 {len(chasing)}개 — 신규 진입 자제:")
        for s in chasing:
            lines.append(f"  └ {s.name} ({s.current_change_pct:+.1f}%)")
    morning_msg = "\n".join(lines)

    # 급부각 스캔 → watchlist 형식 변환
    morning_codes = {s.code for s in morning_signals}
    surges = scan_intraday_surge(
        morning_codes,
        min_change_pct=min_change_pct,
        min_trading_value=min_trading_value,
    )
    log.info("급부각 스캔: %d개 → 상위 %d개 선별", len(surges), min(top_n, len(surges)))

    watchlist = []
    for s in surges[:top_n]:
        strength = "강" if s.change_pct >= 15 else ("중" if s.change_pct >= 7 else "약")
        watchlist.append({
            "ticker": s.code,
            "name": s.name,
            "sector": s.market,
            "prev_close": s.current_price,
            "change_pct": round(s.change_pct, 2),
            "signal_score": round(min(s.change_pct / 3.0, 10.0), 1),
            "strategies": "afternoon_surge",
            "strategy_count": 1,
            "signal_strength": strength,
            "theme": f"거래대금 {s.trading_value // 100_000_000:,}억",
        })

    return watchlist, morning_msg


def run_afternoon_scan(no_telegram: bool = False) -> dict:
    """오후 스캔 전체 파이프라인 (레거시/디버그용). 결과 요약 dict를 반환한다."""
    log.info("=== 오후 적응형 스캔 시작 ===")

    regime = get_market_regime()
    log.info("시장 국면: %s (KOSPI %+.2f%%)", regime.label, regime.kospi_pct)

    morning_signals = load_morning_signals()
    log.info("오전 시그널 로드: %d개", len(morning_signals))

    morning_signals = recheck_morning_signals(morning_signals, regime)
    confirmed_cnt = sum(1 for s in morning_signals if s.status == "CONFIRMED")
    chasing_cnt = sum(1 for s in morning_signals if s.status == "CHASING")
    cancelled_cnt = sum(1 for s in morning_signals if s.status == "CANCELLED")
    log.info("재검증 — 유지: %d, 추격위험: %d, 취소: %d", confirmed_cnt, chasing_cnt, cancelled_cnt)

    morning_codes = {s.code for s in morning_signals}
    surges = scan_intraday_surge(morning_codes)
    log.info("신규 장중 급부각: %d개", len(surges))

    msg = build_afternoon_message(regime, morning_signals, surges)

    if no_telegram:
        print(msg)
        log.info("텔레그램 전송 건너뜀 (--no-telegram)")
    else:
        from notifier import _send_long_telegram
        _send_long_telegram(msg)
        log.info("텔레그램 전송 완료")

    log.info("=== 오후 스캔 완료 ===")
    return {
        "regime": regime.label,
        "kospi_pct": regime.kospi_pct,
        "confirmed": confirmed_cnt,
        "chasing": chasing_cnt,
        "cancelled": cancelled_cnt,
        "new_surges": len(surges),
    }
