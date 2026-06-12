"""
build_daily_prices.py
jongga_v2_results_*.json 의 모든 시그널 종목에 대해
signal_date 이후 일별 시가/고가/저가/종가를 수집하여
marketflow/data/daily_prices.csv 로 저장한다.

사용법:
  python marketflow/engine/build_daily_prices.py
"""

import csv
import glob
import json
import os
import time

import requests
import yfinance as yf
from bs4 import BeautifulSoup

# ── 경로 설정 ────────────────────────────────────────────────────
BASE_DIR = os.path.join(os.path.dirname(__file__), '..')
DATA_DIR = os.path.join(BASE_DIR, 'data')
OUTPUT_CSV = os.path.join(DATA_DIR, 'daily_prices.csv')

FIELDNAMES = ["stock_code", "date", "open", "high", "low", "close", "volume"]

_NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ── 헬퍼 ────────────────────────────────────────────────────────

def _parse_int(val: str) -> int:
    try:
        return int(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return 0


def _naver_date_to_iso(naver_date: str) -> str:
    """'2026.03.05' → '2026-03-05'"""
    return naver_date.replace(".", "-")


# ── 네이버 금융 OHLC 수집 ────────────────────────────────────────

def fetch_ohlc_naver(code: str, since_date: str, fetch_pages: int = 10) -> list[dict]:
    """
    네이버 금융 sise_day 에서 code 종목의 일별 OHLC를 수집한다.
    컬럼: [날짜, 종가, 전일비, 시가, 고가, 저가, 거래량]
    since_date(YYYY-MM-DD) 이후 데이터만 반환. 날짜 오름차순.
    """
    base_url = f"https://finance.naver.com/item/sise_day.naver?code={code}"
    headers = {**_NAVER_HEADERS, "Referer": base_url}
    rows = []
    stop_fetching = False

    for page in range(1, fetch_pages + 1):
        if stop_fetching:
            break
        try:
            resp = requests.get(f"{base_url}&page={page}", headers=headers, timeout=5)
            resp.encoding = "euc-kr"
        except requests.RequestException as e:
            print(f"  [경고] {code} page={page} 요청 실패: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table", class_="type2")
        if not table:
            break

        found_any = False
        for tr in table.find_all("tr"):
            cols = tr.find_all("td")
            if len(cols) < 7:
                continue
            date_text = cols[0].get_text(strip=True)
            if not date_text:
                continue
            found_any = True
            iso_date = _naver_date_to_iso(date_text)

            if iso_date < since_date:
                stop_fetching = True
                break

            rows.append({
                "stock_code": code,
                "date":       iso_date,
                "open":       _parse_int(cols[3].get_text()),
                "high":       _parse_int(cols[4].get_text()),
                "low":        _parse_int(cols[5].get_text()),
                "close":      _parse_int(cols[1].get_text()),
                "volume":     _parse_int(cols[6].get_text()),
            })

        if not found_any:
            break

        time.sleep(0.2)

    rows.sort(key=lambda r: r["date"])
    return rows


# ── yfinance fallback ────────────────────────────────────────────

def fetch_ohlc_yf(code: str, since_date: str) -> list[dict]:
    """yfinance로 OHLC 수집 (네이버 실패 시 fallback)."""
    for suffix in (".KS", ".KQ"):
        try:
            hist = yf.Ticker(code + suffix).history(period="1y")
            if hist.empty:
                continue
            rows = []
            for idx, row in hist.iterrows():
                d = str(idx.date())
                if d < since_date:
                    continue
                rows.append({
                    "stock_code": code,
                    "date":   d,
                    "open":   round(float(row["Open"])),
                    "high":   round(float(row["High"])),
                    "low":    round(float(row["Low"])),
                    "close":  round(float(row["Close"])),
                    "volume": int(row["Volume"]),
                })
            if rows:
                rows.sort(key=lambda r: r["date"])
                return rows
        except Exception:
            continue
    return []


# ── 시그널 종목 로드 ─────────────────────────────────────────────

def load_signals() -> dict[str, str]:
    """
    모든 jongga_v2_results_*.json 에서 {stock_code: earliest_signal_date} 반환.
    """
    pattern = os.path.join(DATA_DIR, "jongga_v2_results_*.json")
    files = sorted(glob.glob(pattern))

    if not files:
        print("결과 파일이 없습니다:", pattern)
        return {}

    earliest: dict[str, str] = {}
    for fp in files:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        for s in data.get("signals", []):
            code = s.get("stock_code", "")
            sig_date = s.get("signal_date", "")
            if not code or not sig_date:
                continue
            if code not in earliest or sig_date < earliest[code]:
                earliest[code] = sig_date

    return earliest


# ── 메인 ────────────────────────────────────────────────────────

def main():
    print("=== build_daily_prices.py 시작 (OHLC) ===")

    stock_dates = load_signals()
    if not stock_dates:
        print("수집할 종목이 없습니다. 종료합니다.")
        return

    print(f"수집 대상 종목 수: {len(stock_dates)}")

    all_rows: list[dict] = []
    for i, (code, since_date) in enumerate(sorted(stock_dates.items()), 1):
        print(f"[{i}/{len(stock_dates)}] {code}  (since {since_date}) 수집 중...")
        rows = fetch_ohlc_naver(code, since_date=since_date)
        if not rows:
            print(f"  → 네이버 실패, yfinance fallback...")
            rows = fetch_ohlc_yf(code, since_date=since_date)
        print(f"  → {len(rows)}개 행 수집")
        all_rows.extend(rows)

    if not all_rows:
        print("수집된 데이터가 없습니다.")
        return

    all_rows.sort(key=lambda r: (r["stock_code"], r["date"]))

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\n저장 완료: {OUTPUT_CSV}")
    print(f"총 행 수: {len(all_rows)}")
    print("=== 완료 ===")


if __name__ == "__main__":
    main()
