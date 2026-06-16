"""실제 VCP 전략 시그널의 forward 알파 측정 (#4 ①).

라이브와 동일한 marketflow detect_vcp()를 그대로 호출해, VCP 패턴 탐지일의
forward 10/20일 수익률·KOSPI 대비 alpha·신규청산 기대값을 등급(A/B/C/D)별로 측정.
'우리 플래그십 전략에 실제 선별 엣지가 있는가'에 답한다.
"""
import sys, os
import numpy as np
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta

# marketflow 엔진 모듈 경로 (vcp_detector가 config/indicators를 상대 import)
ENG = os.path.join(os.path.dirname(__file__), "..", "marketflow", "engine")
sys.path.insert(0, os.path.abspath(ENG))
from vcp_detector import detect_vcp          # noqa: E402
from config import VCPConfig                  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
TOP_SKIP = int(sys.argv[2]) if len(sys.argv) > 2 else 120
PERIOD_DAYS, FWD = 400, 10
COST_RT = 0.41
ATR_MULT, STOP_FLOOR, STOP_CEIL = 1.5, 2.5, 6.0
TRAIL_MULT, PARTIAL_R, PARTIAL_FRAC = 2.0, 1.0, 1.0 / 3
TIME_STOP_DAYS, TIME_STOP_THRESH = 10, -1.0
END = datetime.today(); START = END - timedelta(days=PERIOD_DAYS)
SS, EE = START.strftime("%Y-%m-%d"), END.strftime("%Y-%m-%d")
VCFG = VCPConfig()


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


def main():
    print(f"■ 실제 VCP 전략 alpha 측정 {SS}~{EE} (중소형 {N}종목, detect_vcp 그대로)\n")
    uni = get_universe(N)
    print(f"  유니버스 {len(uni)}종목")
    try:
        ks = fdr.DataReader("KS11", SS, EE)["Close"]
    except Exception:
        ks = None
    rec = []  # grade, f10, alpha, net, surge
    done = 0
    for code in uni:
        try:
            raw = fdr.DataReader(code, SS, EE)
        except Exception:
            continue
        if raw is None or len(raw) < VCFG.lookback + FWD + 5:
            continue
        df = raw.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                 "Close": "close", "Volume": "volume"})[
            ["open", "high", "low", "close", "volume"]].reset_index(drop=True)
        carr = df["close"].to_numpy()
        idx = raw.index
        h, l, c = df["high"], df["low"], df["close"]; pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        atrp_s = (tr.rolling(14).mean() / c * 100).to_numpy()
        cool = -1
        for i in range(VCFG.lookback, len(df) - FWD - 1):
            if i <= cool:
                continue
            res = detect_vcp(df.iloc[: i + 1])
            if not res.detected:
                continue
            entry = carr[i]
            f10 = (carr[i + FWD] / entry - 1) * 100
            alpha = f10
            if ks is not None:
                kp = ks.index.get_indexer([idx[i]], method="nearest")[0]
                if 0 <= kp and kp + FWD < len(ks):
                    alpha -= (ks.iloc[kp + FWD] / ks.iloc[kp] - 1) * 100
            atrp = atrp_s[i] if not np.isnan(atrp_s[i]) else 0.0
            net = _sim_new(carr, i, entry, _sp(atrp))
            rec.append((res.grade, f10, alpha, net, res.is_surge_candidate))
            cool = i + 5
        done += 1
        if done % 8 == 0:
            print(f"  ...{done}종목, VCP 탐지 누적 {len(rec)}건")

    if not rec:
        print("\nVCP 탐지 이벤트 없음."); return
    d = pd.DataFrame(rec, columns=["grade", "f10", "alpha", "net", "surge"])

    def _rep(tag, sub):
        if len(sub) == 0:
            print(f"  {tag:<22} 0건"); return
        net = sub["net"]; w = net[net > 0]; lo = net[net <= 0]
        payoff = (w.mean() / abs(lo.mean())) if len(lo) and lo.mean() != 0 else float("inf")
        print(f"  {tag:<22}{len(sub):>5}건  alpha {sub['alpha'].mean():+6.2f}%p  "
              f"승률 {(net>0).mean()*100:5.1f}%  기댓값 {net.mean():+6.2f}%  손익비 {payoff:5.2f}")

    print(f"\n■ VCP 탐지 {len(d)}건 — 등급별 forward 알파 / 신규청산 기대값\n")
    _rep("전체", d)
    for g in ["A", "B", "C", "D"]:
        _rep(f"등급 {g}", d[d["grade"] == g])
    _rep("A+B (고품질)", d[d["grade"].isin(["A", "B"])])
    _rep("S (급성장후보)", d[d["surge"]])
    print("\n해석: alpha(+)= VCP가 KOSPI 대비 선별 엣지 있음. A/B/S가 +면 라이브 채택 근거.")


if __name__ == "__main__":
    main()
