"""유니버스 진입 엣지 백테스트 + 팩터 IC (#4).

라이브 진입 게이트(가격/거래량 기반)를 유니버스 전체에 적용해
 1) 진입 이벤트의 forward 5/10/20일 수익률과 KOSPI 대비 초과수익(alpha)
 2) 각 팩터의 정보계수(IC = 팩터값 vs forward 10일 수익률 상관)
 3) 신규 청산(ATR손절+1R부분익절+광폭트레일) 적용 시 비용 반영 기대값
을 측정한다. H1/H2 반기 분할로 안정성도 확인.

주의: 뉴스/수급/공매도/LLM 점수는 OHLCV로 재현 불가 → 본 측정에서 제외.
"""
import sys
import numpy as np
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta

# ── 파라미터 ──────────────────────────────────────────────────
UNIVERSE_SIZE = int(sys.argv[1]) if len(sys.argv) > 1 else 40
PERIOD_DAYS = 400
FWD = [5, 10, 20]
COST_RT = 0.41                       # 왕복 거래비용 %
RVOL_MIN, MA20_FLOOR, CHANGE_MAX, GAP_BLOCK = 1.5, -2.0, 5.0, 3.0
ATR_MULT, STOP_FLOOR, STOP_CEIL = 1.5, 2.5, 6.0
TRAIL_MULT, PARTIAL_R, PARTIAL_FRAC = 2.0, 1.0, 1.0 / 3
TIME_STOP_DAYS, TIME_STOP_THRESH = 10, -1.0

END = datetime.today()
START = END - timedelta(days=PERIOD_DAYS)
SS, EE = START.strftime("%Y-%m-%d"), END.strftime("%Y-%m-%d")

CURATED = [
    "005930","000660","373220","207940","005380","000270","068270","005490","051910","006400",
    "035420","035720","051900","028260","105560","055550","086790","015760","034730","012330",
    "009150","011200","032830","003550","066570","096770","017670","030200","018260","010130",
    "011170","009830","024110","316140","071050","139480","004020","010950","267250","047050",
    "078930","000810","021240","161390","097950","271560","112610","302440","326030","007070",
]


def get_universe(n):
    try:
        lst = fdr.StockListing("KOSPI")
        cols = lst.columns
        capcol = next((c for c in ["Marcap", "MarketCap", "Market Cap", "시가총액"] if c in cols), None)
        codecol = "Code" if "Code" in cols else ("Symbol" if "Symbol" in cols else None)
        namecol = "Name" if "Name" in cols else ("Symbol" if "Symbol" in cols else None)
        if capcol and codecol:
            lst = lst.dropna(subset=[capcol]).sort_values(capcol, ascending=False)
            out = []
            for _, r in lst.iterrows():
                nm = str(r.get(namecol, ""))
                if any(k in nm for k in ["ETF", "ETN", "우", "스팩", "리츠", "인버스", "레버리지"]):
                    continue
                out.append((str(r[codecol]).zfill(6), nm))
                if len(out) >= n:
                    break
            if out:
                return out
    except Exception as e:
        print(f"  (StockListing 실패 → 큐레이션 폴백: {e})")
    return [(c, c) for c in CURATED[:n]]


def _stop_pct_from_atrp(atrp):
    return 3.0 if atrp <= 0 else max(STOP_FLOOR, min(STOP_CEIL, atrp * ATR_MULT))


def _sim_new(df, i, entry, sp):
    stop = entry * (1 - sp / 100)
    trail_w = sp * TRAIL_MULT
    peak = entry
    partial_ret, partial_done = 0.0, False
    for j in range(i + 1, len(df)):
        c = float(df["Close"].iloc[j]); gain = (c / entry - 1) * 100
        held = j - i
        if not partial_done and gain >= sp * PARTIAL_R:
            partial_ret = (c / entry - 1) * 100 * PARTIAL_FRAC
            partial_done = True; peak = c
            stop = max(stop, peak * (1 - trail_w / 100))
        elif partial_done and c > peak:
            peak = c; stop = max(stop, peak * (1 - trail_w / 100))
        if c <= stop or (held >= TIME_STOP_DAYS and gain <= TIME_STOP_THRESH):
            rem = (1 - PARTIAL_FRAC) if partial_done else 1.0
            return partial_ret + (c / entry - 1) * 100 * rem - COST_RT
    rem = (1 - PARTIAL_FRAC) if partial_done else 1.0
    return partial_ret + (float(df["Close"].iloc[-1]) / entry - 1) * 100 * rem - COST_RT


def main():
    print(f"■ 유니버스 진입 엣지 백테스트  {SS}~{EE}  (목표 {UNIVERSE_SIZE}종목)")
    uni = get_universe(UNIVERSE_SIZE)
    try:
        ks = fdr.DataReader("KS11", SS, EE)["Close"]
        ks_ret = ks.pct_change()
    except Exception:
        ks_ret = None

    rows = []   # 각 진입 이벤트 레코드
    done = 0
    for code, name in uni:
        try:
            df = fdr.DataReader(code, SS, EE)
        except Exception:
            continue
        if df is None or len(df) < 80:
            continue
        df = df.copy()
        df["MA20"] = df["Close"].rolling(20).mean()
        df["MA60"] = df["Close"].rolling(60).mean()
        df["RVOL"] = df["Volume"] / df["Volume"].rolling(20).mean()
        df["Chg"] = df["Close"].pct_change() * 100
        h, l, c = df["High"], df["Low"], df["Close"]; pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        df["ATRP"] = tr.rolling(14).mean() / c * 100
        n = len(df)
        last_exit = -1
        for i in range(60, n - max(FWD) - 1):
            if i <= last_exit:
                continue
            ma20 = df["MA20"].iloc[i]; rv = df["RVOL"].iloc[i]; chg = df["Chg"].iloc[i]
            close = float(c.iloc[i]); prev = float(c.iloc[i - 1])
            if pd.isna(ma20) or pd.isna(rv) or ma20 <= 0:        continue
            if rv < RVOL_MIN:                                    continue
            if (close / ma20 - 1) * 100 <= MA20_FLOOR:           continue
            if chg >= CHANGE_MAX:                                continue
            if prev > 0 and (close / prev - 1) * 100 >= GAP_BLOCK: continue
            if close <= ma20:                                    continue
            # 팩터
            mom20 = (close / float(c.iloc[i - 20]) - 1) * 100 if c.iloc[i - 20] > 0 else 0
            ma20_dist = (close / ma20 - 1) * 100
            ma60 = df["MA60"].iloc[i]
            ma60_dist = (close / ma60 - 1) * 100 if pd.notna(ma60) and ma60 > 0 else 0
            high60 = float(df["High"].iloc[i - 60:i].max())
            brk = 1.0 if close > high60 else 0.0
            atrp = float(df["ATRP"].iloc[i]) if pd.notna(df["ATRP"].iloc[i]) else 0.0
            rs = 0.0
            if ks_ret is not None:
                ks20 = (ks.iloc[i] / ks.iloc[i - 20] - 1) * 100 if i - 20 >= 0 else 0
                rs = mom20 - ks20
            # forward 수익률
            fwd = {}
            for k in FWD:
                fwd[k] = (float(c.iloc[i + k]) / close - 1) * 100
            # 벤치마크 forward (동일 구간)
            bench10 = 0.0
            if ks_ret is not None:
                bench10 = (ks.iloc[i + 10] / ks.iloc[i] - 1) * 100
            sp = _stop_pct_from_atrp(atrp)
            net = _sim_new(df, i, close, sp)
            rows.append(dict(rvol=rv, ma20_dist=ma20_dist, ma60_dist=ma60_dist, mom20=mom20,
                             rs=rs, brk=brk, chg=chg, atrp=atrp,
                             f5=fwd[5], f10=fwd[10], f20=fwd[20], bench10=bench10, net=net))
            last_exit = i + 3   # 중복 진입 완화
        done += 1
        if done % 10 == 0:
            print(f"  ...{done}종목 처리, 누적 진입 {len(rows)}건")

    if not rows:
        print("진입 이벤트 없음 (데이터/네트워크 확인)"); return
    d = pd.DataFrame(rows)
    N = len(d)

    print(f"\n■ 진입 이벤트: {N}건  (처리 {done}종목)\n")
    print("── forward 수익률 (단순 보유, 비용 미반영) ──")
    for k in FWD:
        col = d[f"f{k}"]
        print(f"  +{k:>2}일: 평균 {col.mean():+.2f}%  중앙 {col.median():+.2f}%  승률 {(col>0).mean()*100:.1f}%")
    print(f"  +10일 KOSPI 대비 초과(alpha): {(d['f10']-d['bench10']).mean():+.2f}%p")

    print("\n── 신규 청산 적용 기대값 (비용 반영) ──")
    net = d["net"]
    wins = net[net > 0]; losses = net[net <= 0]
    wr = (net > 0).mean() * 100
    payoff = (wins.mean() / abs(losses.mean())) if len(losses) and losses.mean() != 0 else float("inf")
    print(f"  기댓값/거래 {net.mean():+.2f}%  승률 {wr:.1f}%  "
          f"평균수익 {wins.mean():+.2f}%  평균손실 {losses.mean():+.2f}%  손익비 {payoff:.2f}")

    print("\n── 팩터 IC (값 vs forward 10일 수익률, Pearson / Spearman) ──")
    for f in ["rvol", "ma20_dist", "ma60_dist", "mom20", "rs", "brk", "chg", "atrp"]:
        try:
            pear = d[f].corr(d["f10"])
            spear = d[f].corr(d["f10"], method="spearman")
            print(f"  {f:<10} IC(p)={pear:+.3f}  IC(s)={spear:+.3f}")
        except Exception:
            pass

    print("\n── 반기 안정성 (전반/후반) ──")
    half = N // 2
    for tag, seg in (("전반", d.iloc[:half]), ("후반", d.iloc[half:])):
        s = seg["net"]
        print(f"  {tag}: {len(s)}건  기댓값 {s.mean():+.2f}%  승률 {(s>0).mean()*100:.1f}%  f10평균 {seg['f10'].mean():+.2f}%")


if __name__ == "__main__":
    main()
