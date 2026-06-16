"""Step D — 6개월 모멘텀 결합 검증.

중소형 유니버스에서 (1) 눌림 진입 후보를, 그 날짜의 횡단면 6개월 모멘텀
상위/하위로 나눠 비교하고, (2) 모멘텀 상위 단독(참고)도 측정.
청산=신규(부분익절+광폭트레일), 비용 반영, alpha=KOSPI 대비.

가설: 외국인(B)은 결합 시 반전했으나, 모멘텀은 결합해도 +가 유지된다.
"""
import sys
import numpy as np
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta

N = int(sys.argv[1]) if len(sys.argv) > 1 else 80
TOP_SKIP = int(sys.argv[2]) if len(sys.argv) > 2 else 120
PERIOD_DAYS = 450
SKIP, LB6, FWD = 21, 126, 10
COST_RT = 0.41
ATR_MULT, STOP_FLOOR, STOP_CEIL = 1.5, 2.5, 6.0
TRAIL_MULT, PARTIAL_R, PARTIAL_FRAC = 2.0, 1.0, 1.0 / 3
TIME_STOP_DAYS, TIME_STOP_THRESH = 10, -1.0
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


def _sp(atrp):
    return 3.0 if atrp <= 0 else max(STOP_FLOOR, min(STOP_CEIL, atrp * ATR_MULT))


def _sim_new(carr, i, entry, sp):
    stop = entry * (1 - sp / 100); trail_w = sp * TRAIL_MULT
    peak = entry; pr = 0.0; pd_ = False
    for j in range(i + 1, len(carr)):
        c = carr[j]; gain = (c / entry - 1) * 100; held = j - i
        if not pd_ and gain >= sp * PARTIAL_R:
            pr = (c / entry - 1) * 100 * PARTIAL_FRAC; pd_ = True; peak = c
            stop = max(stop, peak * (1 - trail_w / 100))
        elif pd_ and c > peak:
            peak = c; stop = max(stop, peak * (1 - trail_w / 100))
        if c <= stop or (held >= TIME_STOP_DAYS and gain <= TIME_STOP_THRESH):
            rem = (1 - PARTIAL_FRAC) if pd_ else 1.0
            return pr + (c / entry - 1) * 100 * rem - COST_RT
    rem = (1 - PARTIAL_FRAC) if pd_ else 1.0
    return pr + (carr[-1] / entry - 1) * 100 * rem - COST_RT


def _rsi(c, n=14):
    d = c.diff(); up = d.clip(lower=0).rolling(n).mean(); dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def main():
    print(f"■ Step D 모멘텀 결합 {SS}~{EE} (중소형 {N}종목)\n")
    uni = get_universe(N)
    print(f"  유니버스 {len(uni)}종목")
    try:
        ks = fdr.DataReader("KS11", SS, EE)["Close"]
    except Exception:
        ks = None

    # ── Pass 1: 종목 데이터 적재 + 일자별 mom6 패널 ──
    store = {}
    mom_panel = {}   # date -> list of mom6
    done = 0
    for code in uni:
        try:
            df = fdr.DataReader(code, SS, EE)
        except Exception:
            continue
        if df is None or len(df) < LB6 + SKIP + FWD + 5:
            continue
        df = df.copy()
        df["MA20"] = df["Close"].rolling(20).mean(); df["MA60"] = df["Close"].rolling(60).mean()
        df["RSI"] = _rsi(df["Close"])
        h, l, c = df["High"], df["Low"], df["Close"]; pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        df["ATRP"] = tr.rolling(14).mean() / c * 100
        carr = c.to_numpy()
        mom6 = np.full(len(df), np.nan)
        for i in range(LB6 + SKIP, len(df)):
            b = carr[i - SKIP]
            if b > 0 and carr[i - SKIP - LB6] > 0:
                mom6[i] = b / carr[i - SKIP - LB6] - 1
        df["mom6"] = mom6
        store[code] = df
        for i in range(LB6 + SKIP, len(df) - FWD - 1):
            if not np.isnan(mom6[i]):
                mom_panel.setdefault(df.index[i], []).append(mom6[i])
        done += 1
        if done % 15 == 0:
            print(f"  ...{done}종목 적재")

    # 일자별 모멘텀 분위 임계
    q80 = {dt: np.quantile(v, 0.8) for dt, v in mom_panel.items() if len(v) >= 10}
    q20 = {dt: np.quantile(v, 0.2) for dt, v in mom_panel.items() if len(v) >= 10}

    # ── Pass 2: 진입 분류 + 청산 시뮬 ──
    pull, mom_only = [], []   # (group, alpha, net)
    for code, df in store.items():
        carr = df["Close"].to_numpy(); idx = df.index
        ma20 = df["MA20"].to_numpy(); ma60 = df["MA60"].to_numpy()
        rsi = df["RSI"].to_numpy(); atrp = df["ATRP"].to_numpy()
        op = df["Open"].to_numpy(); mom6 = df["mom6"].to_numpy()
        cool_p = cool_m = -1
        for i in range(LB6 + SKIP, len(df) - FWD - 1):
            dt = idx[i]
            if dt not in q80 or np.isnan(mom6[i]):
                continue
            close = carr[i]; prev = carr[i - 1]
            if prev > 0 and (close / prev - 1) * 100 >= 3.0:
                continue
            if (close / prev - 1) * 100 >= 5.0:
                continue
            alpha = (carr[i + FWD] / close - 1) * 100
            if ks is not None:
                kp = ks.index.get_indexer([dt], method="nearest")[0]
                if 0 <= kp and kp + FWD < len(ks):
                    alpha -= (ks.iloc[kp + FWD] / ks.iloc[kp] - 1) * 100
            net = _sim_new(carr, i, close, _sp(atrp[i] if not np.isnan(atrp[i]) else 0))
            is_top = mom6[i] >= q80[dt]; is_bot = mom6[i] <= q20[dt]
            # 모멘텀 상위 단독 (참고)
            if is_top and i > cool_m:
                mom_only.append(("mom_top", alpha, net)); cool_m = i + 5
            # 눌림 조건
            uptrend = close > ma60[i] and ma20[i] > ma60[i] and ma60[i] > 0
            pull_ok = (abs(close / ma20[i] - 1) * 100 <= 2.0) or (35 <= rsi[i] <= 52)
            if uptrend and pull_ok and close > op[i] and i > cool_p:
                grp = "top" if is_top else ("bot" if is_bot else "mid")
                pull.append((grp, alpha, net)); cool_p = i + 5

    def _rep(tag, sub):
        if not sub:
            print(f"  {tag:<24} 0건"); return
        a = np.array([x[1] for x in sub]); net = np.array([x[2] for x in sub])
        w = net[net > 0]; lo = net[net <= 0]
        payoff = (w.mean() / abs(lo.mean())) if len(lo) and lo.mean() != 0 else float("inf")
        print(f"  {tag:<24}{len(sub):>5}건  alpha {a.mean():+6.2f}%p  "
              f"승률 {(net>0).mean()*100:5.1f}%  기댓값 {net.mean():+6.2f}%  손익비 {payoff:5.2f}")

    print(f"\n■ 결과 (눌림 {len(pull)}건, 모멘텀상위단독 {len(mom_only)}건)\n")
    _rep("눌림 전체", pull)
    _rep("눌림 + 모멘텀 상위", [x for x in pull if x[0] == "top"])
    _rep("눌림 + 모멘텀 하위", [x for x in pull if x[0] == "bot"])
    _rep("모멘텀 상위 단독(참고)", mom_only)
    print("\n해석: '눌림+모멘텀 상위'가 '눌림 전체/하위'보다 alpha·기댓값 높고 +면 결합 엣지 확정.")


if __name__ == "__main__":
    main()
