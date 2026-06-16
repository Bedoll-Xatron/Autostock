"""장기 모멘텀(횡단면) 알파 검증 (#4 ②, Jegadeesh-Titman).

최근 1개월(21일) 스킵한 3개월(63d)/6개월(126d) 수익률로 종목을 횡단면 랭크해,
상위 vs 하위 분위의 forward 20일 수익률·KOSPI 대비 alpha·롱숏 스프레드를 측정.
'정석 가격 팩터(모멘텀)에 알파가 있는가'에 답 — 단기 20일은 음(−)이었음.
"""
import sys
import numpy as np
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta

N = int(sys.argv[1]) if len(sys.argv) > 1 else 80
TOP_SKIP = int(sys.argv[2]) if len(sys.argv) > 2 else 120
PERIOD_DAYS = 450
SKIP, FWD = 21, 20            # 최근 1개월 스킵, 1개월 보유
LB3, LB6 = 63, 126           # 3개월/6개월 lookback
END = datetime.today(); START = END - timedelta(days=PERIOD_DAYS)
SS, EE = START.strftime("%Y-%m-%d"), END.strftime("%Y-%m-%d")


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


def main():
    print(f"■ 장기 모멘텀 횡단면 검증 {SS}~{EE} (중소형 {N}종목, 스킵{SKIP}/보유{FWD})\n")
    uni = get_universe(N)
    print(f"  유니버스 {len(uni)}종목")
    try:
        ks = fdr.DataReader("KS11", SS, EE)["Close"]
    except Exception:
        ks = None

    rows = []
    done = 0
    for code in uni:
        try:
            df = fdr.DataReader(code, SS, EE)
        except Exception:
            continue
        if df is None or len(df) < LB6 + SKIP + FWD + 5:
            continue
        c = df["Close"].to_numpy(); idx = df.index
        for i in range(LB6 + SKIP, len(df) - FWD - 1):
            base = c[i - SKIP]
            if base <= 0 or c[i - SKIP - LB3] <= 0 or c[i - SKIP - LB6] <= 0 or c[i] <= 0:
                continue
            mom3 = base / c[i - SKIP - LB3] - 1
            mom6 = base / c[i - SKIP - LB6] - 1
            fwd = c[i + FWD] / c[i] - 1
            kfwd = 0.0
            if ks is not None:
                kp = ks.index.get_indexer([idx[i]], method="nearest")[0]
                if 0 <= kp and kp + FWD < len(ks):
                    kfwd = ks.iloc[kp + FWD] / ks.iloc[kp] - 1
            rows.append((idx[i], mom3, mom6, fwd, fwd - kfwd))
        done += 1
        if done % 15 == 0:
            print(f"  ...{done}종목, 누적 {len(rows)}관측")

    if not rows:
        print("\n관측 없음."); return
    d = pd.DataFrame(rows, columns=["date", "mom3", "mom6", "fwd", "alpha"])
    print(f"\n■ 관측 {len(d)}건 — 모멘텀 팩터 검증\n")

    for fac in ["mom3", "mom6"]:
        ic = d[fac].corr(d["fwd"], method="spearman")
        q80, q20 = d[fac].quantile(0.8), d[fac].quantile(0.2)
        top, bot = d[d[fac] >= q80], d[d[fac] <= q20]
        # 날짜 중립 롱숏: 각 날짜 상위20% - 하위20% forward, 날짜평균
        ls = []
        for _, g in d.groupby("date"):
            if len(g) < 10:
                continue
            t = g[g[fac] >= g[fac].quantile(0.8)]["fwd"].mean()
            b = g[g[fac] <= g[fac].quantile(0.2)]["fwd"].mean()
            if pd.notna(t) and pd.notna(b):
                ls.append(t - b)
        ls_mean = np.mean(ls) * 100 if ls else float("nan")
        lab = "3개월" if fac == "mom3" else "6개월"
        print(f"  [{lab} 모멘텀] IC(s)={ic:+.3f}")
        print(f"     상위20% fwd={top['fwd'].mean()*100:+.2f}% alpha={top['alpha'].mean()*100:+.2f}%p | "
              f"하위20% fwd={bot['fwd'].mean()*100:+.2f}% alpha={bot['alpha'].mean()*100:+.2f}%p")
        print(f"     풀 스프레드(상-하)={ (top['fwd'].mean()-bot['fwd'].mean())*100:+.2f}%p | "
              f"날짜중립 롱숏={ls_mean:+.2f}%p\n")
    print("해석: IC(+)·상위 alpha(+)·롱숏(+)이면 모멘텀에 선별 알파 존재(상위 모멘텀 매수 유효).")


if __name__ == "__main__":
    main()
