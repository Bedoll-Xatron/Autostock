"""
supabase_sync.py
================
marketflow/data/ 의 최신 JSON 파일을 Supabase 테이블에 upsert 한다.

단독 실행:
  python supabase_sync.py           # 오늘 날짜 기준 latest 파일 동기화
  python supabase_sync.py --dry-run # DB 쓰기 없이 파싱 결과만 출력
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client, Client

# ── 경로 설정 ────────────────────────────────────────────────
ENGINE_DIR = Path(__file__).resolve().parent
DATA_DIR   = ENGINE_DIR.parent / "data"
ENV_PATH   = ENGINE_DIR.parent.parent / ".env"

load_dotenv(ENV_PATH, override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── Supabase 클라이언트 ──────────────────────────────────────
def _get_client() -> Client:
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY 가 .env에 없습니다.")
    return create_client(url, key)


def _upsert(client: Client, table: str, rows: list[dict], conflict: str, dry_run: bool) -> int:
    """rows 를 table 에 upsert. 성공한 row 수 반환."""
    if not rows:
        return 0
    if dry_run:
        log.info("  [dry-run] %s → %d rows", table, len(rows))
        return len(rows)
    try:
        client.table(table).upsert(rows, on_conflict=conflict).execute()
        log.info("  upsert %s → %d rows", table, len(rows))
        return len(rows)
    except Exception as e:
        log.error("  upsert %s 실패: %s", table, e)
        return 0


# ── JSON 읽기 헬퍼 ───────────────────────────────────────────
def _load(path: Path) -> dict | None:
    if not path.exists():
        log.warning("  파일 없음: %s", path)
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── 전략별 변환 함수 ─────────────────────────────────────────

def _sync_jongga(client: Client, dry_run: bool) -> int:
    data = _load(DATA_DIR / "jongga_v2_latest.json")
    if not data or not data.get("signals"):
        return 0

    date_str = data.get("date")
    if not date_str:
        log.error("jongga 데이터에 date 필드 없음")
        return 0

    rows = []
    for s in data.get("signals", []):
        stock_code = s.get("stock_code")
        stock_name = s.get("stock_name")
        if not stock_code or not stock_name:
            log.warning("stock_code/stock_name 누락, 건너뜀: %s", s)
            continue
        sc = s.get("score", {})
        rows.append({
            "date":                    date_str,
            "stock_code":              stock_code,
            "stock_name":              stock_name,
            "market":                  s.get("market"),
            "grade":                   s.get("grade"),
            "score_total":             sc.get("total"),
            "score_news":              sc.get("news"),
            "score_volume":            sc.get("volume"),
            "score_chart":             sc.get("chart"),
            "score_candle":            sc.get("candle"),
            "score_consolidation":     sc.get("consolidation"),
            "score_supply":            sc.get("supply"),
            "score_retracement":       sc.get("retracement"),
            "score_pullback_support":  sc.get("pullback_support"),
            "llm_reason":              sc.get("llm_reason"),
            "current_price":           s.get("current_price"),
            "entry_price":             s.get("entry_price"),
            "stop_price":              s.get("stop_price"),
            "target_price":            s.get("target_price"),
            "quantity":                s.get("quantity"),
            "position_size":           s.get("position_size"),
            "r_value":                 s.get("r_value"),
            "r_multiplier":            s.get("r_multiplier"),
            "trading_value":           s.get("trading_value"),
            "change_pct":              s.get("change_pct"),
            "foreign_5d":              s.get("foreign_5d"),
            "inst_5d":                 s.get("inst_5d"),
            "quality":                 s.get("quality"),
            "themes":                  s.get("themes", []),
            "news_items":              s.get("news_items", []),
        })
    return _upsert(client, "mf_jongga", rows, "date,stock_code", dry_run)


def _sync_vcp(client: Client, dry_run: bool) -> int:
    data = _load(DATA_DIR / "vcp_signals.json")
    if not data or not data.get("signals"):
        return 0

    date_str = data.get("date")
    if not date_str:
        log.error("vcp 데이터에 date 필드 없음")
        return 0

    rows = []
    for s in data.get("signals", []):
        code = s.get("code")
        name = s.get("name")
        if not code or not name:
            log.warning("vcp code/name 누락, 건너뜀: %s", s)
            continue
        rows.append({
            "date":       date_str,
            "stock_code": code,
            "stock_name": name,
            "market":     s.get("market"),
            "grade":      s.get("grade"),
            "score":      s.get("score"),
            "c1":         s.get("c1"),
            "c2":         s.get("c2"),
            "c3":         s.get("c3"),
            "r12":        s.get("r12"),
            "r23":        s.get("r23"),
            "pivot_high": s.get("pivot_high"),
            "foreign_5d": s.get("foreign_5d"),
            "inst_5d":    s.get("inst_5d"),
        })
    return _upsert(client, "mf_vcp", rows, "date,stock_code", dry_run)


def _sync_flow(client: Client, dry_run: bool) -> int:
    data = _load(DATA_DIR / "flow_momentum_latest.json")
    if not data or not data.get("signals"):
        return 0

    date_str = data.get("date")
    if not date_str:
        log.error("flow 데이터에 date 필드 없음")
        return 0

    rows = []
    for s in data.get("signals", []):
        ticker = s.get("ticker")
        name = s.get("name")
        if not ticker or not name:
            log.warning("flow ticker/name 누락, 건너뜀: %s", s)
            continue
        rows.append({
            "date":             date_str,
            "ticker":           ticker,
            "name":             name,
            "market":           s.get("market"),
            "score":            s.get("score"),
            "flow_score":       s.get("flow_score"),
            "trend_score":      s.get("trend_score"),
            "vol_score":        s.get("vol_score"),
            "foreign_flow":     s.get("foreign_flow"),
            "institution_flow": s.get("institution_flow"),
            "volume_ratio":     s.get("volume_ratio"),
            "signal_strength":  s.get("signal_strength"),
            "price":            s.get("price"),
            "change_pct":       s.get("change_pct"),
            "ma20":             s.get("ma20"),
            "ma60":             s.get("ma60"),
            "trend":            s.get("trend"),
        })
    return _upsert(client, "mf_flow", rows, "date,ticker", dry_run)


def _sync_sector(client: Client, dry_run: bool) -> int:
    data = _load(DATA_DIR / "sector_rotation_latest.json")
    if not data or not data.get("signals"):
        return 0

    date_str = data.get("date")
    if not date_str:
        log.error("sector 데이터에 date 필드 없음")
        return 0

    rows = []
    for s in data.get("signals", []):
        ticker = s.get("ticker")
        name = s.get("name")
        if not ticker or not name:
            log.warning("sector ticker/name 누락, 건너뜀: %s", s)
            continue
        rows.append({
            "date":               date_str,
            "ticker":             ticker,
            "name":               name,
            "market":             s.get("market"),
            "score":              s.get("score"),
            "sector":             s.get("sector"),
            "rotation_phase":     s.get("rotation_phase"),
            "relative_strength":  s.get("relative_strength"),
            "rs_raw":             s.get("rs_raw"),
            "price":              s.get("price"),
            "ma20":               s.get("ma20"),
            "ma60":               s.get("ma60"),
        })
    return _upsert(client, "mf_sector", rows, "date,ticker", dry_run)


def _sync_contrarian(client: Client, dry_run: bool) -> int:
    data = _load(DATA_DIR / "contrarian_latest.json")
    if not data or not data.get("signals"):
        return 0

    date_str = data.get("date")
    if not date_str:
        log.error("contrarian 데이터에 date 필드 없음")
        return 0

    rows = []
    for s in data.get("signals", []):
        ticker = s.get("ticker")
        name = s.get("name")
        if not ticker or not name:
            log.warning("contrarian ticker/name 누락, 건너뜀: %s", s)
            continue
        rows.append({
            "date":                   date_str,
            "ticker":                 ticker,
            "name":                   name,
            "market":                 s.get("market"),
            "score":                  s.get("score"),
            "oversold_score":         s.get("oversold_score"),
            "reversal_probability":   s.get("reversal_probability"),
            "support_level":          s.get("support_level"),
            "rsi":                    s.get("rsi"),
            "price":                  s.get("price"),
            "change_pct":             s.get("change_pct"),
        })
    return _upsert(client, "mf_contrarian", rows, "date,ticker", dry_run)


def _sync_narrative(client: Client, dry_run: bool) -> int:
    data = _load(DATA_DIR / "narrative_momentum_latest.json")
    if not data or not data.get("signals"):
        return 0

    date_str = data.get("date")
    if not date_str:
        log.error("narrative 데이터에 date 필드 없음")
        return 0

    rows = []
    for s in data.get("signals", []):
        ticker = s.get("ticker")
        name = s.get("name")
        if not ticker or not name:
            log.warning("narrative ticker/name 누락, 건너뜀: %s", s)
            continue
        rows.append({
            "date":             date_str,
            "ticker":           ticker,
            "name":             name,
            "market":           s.get("market"),
            "score":            s.get("score"),
            "theme":            s.get("theme"),
            "news_sentiment":   s.get("news_sentiment"),
            "sns_momentum":     s.get("sns_momentum"),
            "narrative_score":  s.get("narrative_score"),
            "news_pts":         s.get("news_pts"),
            "theme_pts":        s.get("theme_pts"),
            "vol_pts":          s.get("vol_pts"),
            "llm_source":       s.get("llm_source"),
            "news_reason":      s.get("news_reason"),
            "price":            s.get("price"),
            "change_pct":       s.get("change_pct"),
            "all_themes":       s.get("all_themes", []),
            "theme_peers":      s.get("theme_peers", []),
        })
    return _upsert(client, "mf_narrative", rows, "date,ticker", dry_run)


# ── 메인 ─────────────────────────────────────────────────────

SYNCS = [
    ("종가베팅 V2",    _sync_jongga),
    ("VCP 패턴",       _sync_vcp),
    ("수급 모멘텀",    _sync_flow),
    ("섹터 로테이션",  _sync_sector),
    ("역발상 반전",    _sync_contrarian),
    ("테마 모멘텀",    _sync_narrative),
]


def sync_all(dry_run: bool = False) -> dict[str, int]:
    """모든 전략 데이터를 Supabase에 동기화. {전략명: upsert된 row수} 반환."""
    client = _get_client()
    results: dict[str, int] = {}

    log.info("=== Supabase 동기화 시작 (dry_run=%s) ===", dry_run)
    for name, fn in SYNCS:
        log.info("[%s]", name)
        results[name] = fn(client, dry_run)

    total = sum(results.values())
    log.info("=== 완료: 총 %d rows ===", total)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MarketFlow → Supabase 동기화")
    parser.add_argument("--dry-run", action="store_true", help="DB 쓰기 없이 파싱만 테스트")
    args = parser.parse_args()

    results = sync_all(dry_run=args.dry_run)

    log.info("\n[ 결과 요약 ]")
    for name, cnt in results.items():
        log.info("  %s: %d개", name, cnt)
