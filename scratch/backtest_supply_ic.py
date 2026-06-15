"""수급(외국인/기관 순매수) 팩터 IC 측정 (#4-B).

라이브와 동일한 Naver frgn 페이지에서 일별 외국인/기관 순매수(주)를 스크래핑해,
거래량으로 정규화한 5일 누적 순매수가 forward 10일 수익률을 예측하는지(IC) 측정.
가격/거래량 팩터엔 +엣지가 없었으므로(#4-A), 진짜 알파가 수급에 있는지 검증.

주의: pykrx 투자자 엔드포인트는 현재 환경에서 동작하지 않아 Naver 스크래핑 사용.
"""
import sys
import re
import time
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

UNIVERSE_SIZE = int(sys.argv[1]) if len(sys.argv) > 1 else 15
PAGES = int(sys.argv[2]) if len(sys.argv) > 2 else 7   # 페이지당 ~25일
FWD_K = 10

CURATED = [
    "005930","000660","373220","207940","005380","000270","068270","005490","051910","006400",
    "035420","035720","051900","028260","105560","055550","086790","015760","034730","012330",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def _pint(s):
    s = re.sub(r"[^\d\-]", "", s or "")
    return int(s) if s and s not in ("-", "") else 0


def fetch_frgn(code, pages):
    """Naver frgn 페이지에서 일별 [날짜, 종가, 거래량, 기관순매수, 외국인순매수] 수집."""
    out = []
    for p in range(1, pages + 1):
        url = f"https://finance.naver.com/item/frgn.naver?code={code}&page={p}"
        try:
            r = requests.get(url, headers={**HEADERS, "Referer": url}, timeout=6)
            r.encoding = "euc-kr"
        except Exception:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        tabs = soup.find_all("table", class_="type2")
        if len(tabs) < 2:
            break
        cnt = 0
        for tr in tabs[1].find_all("tr"):
            td = tr.find_all("td")
            if len(td) < 9:
                continue
            dt = td[0].get_text(strip=True)
            if not re.match(r"\d{4}\.\d{2}\.\d{2}", dt):
                continue
            out.append({
                "date": dt.replace(".", "-"),
                "close": _pint(td[1].get_text()),
                "vol": _pint(td[4].get_text()),
                "inst": _pint(td[5].get_text()),
                "frgn": _pint(td[6].get_text()),
            })
            cnt += 1
        if cnt == 0:
            break
        time.sleep(0.3)
    if not out:
        return None
    df = pd.DataFrame(out).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    return df if len(df) >= 40 else None


def main():
    print(f"■ 수급 팩터 IC (Naver frgn, {UNIVERSE_SIZE}종목 × {PAGES}p)\n")
    pool = []
    ok = 0
    for code in CURATED[:UNIVERSE_SIZE]:
        df = fetch_frgn(code, PAGES)
        if df is None:
            print(f"  [{code}] 데이터 부족/실패")
            continue
        df["vol20"] = df["vol"].rolling(20).mean()
        df["frgn5"] = df["frgn"].rolling(5).sum()
        df["inst5"] = df["inst"].rolling(5).sum()
        df["f_frgn"] = df["frgn5"] / df["vol20"].replace(0, np.nan)
        df["f_inst"] = df["inst5"] / df["vol20"].replace(0, np.nan)
        df["f_both"] = (df["frgn5"] + df["inst5"]) / df["vol20"].replace(0, np.nan)
        df["fwd10"] = df["close"].shift(-FWD_K) / df["close"] - 1
        sub = df.dropna(subset=["f_frgn", "f_inst", "f_both", "fwd10"])
        pool.append(sub[["f_frgn", "f_inst", "f_both", "fwd10"]])
        ok += 1
        print(f"  [{code}] {len(df)}일 수집")

    if not pool:
        print("\n수급 데이터 수집 실패 — 소스 접근 불가."); return
    d = pd.concat(pool, ignore_index=True)
    print(f"\n■ 풀 표본 {len(d)}건 ({ok}종목)  — forward {FWD_K}일 수익률과의 IC\n")
    for f, label in (("f_frgn", "외국인 5일순매수/거래량"),
                     ("f_inst", "기관 5일순매수/거래량"),
                     ("f_both", "외+기관 5일순매수/거래량")):
        pear = d[f].corr(d["fwd10"])
        spear = d[f].corr(d["fwd10"], method="spearman")
        # 상위 20% vs 하위 20% forward 수익률 비교
        q80, q20 = d[f].quantile(0.8), d[f].quantile(0.2)
        top = d[d[f] >= q80]["fwd10"].mean() * 100
        bot = d[d[f] <= q20]["fwd10"].mean() * 100
        print(f"  {label:<22} IC(p)={pear:+.3f} IC(s)={spear:+.3f}  "
              f"상위20%={top:+.2f}% 하위20%={bot:+.2f}% 스프레드={top-bot:+.2f}%p")
    print("\n해석: IC(+)·스프레드(+)= 순매수 많은 종목이 이후 더 오름(수급에 알파 존재).")


if __name__ == "__main__":
    main()
