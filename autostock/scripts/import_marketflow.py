"""MarketFlow mf_* 테이블 → watch_list 동기화.

Supabase mf_* 테이블을 우선 조회하고, 비어있으면 로컬 JSON 파일로 폴백합니다.
"""
import json
import os
import FinanceDataReader as fdr
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from autostock import config
from autostock.db import supabase as db
from autostock.logger import get_logger

log = get_logger(__name__)

_MAX_DATA_AGE_DAYS = 5
_TOP_N = 3


@dataclass
class SignalEntry:
    """ticker 하나에 대한 병합된 시그널 정보."""
    ticker: str
    name: str
    sector: str = ""
    prev_close: float = 0.0
    strategy_scores: list[tuple[str, float]] = field(default_factory=list)
    foreign_5d: Optional[int] = None
    inst_5d: Optional[int] = None
    signal_strength: Optional[str] = None
    theme: Optional[str] = None
    rotation_phase: Optional[str] = None
    change_pct: Optional[float] = None

    @property
    def signal_score(self) -> Optional[float]:
        return max(s for _, s in self.strategy_scores) if self.strategy_scores else None

    @property
    def strategies_str(self) -> str:
        return ",".join(f"{n}:{s:.0f}" for n, s in sorted(self.strategy_scores, key=lambda x: -x[1]))

    @property
    def strategy_count(self) -> int:
        return len(self.strategy_scores)

    def merge(self, other: "SignalEntry") -> None:
        self.strategy_scores.extend(other.strategy_scores)
        if self.foreign_5d is None and other.foreign_5d is not None:
            self.foreign_5d = other.foreign_5d
        if self.inst_5d is None and other.inst_5d is not None:
            self.inst_5d = other.inst_5d
        if self.signal_strength is None and other.signal_strength is not None:
            self.signal_strength = other.signal_strength
        if self.theme is None and other.theme is not None:
            self.theme = other.theme
        if self.rotation_phase is None and other.rotation_phase is not None:
            self.rotation_phase = other.rotation_phase
        if self.change_pct is None and other.change_pct is not None:
            self.change_pct = other.change_pct
        if self.prev_close <= 0 and other.prev_close > 0:
            self.prev_close = other.prev_close
        if not self.sector and other.sector:
            self.sector = other.sector


def _cutoff() -> str:
    return (date.today() - timedelta(days=_MAX_DATA_AGE_DAYS)).isoformat()


def _latest_date(table: str) -> Optional[str]:
    try:
        res = (
            db.get_client()
            .table(table)
            .select("date")
            .order("date", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0]["date"] if res.data else None
    except Exception as e:
        log.warning("%s 날짜 조회 실패: %s", table, e)
        return None


def _load_json(filename: str) -> Optional[dict]:
    path = os.path.join(config.MARKETFLOW_DATA_DIR, filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("JSON 파싱 실패 %s: %s", filename, e)
        return None


def _to_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _to_int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fetch_vcp() -> list[SignalEntry]:
    latest = _latest_date("mf_vcp")
    if latest and latest >= _cutoff():
        try:
            res = (
                db.get_client()
                .table("mf_vcp")
                .select("stock_code,stock_name,score,grade,pivot_high,foreign_5d,inst_5d")
                .eq("date", latest)
                .order("score", desc=True)
                .limit(_TOP_N)
                .execute()
            )
            entries = []
            for r in (res.data or []):
                entries.append(SignalEntry(
                    ticker=r["stock_code"],
                    name=r["stock_name"],
                    prev_close=_to_float(r.get("pivot_high")),
                    strategy_scores=[("vcp", _to_float(r.get("score")))],
                    foreign_5d=_to_int(r.get("foreign_5d")),
                    inst_5d=_to_int(r.get("inst_5d")),
                    signal_strength=r.get("grade"),
                ))
            if entries:
                log.info("mf_vcp(DB) 상위 %d개 (date=%s): %s", len(entries), latest,
                         [(e.ticker, e.signal_score) for e in entries])
                return entries
        except Exception as e:
            log.warning("mf_vcp DB 조회 실패, JSON 폴백: %s", e)

    # JSON 폴백
    data = _load_json("vcp_signals.json")
    if not data or data.get("date", "") < _cutoff():
        log.warning("mf_vcp JSON도 오래됨")
        return []
    entries = []
    for s in sorted(data.get("signals", []), key=lambda x: -_to_float(x.get("score")))[:_TOP_N]:
        ticker = str(s.get("code", "")).strip()
        if ticker:
            grade = str(s.get("grade", "N/A"))
            if s.get("is_surge"):
                grade = f"{grade}(S)"
            entries.append(SignalEntry(
                ticker=ticker,
                name=str(s.get("name", ticker)),
                prev_close=_to_float(s.get("pivot_high")),
                strategy_scores=[("vcp", _to_float(s.get("score")))],
                foreign_5d=_to_int(s.get("foreign_5d")),
                inst_5d=_to_int(s.get("inst_5d")),
                signal_strength=grade,
            ))
    log.info("mf_vcp(JSON) 상위 %d개: %s", len(entries), [(e.ticker, e.signal_score) for e in entries])
    return entries


def _fetch_jongga() -> list[SignalEntry]:
    latest = _latest_date("mf_jongga")
    if latest and latest >= _cutoff():
        try:
            res = (
                db.get_client()
                .table("mf_jongga")
                .select("stock_code,stock_name,score_total,current_price,foreign_5d,inst_5d,change_pct")
                .eq("date", latest)
                .order("score_total", desc=True)
                .limit(_TOP_N)
                .execute()
            )
            entries = []
            for r in (res.data or []):
                entries.append(SignalEntry(
                    ticker=r["stock_code"],
                    name=r["stock_name"],
                    prev_close=_to_float(r.get("current_price")),
                    strategy_scores=[("jongga", _to_float(r.get("score_total")))],
                    foreign_5d=_to_int(r.get("foreign_5d")),
                    inst_5d=_to_int(r.get("inst_5d")),
                    change_pct=r.get("change_pct"),
                ))
            if entries:
                log.info("mf_jongga(DB) 상위 %d개 (date=%s): %s", len(entries), latest,
                         [(e.ticker, e.signal_score) for e in entries])
                return entries
        except Exception as e:
            log.warning("mf_jongga DB 조회 실패, JSON 폴백: %s", e)

    # JSON 폴백
    data = _load_json("jongga_v2_latest.json")
    if not data or data.get("date", "") < _cutoff():
        log.warning("mf_jongga JSON도 오래됨")
        return []
    entries = []
    for s in sorted(data.get("signals", []),
                    key=lambda x: -_to_float((x.get("score") or {}).get("total") if isinstance(x.get("score"), dict) else x.get("score_total")))[:_TOP_N]:
        ticker = str(s.get("stock_code", "")).strip()
        if not ticker:
            continue
        score_block = s.get("score", {}) or {}
        score = _to_float(score_block.get("total") if isinstance(score_block, dict) else s.get("score_total"))
        entries.append(SignalEntry(
            ticker=ticker,
            name=str(s.get("stock_name", ticker)),
            prev_close=_to_float(s.get("current_price")),
            strategy_scores=[("jongga", score)],
            foreign_5d=_to_int(s.get("foreign_5d")),
            inst_5d=_to_int(s.get("inst_5d")),
            change_pct=s.get("change_pct"),
        ))
    log.info("mf_jongga(JSON) 상위 %d개: %s", len(entries), [(e.ticker, e.signal_score) for e in entries])
    return entries


def _fetch_flow() -> list[SignalEntry]:
    latest = _latest_date("mf_flow")
    if latest and latest >= _cutoff():
        try:
            res = (
                db.get_client()
                .table("mf_flow")
                .select("ticker,name,score,price,foreign_flow,institution_flow,signal_strength,change_pct")
                .eq("date", latest)
                .order("score", desc=True)
                .limit(_TOP_N)
                .execute()
            )
            entries = []
            for r in (res.data or []):
                entries.append(SignalEntry(
                    ticker=r["ticker"],
                    name=r["name"],
                    prev_close=_to_float(r.get("price")),
                    strategy_scores=[("flow", _to_float(r.get("score")))],
                    foreign_5d=_to_int(r.get("foreign_flow")),
                    inst_5d=_to_int(r.get("institution_flow")),
                    signal_strength=r.get("signal_strength"),
                    change_pct=r.get("change_pct"),
                ))
            if entries:
                log.info("mf_flow(DB) 상위 %d개 (date=%s): %s", len(entries), latest,
                         [(e.ticker, e.signal_score) for e in entries])
                return entries
        except Exception as e:
            log.warning("mf_flow DB 조회 실패, JSON 폴백: %s", e)

    data = _load_json("flow_momentum_latest.json")
    if not data or data.get("date", "") < _cutoff():
        log.warning("mf_flow JSON도 오래됨")
        return []
    entries = []
    for s in sorted(data.get("signals", []), key=lambda x: -_to_float(x.get("score")))[:_TOP_N]:
        ticker = str(s.get("ticker", "")).strip()
        if ticker:
            entries.append(SignalEntry(
                ticker=ticker,
                name=str(s.get("name", ticker)),
                prev_close=_to_float(s.get("price")),
                strategy_scores=[("flow", _to_float(s.get("score")))],
                foreign_5d=_to_int(s.get("foreign_flow")),
                inst_5d=_to_int(s.get("institution_flow")),
                signal_strength=s.get("signal_strength"),
                change_pct=s.get("change_pct"),
            ))
    log.info("mf_flow(JSON) 상위 %d개: %s", len(entries), [(e.ticker, e.signal_score) for e in entries])
    return entries


def _fetch_narrative() -> list[SignalEntry]:
    latest = _latest_date("mf_narrative")
    if latest and latest >= _cutoff():
        try:
            res = (
                db.get_client()
                .table("mf_narrative")
                .select("ticker,name,score,price,theme,change_pct")
                .eq("date", latest)
                .order("score", desc=True)
                .limit(_TOP_N)
                .execute()
            )
            entries = []
            for r in (res.data or []):
                entries.append(SignalEntry(
                    ticker=r["ticker"],
                    name=r["name"],
                    sector=str(r.get("theme") or ""),
                    prev_close=_to_float(r.get("price")),
                    strategy_scores=[("narrative", _to_float(r.get("score")))],
                    theme=r.get("theme"),
                    change_pct=r.get("change_pct"),
                ))
            if entries:
                log.info("mf_narrative(DB) 상위 %d개 (date=%s): %s", len(entries), latest,
                         [(e.ticker, e.signal_score) for e in entries])
                return entries
        except Exception as e:
            log.warning("mf_narrative DB 조회 실패, JSON 폴백: %s", e)

    data = _load_json("narrative_momentum_latest.json")
    if not data or data.get("date", "") < _cutoff():
        log.warning("mf_narrative JSON도 오래됨")
        return []
    entries = []
    for s in sorted(data.get("signals", []), key=lambda x: -_to_float(x.get("score")))[:_TOP_N]:
        ticker = str(s.get("ticker", "")).strip()
        if ticker:
            entries.append(SignalEntry(
                ticker=ticker,
                name=str(s.get("name", ticker)),
                sector=str(s.get("theme") or ""),
                prev_close=_to_float(s.get("price")),
                strategy_scores=[("narrative", _to_float(s.get("score")))],
                theme=s.get("theme"),
                change_pct=s.get("change_pct"),
            ))
    log.info("mf_narrative(JSON) 상위 %d개: %s", len(entries), [(e.ticker, e.signal_score) for e in entries])
    return entries


def _fetch_sector() -> list[SignalEntry]:
    latest = _latest_date("mf_sector")
    if latest and latest >= _cutoff():
        try:
            res = (
                db.get_client()
                .table("mf_sector")
                .select("ticker,name,score,sector,price,rotation_phase")
                .eq("date", latest)
                .order("score", desc=True)
                .limit(_TOP_N)
                .execute()
            )
            entries = []
            for r in (res.data or []):
                entries.append(SignalEntry(
                    ticker=r["ticker"],
                    name=r["name"],
                    sector=str(r.get("sector") or ""),
                    prev_close=_to_float(r.get("price")),
                    strategy_scores=[("sector", _to_float(r.get("score")))],
                    rotation_phase=r.get("rotation_phase"),
                    change_pct=r.get("change_pct"),
                ))
            if entries:
                log.info("mf_sector(DB) 상위 %d개 (date=%s): %s", len(entries), latest,
                         [(e.ticker, e.signal_score) for e in entries])
                return entries
        except Exception as e:
            log.warning("mf_sector DB 조회 실패, JSON 폴백: %s", e)

    data = _load_json("sector_rotation_latest.json")
    if not data or data.get("date", "") < _cutoff():
        log.warning("mf_sector JSON도 오래됨")
        return []
    entries = []
    for s in sorted(data.get("signals", []), key=lambda x: -_to_float(x.get("score")))[:_TOP_N]:
        ticker = str(s.get("ticker", "")).strip()
        if ticker:
            entries.append(SignalEntry(
                ticker=ticker,
                name=str(s.get("name", ticker)),
                sector=str(s.get("sector") or ""),
                prev_close=_to_float(s.get("price")),
                strategy_scores=[("sector", _to_float(s.get("score")))],
                rotation_phase=s.get("rotation_phase"),
                change_pct=s.get("change_pct"),
            ))
    log.info("mf_sector(JSON) 상위 %d개: %s", len(entries), [(e.ticker, e.signal_score) for e in entries])
    return entries


def _fetch_contrarian() -> list[SignalEntry]:
    latest = _latest_date("mf_contrarian")
    if latest and latest >= _cutoff():
        try:
            res = (
                db.get_client()
                .table("mf_contrarian")
                .select("ticker,name,score,price,change_pct")
                .eq("date", latest)
                .order("score", desc=True)
                .limit(_TOP_N)
                .execute()
            )
            entries = []
            for r in (res.data or []):
                entries.append(SignalEntry(
                    ticker=r["ticker"],
                    name=r["name"],
                    prev_close=_to_float(r.get("price")),
                    strategy_scores=[("contrarian", _to_float(r.get("score")))],
                    change_pct=r.get("change_pct"),
                ))
            if entries:
                log.info("mf_contrarian(DB) 상위 %d개 (date=%s): %s", len(entries), latest,
                         [(e.ticker, e.signal_score) for e in entries])
                return entries
        except Exception as e:
            log.warning("mf_contrarian DB 조회 실패, JSON 폴백: %s", e)

    data = _load_json("contrarian_latest.json")
    if not data or data.get("date", "") < _cutoff():
        log.warning("mf_contrarian JSON도 오래됨")
        return []
    entries = []
    for s in sorted(data.get("signals", []), key=lambda x: -_to_float(x.get("score")))[:_TOP_N]:
        ticker = str(s.get("ticker", "")).strip()
        if ticker:
            entries.append(SignalEntry(
                ticker=ticker,
                name=str(s.get("name", ticker)),
                prev_close=_to_float(s.get("price")),
                strategy_scores=[("contrarian", _to_float(s.get("score")))],
                change_pct=s.get("change_pct"),
            ))
    log.info("mf_contrarian(JSON) 상위 %d개: %s", len(entries), [(e.ticker, e.signal_score) for e in entries])
    return entries


def _fetch_price_fdr(ticker: str) -> float:
    try:
        df = fdr.DataReader(ticker)
        return float(df["Close"].iloc[-1])
    except Exception:
        return 0.0


def _collect_signals() -> list[SignalEntry]:
    """모든 mf_* 테이블에서 시그널 수집 후 ticker별 병합."""
    fetchers = [
        _fetch_vcp,
        _fetch_jongga,
        _fetch_flow,
        _fetch_narrative,
        _fetch_sector,
        _fetch_contrarian,
    ]
    merged: dict[str, SignalEntry] = {}
    for fetch_fn in fetchers:
        for entry in fetch_fn():
            if entry.ticker in merged:
                merged[entry.ticker].merge(entry)
            else:
                merged[entry.ticker] = entry

    result = list(merged.values())

    # prev_close 보정 — 0이면 FDR 조회
    for entry in result:
        if entry.prev_close <= 0:
            entry.prev_close = _fetch_price_fdr(entry.ticker)

    # 멀티컨펌 → 단일전략 순 정렬
    result.sort(key=lambda e: (-e.strategy_count, -(e.signal_score or 0)))
    return result


def _delete_oldest(n: int) -> None:
    if n <= 0:
        return
    try:
        res = (
            db.get_client()
            .table("watch_list")
            .select("ticker")
            .order("created_at", desc=False)
            .limit(n)
            .execute()
        )
        tickers = [r["ticker"] for r in (res.data or [])]
        if not tickers:
            return
        db.get_client().table("watch_list").delete().in_("ticker", tickers).execute()
        log.info("watch_list 오래된 종목 %d개 삭제: %s", len(tickers), tickers[:5])
    except Exception as e:
        log.error("watch_list 삭제 실패: %s", e)


def import_to_watchlist() -> int:
    """MarketFlow mf_* 테이블 → watch_list upsert."""
    entries = _collect_signals()
    if not entries:
        log.info("오늘자 시그널 없음 — watch_list 변경 없음")
        return 0

    _delete_oldest(len(entries))

    count = 0
    for e in entries:
        ok = db.add_to_watchlist(
            ticker=e.ticker,
            name=e.name,
            sector=e.sector,
            prev_close=e.prev_close,
            signal_score=e.signal_score,
            strategies=e.strategies_str,
            strategy_count=e.strategy_count,
            foreign_5d=e.foreign_5d,
            inst_5d=e.inst_5d,
            signal_strength=e.signal_strength,
            theme=e.theme,
            rotation_phase=e.rotation_phase,
            change_pct=e.change_pct,
        )
        if ok:
            count += 1
            log.info(
                "watch_list upsert: %s %s | %s | 전략수:%d",
                e.ticker, e.name, e.strategies_str, e.strategy_count,
            )
        else:
            log.warning("watch_list upsert 실패: %s", e.ticker)

    log.info("MarketFlow import 완료: %d개", count)
    return count


if __name__ == "__main__":
    n = import_to_watchlist()
    print(f"완료: {n}개 종목 upsert")
