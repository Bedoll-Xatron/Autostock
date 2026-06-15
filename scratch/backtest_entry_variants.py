"""진입 변형 비교 백테스트 (#4-A).

같은 유니버스/청산/비용에서 진입 정의만 바꿔 forward 수익률·KOSPI 대비 alpha·
신규청산 기대값을 비교한다. 목표: 음(−) IC인 '강세추종'을 '눌림/되돌림'으로
바꾸면 alpha가 +로 도는지 측정.

변형:
  base       : 현행 게이트 (RVOL≥1.5, close>MA20, 이격>-2%, 강세추종)
  pullback   : 상승추세(close>MA60, MA20>MA60)에서 MA20 부근 눌림 + 당일 반등
  pull_rsi   : 상승추세에서 RSI14 35~50 (과매도성) + 당일 반등
  breakout   : 순수 60일 신고가 돌파 (음 IC 확인용 대조군)
"""
import sys
import numpy as np
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta

UNIVERSE_SIZE = int(sys.argv[1]) if len(sys.argv) > 1 else 40
TOP_SKIP = int(sys.argv[2]) if len(sys.argv) > 2 else 120  # 시총 상위 제외(대형주 배제→중소형)
PERIOD_DAYS = 400
COST_RT = 0.41
ATR_MULT, STOP_FLOOR, STOP_CEIL = 1.5, 2.5, 6.0
TRAIL_MULT, PARTIAL_R, PARTIAL_FRAC = 2.0, 1.0, 1.0 / 3
TIME_STOP_DAYS, TIME_STOP_THRESH = 10, -1.0
FWD_K = 10

END = datetime.today(); START = END - timedelta(days=PERIOD_DAYS)
SS, EE = START.strftime("%Y-%m-%d"), END.strftime("%Y-%m-%d")

CURATED = [
    "005930","000660","373220","207940","005380","000270","068270","005490","051910","006400",
    "035420","035720","051900","028260","105560","055550","086790","015760","034730","012330",
    "009150","011200","032830","003550","066570","096770","017670","030200","018260","010130",
    "011170","009830","024110","316140","071050","139480","004020","010950","267250","047050",
]


def get_universe(n, top_skip=TOP_SKIP):
    """KOSPI+KOSDAQ 시총 상위 top_skip개 제외 후 다음 n종목(중소형 밴드)."""
    try:
        frames = []
        for mkt in ("KOSPI", "KOSDAQ"):
            try:
                frames.append(fdr.StockListing(mkt))
            except Exception:
                pass
        lst = pd.concat(frames, ignore_index=True) if frames else fdr.StockListing("KOSPI")
        cols = lst.columns
        capcol = next((c for c in ["Marcap", "MarketCap", "시가총액"] if c in cols), None)
        codecol = "Code" if "Code" in cols else ("Symbol" if "Symbol" in cols else None)
        namecol = "Name" if "Name" in cols else None
        if capcol and codecol:
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
            if out:
                print(f"  유니버스: 중소형 {len(out)}종목 (시총 {top_skip}위 이후)")
                return out
    except Exception as e:
        print(f"  (StockListing 폴백: {e})")
    return CURATED[:n]


def _sp(atrp):
    return 3.0 if atrp <= 0 else max(STOP_FLOOR, min(STOP_CEIL, atrp * ATR_MULT))


def _sim_new(df, i, entry, sp):
    stop = entry * (1 - sp / 100); trail_w = sp * TRAIL_MULT
    peak = entry; pr = 0.0; pd_ = False
    for j in range(i + 1, len(df)):
        c = float(df["Close"].iloc[j]); gain = (c / entry - 1) * 100; held = j - i
        if not pd_ and gain >= sp * PARTIAL_R:
            pr = (c / entry - 1) * 100 * PARTIAL_FRAC; pd_ = True; peak = c
            stop = max(stop, peak * (1 - trail_w / 100))
        elif pd_ and c > peak:
            peak = c; stop = max(stop, peak * (1 - trail_w / 100))
        if c <= stop or (held >= TIME_STOP_DAYS and gain <= TIME_STOP_THRESH):
            rem = (1 - PARTIAL_FRAC) if pd_ else 1.0
            return pr + (c / entry - 1) * 100 * rem - COST_RT
    rem = (1 - PARTIAL_FRAC) if pd_ else 1.0
    return pr + (float(df["Close"].iloc[-1]) / entry - 1) * 100 * rem - COST_RT


def _rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


VARIANTS = ["base", "pullback", "pull_rsi", "breakout"]


def _entry_signals(df, i):
    """각 변형의 진입 조건 충족 여부 dict."""
    c = float(df["Close"].iloc[i]); prev = float(df["Close"].iloc[i - 1])
    o = float(df["Open"].iloc[i])
    ma20 = df["MA20"].iloc[i]; ma60 = df["MA60"].iloc[i]
    rv = df["RVOL"].iloc[i]; chg = df["Chg"].iloc[i]; rsi = df["RSI"].iloc[i]
    if pd.isna(ma20) or pd.isna(ma60) or pd.isna(rv) or ma20 <= 0 or ma60 <= 0:
        return {}
    gap_ok = not (prev > 0 and (c / prev - 1) * 100 >= 3.0)
    chg_ok = chg < 5.0
    uptrend = c > ma60 and ma20 > ma60
    sig = {}
    # base: 현행 강세추종
    sig["base"] = (rv >= 1.5 and (c / ma20 - 1) * 100 > -2.0 and c > ma20 and chg_ok and gap_ok)
    # pullback: 상승추세 + MA20 부근(±1.5%) 눌림 + 당일 반등(양봉)
    near_ma20 = abs(c / ma20 - 1) * 100 <= 1.5
    sig["pullback"] = (uptrend and near_ma20 and c > o and chg_ok and gap_ok)
    # pull_rsi: 상승추세 + RSI 35~50 + 양봉
    sig["pull_rsi"] = (uptrend and pd.notna(rsi) and 35 <= rsi <= 50 and c > o and gap_ok)
    # breakout: 순수 60일 신고가 돌파 (대조군)
    high60 = float(df["High"].iloc[i - 60:i].max())
    sig["breakout"] = (c > high60 and chg_ok and gap_ok)
    return sig


def main():
    print(f"■ 진입 변형 비교  {SS}~{EE}  (목표 {UNIVERSE_SIZE}종목, 청산=신규/비용반영)\n")
    uni = get_universe(UNIVERSE_SIZE)
    try:
        ks = fdr.DataReader("KS11", SS, EE)["Close"]
    except Exception:
        ks = None

    rec = {v: [] for v in VARIANTS}   # v -> list of (f10, alpha, net, ext)
    done = 0
    for code in uni:
        try:
            df = fdr.DataReader(code, SS, EE)
        except Exception:
            continue
        if df is None or len(df) < 90:
            continue
        df = df.copy()
        df["MA20"] = df["Close"].rolling(20).mean()
        df["MA60"] = df["Close"].rolling(60).mean()
        df["RVOL"] = df["Volume"] / df["Volume"].rolling(20).mean()
        df["Chg"] = df["Close"].pct_change() * 100
        df["RSI"] = _rsi(df["Close"])
        h, l, c = df["High"], df["Low"], df["Close"]; pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        df["ATRP"] = tr.rolling(14).mean() / c * 100
        n = len(df)
        cool = {v: -1 for v in VARIANTS}
        for i in range(60, n - FWD_K - 1):
            sig = _entry_signals(df, i)
            if not sig:
                continue
            close = float(c.iloc[i])
            for v in VARIANTS:
                if not sig.get(v) or i <= cool[v]:
                    continue
                f10 = (float(c.iloc[i + FWD_K]) / close - 1) * 100
                alpha = f10
                if ks is not None and i + FWD_K < len(ks):
                    alpha = f10 - (ks.iloc[i + FWD_K] / ks.iloc[i] - 1) * 100
                atrp = float(df["ATRP"].iloc[i]) if pd.notna(df["ATRP"].iloc[i]) else 0.0
                net = _sim_new(df, i, close, _sp(atrp))
                ext = (close / df["MA20"].iloc[i] - 1) * 100   # MA20 이격(팩터)
                rec[v].append((f10, alpha, net, ext))
                cool[v] = i + 3
        done += 1
        if done % 10 == 0:
            print(f"  ...{done}종목 처리")

    print(f"\n  (처리 {done}종목)\n")
    hdr = f"{'변형':<10}{'건수':>5}{'f10평균':>9}{'alpha':>8}{'승률(net)':>9}{'기댓값':>8}{'손익비':>7}{'이격IC':>8}"
    print(hdr); print("-" * len(hdr))
    for v in VARIANTS:
        r = rec[v]
        if not r:
            print(f"{v:<10}{0:>5}  (신호 없음)"); continue
        d = pd.DataFrame(r, columns=["f10", "alpha", "net", "ext"])
        net = d["net"]; wins = net[net > 0]; losses = net[net <= 0]
        payoff = (wins.mean() / abs(losses.mean())) if len(losses) and losses.mean() != 0 else float("inf")
        ic = d["ext"].corr(d["f10"], method="spearman")
        print(f"{v:<10}{len(d):>5}{d['f10'].mean():>+8.2f}%{d['alpha'].mean():>+7.2f}%"
              f"{(net>0).mean()*100:>8.1f}%{net.mean():>+7.2f}%{payoff:>6.2f}{ic:>+8.3f}")
    print("\n해석: alpha(+)= KOSPI보다 잘 고름. 이격IC(+)= 이격 클수록 잘 오름(추종 유리),"
          " (−)= 눌림이 유리.")


if __name__ == "__main__":
    main()
