"""Step C — 검증된 엣지 결합 백테스트.

진입 = 중소형주 + 눌림(상승추세 MA20 부근 반등) 후보를, 외국인 5일순매수/거래량
       상위(High) vs 하위(Low)로 나눠 비교.
사이징 = ATR(가정), 청산 = 신규(부분익절+광폭트레일), 비용 반영.

가설: 눌림 후보 중 '외국인 순매수 상위'를 고르면 alpha·기댓값이 +로 돈다.
데이터: OHLCV=FinanceDataReader, 외국인 순매수=Naver frgn (라이브 동일 소스).
"""
import sys, re, time
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
import FinanceDataReader as fdr
from datetime import datetime, timedelta

N = int(sys.argv[1]) if len(sys.argv) > 1 else 25
PAGES = int(sys.argv[2]) if len(sys.argv) > 2 else 8
TOP_SKIP = int(sys.argv[3]) if len(sys.argv) > 3 else 120
PERIOD_DAYS, FWD_K, COST_RT = 400, 10, 0.41
ATR_MULT, STOP_FLOOR, STOP_CEIL = 1.5, 2.5, 6.0
TRAIL_MULT, PARTIAL_R, PARTIAL_FRAC = 2.0, 1.0, 1.0 / 3
TIME_STOP_DAYS, TIME_STOP_THRESH = 10, -1.0
END = datetime.today(); START = END - timedelta(days=PERIOD_DAYS)
SS, EE = START.strftime("%Y-%m-%d"), END.strftime("%Y-%m-%d")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}


def get_universe(n, top_skip=TOP_SKIP):
    frames = []
    for mkt in ("KOSPI", "KOSDAQ"):
        try:
            frames.append(fdr.StockListing(mkt))
        except Exception:
            pass
    lst = pd.concat(frames, ignore_index=True)
    capcol = next((c for c in ["Marcap", "MarketCap", "시가총액"] if c in lst.columns), None)
    codecol = "Code" if "Code" in lst.columns else "Symbol"
    namecol = "Name" if "Name" in lst.columns else None
    lst = lst.dropna(subset=[capcol]).sort_values(capcol, ascending=False).reset_index(drop=True)
    out = []
    for idx, r in lst.iterrows():
        if idx < top_skip:
            continue
        nm = str(r.get(namecol, "")) if namecol else ""
        if any(k in nm for k in ["ETF", "ETN", "우", "스팩", "리츠", "인버스", "레버리지", "선물"]):
            continue
        out.append(str(r[codecol]).zfill(6))
        if len(out) >= n:
            break
    return out


def _pint(s):
    s = re.sub(r"[^\d\-]", "", s or "")
    return int(s) if s and s not in ("-", "") else 0


def fetch_frgn(code, pages):
    out = []
    for p in range(1, pages + 1):
        url = f"https://finance.naver.com/item/frgn.naver?code={code}&page={p}"
        try:
            r = requests.get(url, headers={**HEADERS, "Referer": url}, timeout=6); r.encoding = "euc-kr"
        except Exception:
            break
        tabs = BeautifulSoup(r.text, "html.parser").find_all("table", class_="type2")
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
            out.append({"date": pd.Timestamp(dt.replace(".", "-")), "frgn": _pint(td[6].get_text())})
            cnt += 1
        if cnt == 0:
            break
        time.sleep(0.25)
    return pd.DataFrame(out).drop_duplicates("date").set_index("date") if out else None


def _sp(atrp):
    return 3.0 if atrp <= 0 else max(STOP_FLOOR, min(STOP_CEIL, atrp * ATR_MULT))


def _sim_new(close_arr, i, entry, sp):
    stop = entry * (1 - sp / 100); trail_w = sp * TRAIL_MULT
    peak = entry; pr = 0.0; pd_ = False
    for j in range(i + 1, len(close_arr)):
        c = close_arr[j]; gain = (c / entry - 1) * 100; held = j - i
        if not pd_ and gain >= sp * PARTIAL_R:
            pr = (c / entry - 1) * 100 * PARTIAL_FRAC; pd_ = True; peak = c
            stop = max(stop, peak * (1 - trail_w / 100))
        elif pd_ and c > peak:
            peak = c; stop = max(stop, peak * (1 - trail_w / 100))
        if c <= stop or (held >= TIME_STOP_DAYS and gain <= TIME_STOP_THRESH):
            rem = (1 - PARTIAL_FRAC) if pd_ else 1.0
            return pr + (c / entry - 1) * 100 * rem - COST_RT
    rem = (1 - PARTIAL_FRAC) if pd_ else 1.0
    return pr + (close_arr[-1] / entry - 1) * 100 * rem - COST_RT


def _rsi(c, n=14):
    d = c.diff(); up = d.clip(lower=0).rolling(n).mean(); dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def main():
    print(f"■ Step C 결합 백테스트 {SS}~{EE} (중소형 {N}종목, 눌림+외국인, 신규청산/비용)\n")
    uni = get_universe(N)
    print(f"  유니버스 {len(uni)}종목 (시총 {TOP_SKIP}위 이후)")
    try:
        ks = fdr.DataReader("KS11", SS, EE)["Close"]
    except Exception:
        ks = None
    ev = []   # 눌림 진입 이벤트: f_frgn, alpha, net
    done = 0
    for code in uni:
        try:
            df = fdr.DataReader(code, SS, EE)
        except Exception:
            continue
        if df is None or len(df) < 90:
            continue
        fg = fetch_frgn(code, PAGES)
        if fg is None:
            continue
        df = df.copy()
        df["frgn"] = fg["frgn"].reindex(df.index)
        df["MA20"] = df["Close"].rolling(20).mean(); df["MA60"] = df["Close"].rolling(60).mean()
        df["vol20"] = df["Volume"].rolling(20).mean()
        df["RSI"] = _rsi(df["Close"])
        h, l, c = df["High"], df["Low"], df["Close"]; pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        df["ATRP"] = tr.rolling(14).mean() / c * 100
        df["frgn5"] = df["frgn"].rolling(5).sum()
        df["f_frgn"] = df["frgn5"] / df["vol20"].replace(0, np.nan)
        carr = df["Close"].to_numpy()
        idx = df.index
        cool = -1
        for i in range(60, len(df) - FWD_K - 1):
            if i <= cool:
                continue
            close = float(c.iloc[i]); o = float(df["Open"].iloc[i])
            ma20 = df["MA20"].iloc[i]; ma60 = df["MA60"].iloc[i]
            ff = df["f_frgn"].iloc[i]; rsi = df["RSI"].iloc[i]
            if pd.isna(ma20) or pd.isna(ma60) or pd.isna(ff) or ma20 <= 0 or ma60 <= 0:
                continue
            prev = float(c.iloc[i - 1])
            if prev > 0 and (close / prev - 1) * 100 >= 3.0:   # 갭업 차단
                continue
            if (close / prev - 1) * 100 >= 5.0:
                continue
            uptrend = close > ma60 and ma20 > ma60
            pullback = abs(close / ma20 - 1) * 100 <= 2.0 or (pd.notna(rsi) and 35 <= rsi <= 52)
            if not (uptrend and pullback and close > o):
                continue
            alpha = (carr[i + FWD_K] / close - 1) * 100
            if ks is not None:
                kpos = ks.index.get_indexer([idx[i]], method="nearest")[0]
                if 0 <= kpos and kpos + FWD_K < len(ks):
                    alpha -= (ks.iloc[kpos + FWD_K] / ks.iloc[kpos] - 1) * 100
            atrp = float(df["ATRP"].iloc[i]) if pd.notna(df["ATRP"].iloc[i]) else 0.0
            net = _sim_new(carr, i, close, _sp(atrp))
            ev.append((float(ff), alpha, net))
            cool = i + 3
        done += 1
        if done % 8 == 0:
            print(f"  ...{done}종목, 누적 눌림진입 {len(ev)}건")

    if not ev:
        print("\n눌림 진입 이벤트 없음."); return
    d = pd.DataFrame(ev, columns=["f_frgn", "alpha", "net"])
    hi_th, lo_th = d["f_frgn"].quantile(0.6), d["f_frgn"].quantile(0.4)

    def _rep(tag, sub):
        if len(sub) == 0:
            print(f"  {tag:<24} 0건"); return
        net = sub["net"]; w = net[net > 0]; lo = net[net <= 0]
        payoff = (w.mean() / abs(lo.mean())) if len(lo) and lo.mean() != 0 else float("inf")
        print(f"  {tag:<24}{len(sub):>5}건  alpha {sub['alpha'].mean():+6.2f}%p  "
              f"승률 {(net>0).mean()*100:5.1f}%  기댓값 {net.mean():+6.2f}%  손익비 {payoff:5.2f}")

    print(f"\n■ 눌림 진입 {len(d)}건 — 외국인 순매수 상/하위 비교\n")
    _rep("눌림 전체", d)
    _rep("눌림 + 외국인 상위(High)", d[d["f_frgn"] >= hi_th])
    _rep("눌림 + 외국인 하위(Low)", d[d["f_frgn"] <= lo_th])
    print("\n해석: 'High'가 alpha·기댓값에서 '전체/Low'보다 뚜렷이 높고 alpha(+)면, 결합 엣지 확인.")


if __name__ == "__main__":
    main()
