import asyncio
import json
import os
import time
from datetime import date, datetime
from typing import List, Optional

import aiohttp
import pandas as pd
from bs4 import BeautifulSoup

from config import VCPConfig
from indicators import atr
from vcp_detector import VCPResult, detect_vcp, score_vcp


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}


# ── 비동기 데이터 수집 ──────────────────────────────


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> dict:
    async with session.get(url, headers=HEADERS) as resp:
        return await resp.json()


async def _fetch_top_by_volume(session: aiohttp.ClientSession, market: str, top_n: int) -> list[dict]:
    """거래대금 상위 종목을 가져온다."""
    url = (
        f"https://m.stock.naver.com/api/stocks/marketValue/{market}"
        f"?page=1&pageSize={top_n}"
    )
    data = await _fetch_json(session, url)
    results = []
    for s in data.get("stocks", []):
        name = s.get("stockName", "")
        # ETF/스팩/우선주 제외
        if any(kw in name for kw in ["스팩", "SPAC", "ETF", "ETN", "리츠", "인버스", "레버리지"]):
            continue
        if name.endswith("우") or any(f"{n}우" in name for n in "123"):
            continue
        results.append({
            "code": s["itemCode"],
            "name": name,
            "market": market.upper(),
        })
    return results


async def _fetch_chart_html(session: aiohttp.ClientSession, code: str, page: int) -> str:
    url = f"https://finance.naver.com/item/sise_day.naver?code={code}&page={page}"
    headers = {**HEADERS, "Referer": f"https://finance.naver.com/item/sise_day.naver?code={code}"}
    async with session.get(url, headers=headers) as resp:
        raw = await resp.read()
        return raw.decode("euc-kr", errors="replace")


def _parse_int(val: str) -> int:
    try:
        return int(str(val).replace(",", "").replace("\t", "").replace("\n", ""))
    except (ValueError, TypeError):
        return 0


async def _fetch_chart_df(
    session: aiohttp.ClientSession,
    code: str,
    days: int = 80,
    cutoff_date: Optional[date] = None,
) -> Optional[pd.DataFrame]:
    """일봉 데이터를 DataFrame으로 반환한다.

    Args:
        cutoff_date: 이 날짜 이후 데이터는 제외 (해당일 포함)
    """
    rows = []
    page = 1
    while len(rows) < days:
        html = await _fetch_chart_html(session, code, page)
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_="type2")
        if not table:
            break

        found = False
        for tr in table.find_all("tr"):
            cols = tr.find_all("td")
            if len(cols) < 7:
                continue
            date_text = cols[0].get_text(strip=True)
            if not date_text:
                continue
            found = True

            # cutoff_date 이후 데이터 스킵
            try:
                row_date = datetime.strptime(date_text, "%Y.%m.%d").date()
            except ValueError:
                continue
            if cutoff_date and row_date > cutoff_date:
                continue

            rows.append({
                "date": date_text,
                "open": _parse_int(cols[3].get_text()),
                "high": _parse_int(cols[4].get_text()),
                "low": _parse_int(cols[5].get_text()),
                "close": _parse_int(cols[1].get_text()),
                "volume": _parse_int(cols[6].get_text()),
            })
            if len(rows) >= days:
                break

        if not found:
            break
        page += 1
        await asyncio.sleep(0.15)

    if len(rows) < 60:
        return None

    rows.reverse()
    return pd.DataFrame(rows)


async def _fetch_supply(
    session: aiohttp.ClientSession,
    code: str,
    cutoff_date: Optional[date] = None,
) -> dict:
    """외국인/기관 5일 순매수를 가져온다."""
    url = f"https://finance.naver.com/item/frgn.naver?code={code}&page=1"
    headers = {**HEADERS, "Referer": f"https://finance.naver.com/item/frgn.naver?code={code}"}
    async with session.get(url, headers=headers) as resp:
        raw = await resp.read()
        html = raw.decode("euc-kr", errors="replace")

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table", class_="type2")
    if len(tables) < 2:
        return {"foreign_5d": 0, "inst_5d": 0}

    table = tables[1]
    foreign_sum = 0
    inst_sum = 0
    count = 0

    for tr in table.find_all("tr"):
        cols = tr.find_all("td")
        if len(cols) < 9:
            continue
        date_text = cols[0].get_text(strip=True)
        if not date_text:
            continue

        # cutoff_date 이후 데이터 스킵
        if cutoff_date:
            try:
                row_date = datetime.strptime(date_text, "%Y.%m.%d").date()
                if row_date > cutoff_date:
                    continue
            except ValueError:
                continue

        inst_sum += _parse_int(cols[5].get_text())
        foreign_sum += _parse_int(cols[6].get_text())
        count += 1
        if count >= 5:
            break

    return {"foreign_5d": foreign_sum, "inst_5d": inst_sum}


# ── 메인 스캐너 ─────────────────────────────────────


async def scan_vcp(top_n: int = 50, cutoff_date: Optional[date] = None):
    config = VCPConfig()
    label_date = cutoff_date.isoformat() if cutoff_date else date.today().isoformat()

    print(f"\n{'='*70}")
    print(f"  VCP 스캐너  |  기준일: {label_date}  |  거래대금 상위 {top_n}개")
    if cutoff_date:
        print(f"  ※ {label_date} 이후 데이터 제외 (과거 시점 분석)")
    print(f"{'='*70}\n")

    async with aiohttp.ClientSession() as session:
        # 1. KOSPI + KOSDAQ 거래대금 상위 종목 수집
        kospi_task = _fetch_top_by_volume(session, "KOSPI", top_n)
        kosdaq_task = _fetch_top_by_volume(session, "KOSDAQ", top_n)
        kospi_stocks, kosdaq_stocks = await asyncio.gather(kospi_task, kosdaq_task)

        # 거래대금 상위 top_n개로 제한
        all_stocks = (kospi_stocks + kosdaq_stocks)[:top_n]
        total = len(all_stocks)
        print(f"  수집 완료: {total}개 종목 (KOSPI {len(kospi_stocks)}, KOSDAQ {len(kosdaq_stocks)})\n")

        # 2. 각 종목 VCP 분석
        results: List[dict] = []
        sem = asyncio.Semaphore(5)

        async def _analyze(idx: int, stock: dict):
            async with sem:
                code = stock["code"]
                name = stock["name"]

                df = await _fetch_chart_df(session, code, cutoff_date=cutoff_date)
                if df is None:
                    print(f"  [{idx+1:>2}/{total}] {name:<12} - 데이터 부족, 스킵")
                    return

                result = detect_vcp(df, config)

                if result.detected:
                    # ATR% 계산
                    atr_series = atr(df, period=14)
                    atrp = (atr_series.iloc[-1] / df["close"].iloc[-1]) * 100

                    # 스코어링
                    score = score_vcp(result, atrp, config)
                    result.score = score

                    # 수급 데이터
                    await asyncio.sleep(0.15)
                    supply = await _fetch_supply(session, code, cutoff_date=cutoff_date)

                    # --- 수급 밀집도(Accumulation) 반영 ---
                    # 외인/기관 동반 매수 시 S등급 가점 및 확인
                    is_accumulating = supply["foreign_5d"] > 0 and supply["inst_5d"] > 0
                    
                    if result.is_surge_candidate and is_accumulating:
                        # 더 강력한 응축+수급 합치 신호
                        pass 
                    elif is_accumulating and result.grade in ["A", "B"] and result.tightness < 6.0:
                        # 응축도는 조금 낮아도 수급이 깡패면 S등급 부여
                        result.is_surge_candidate = True

                    results.append({
                        "code": code,
                        "name": name,
                        "market": stock["market"],
                        "grade": result.grade,
                        "score": result.score,
                        "c1": result.c1,
                        "c2": result.c2,
                        "c3": result.c3,
                        "r12": result.r12,
                        "r23": result.r23,
                        "pivot_high": result.pivot_high,
                        "foreign_5d": supply["foreign_5d"],
                        "inst_5d": supply["inst_5d"],
                        "tightness": result.tightness,
                        "vdu_ratio": result.vdu_ratio,
                        "is_surge": result.is_surge_candidate,
                        "is_acc": is_accumulating,
                    })
                    print(f"  [{idx+1:>2}/{total}] {name:<12} - VCP {result.grade}등급 감지! (점수: {score})")
                else:
                    print(f"  [{idx+1:>2}/{total}] {name:<12} - 미감지")

        tasks = [_analyze(i, s) for i, s in enumerate(all_stocks)]
        await asyncio.gather(*tasks)

    # 3. 결과 정렬 (등급순 → 점수 내림차순)
    results.sort(key=lambda r: (GRADE_ORDER.get(r["grade"], 9), -r["score"]))

    # 4. JSON 저장
    output = {
        "date": label_date,
        "total_scanned": total,
        "vcp_detected": len(results),
        "signals": results,
    }
    data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
    os.makedirs(data_dir, exist_ok=True)
    output_path = os.path.join(data_dir, 'vcp_signals.json')
    date_str = label_date.replace('-', '')  # YYYYMMDD
    dated_path = os.path.join(data_dir, f'vcp_signals_{date_str}.json')

    for path in (output_path, dated_path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

    # 5. 결과 테이블 출력
    print(f"\n{'='*90}")
    print(f"  VCP 감지 결과: {len(results)}개 / {total}개 스캔")
    print(f"{'='*90}")

    if results:
        print(
            f"  {'#':>2}  {'종목명':<12} {'VCP등급':>6} {'점수':>4} {'S':>2}"
            f"  {'C1→C2→C3':<18} {'T-ness':>7} {'VDU':>6} {'외인5일':>10}"
        )
        print(f"  {'-'*76}")

        for i, r in enumerate(results, 1):
            contraction = f"{r['c1']:.1f}→{r['c2']:.1f}→{r['c3']:.1f}"
            surge_mark = "★" if r.get("is_surge") else " "
            print(
                f"  {i:>2}  {r['name']:<12} {r['grade']:>6} {r['score']:>4} {surge_mark:>2}"
                f"  {contraction:<18} {r['tightness']:>6.1f}% {r['vdu_ratio']:>6.2f} {r['foreign_5d']:>+10,}"
            )
    else:
        print("  VCP 패턴 감지 종목 없음")

    print(f"\n  => {output_path} 저장 완료")
    print(f"  => {dated_path} 저장 완료")
    print(f"{'='*90}\n")

    return results


if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="VCP 스캐너")
    parser.add_argument("date_str", nargs="?", help="기준일 (YYYY-MM-DD)")
    parser.add_argument("--no-telegram", action="store_true", help="텔레그램 전송 건너뜀 (이미 이 스크립트에는 텔레그램 기능이 없음)")
    args = parser.parse_args()

    cutoff = None
    if args.date_str:
        try:
            cutoff = date.fromisoformat(args.date_str)
        except ValueError:
            print(f"경고: 잘못된 날짜 형식 '{args.date_str}'. 무시하고 오늘 날짜로 진행합니다.")

    asyncio.run(scan_vcp(cutoff_date=cutoff))
