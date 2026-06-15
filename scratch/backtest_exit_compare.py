"""청산 규칙 비교 백테스트 — 기존(본전+2.5%트레일) vs 신규(ATR손절+1R부분익절+광폭트레일).

동일 진입 시그널에 두 청산 엔진을 적용해 거래비용(수수료+세금+슬리피지) 반영 후
기대값/손익비/MDD를 비교한다. (#4 검증 + #6 비용)
"""
import FinanceDataReader as fdr
import pandas as pd
import numpy as np

TARGET_STOCKS = [
    ("000100", "유한양행"),
    ("090350", "노루페인트"),
    ("196700", "웹스"),
    ("459550", "알트"),
]
START, END = "2025-06-16", "2026-06-15"

# ── 진입 규칙 (동일) ──────────────────────────────────────────
RVOL_MIN, MA20_FLOOR, CHANGE_MAX, GAP_BLOCK = 1.5, -2.0, 5.0, 3.0
# ── 거래비용 (#6): 매수 0.115% + 매도 0.295%(수수료+세금0.18%+슬리피지) ──
COST_RT = 0.115 + 0.295   # 왕복 비용 % (라운드트립)
# ── 청산 파라미터 ─────────────────────────────────────────────
TIME_STOP_DAYS, TIME_STOP_THRESH = 10, -1.0
# 기존
OLD_STOP, OLD_BE, OLD_TRIG, OLD_TRAIL = 3.0, 2.0, 5.0, 2.5
# 신규
ATR_MULT, STOP_FLOOR, STOP_CEIL = 1.5, 2.5, 6.0
TRAIL_MULT, PARTIAL_R, PARTIAL_FRAC = 2.0, 1.0, 1.0 / 3


def _atrp(df, i):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.iloc[max(0, i - 13):i + 1].mean()
    return float(atr / c.iloc[i] * 100) if c.iloc[i] > 0 else 0.0


def _stop_pct(df, i):
    a = _atrp(df, i)
    return 3.0 if a <= 0 else max(STOP_FLOOR, min(STOP_CEIL, a * ATR_MULT))


def _sim_old(df, i, entry):
    stop = entry * (1 - OLD_STOP / 100)
    peak, phase = entry, "stop"
    for j in range(i + 1, len(df)):
        c = float(df["Close"].iloc[j]); gain = (c / entry - 1) * 100
        held = (df.index[j] - df.index[i]).days
        if phase in ("stop", "even") and gain >= OLD_TRIG:
            phase, peak = "trail", c; stop = max(stop, peak * (1 - OLD_TRAIL / 100))
        elif phase == "stop" and gain >= OLD_BE:
            phase, stop, peak = "even", entry, c
        elif phase == "trail" and c > peak:
            peak = c; stop = max(stop, peak * (1 - OLD_TRAIL / 100))
        if c <= stop:
            return (c / entry - 1) * 100 - COST_RT
        if held >= TIME_STOP_DAYS and gain <= TIME_STOP_THRESH:
            return (c / entry - 1) * 100 - COST_RT
    return (float(df["Close"].iloc[-1]) / entry - 1) * 100 - COST_RT


def _sim_new(df, i, entry):
    sp = _stop_pct(df, i)
    stop = entry * (1 - sp / 100)
    trail_w = sp * TRAIL_MULT
    peak, phase = entry, "stop"
    partial_ret, partial_done = 0.0, False
    for j in range(i + 1, len(df)):
        c = float(df["Close"].iloc[j]); gain = (c / entry - 1) * 100
        held = (df.index[j] - df.index[i]).days
        if not partial_done and gain >= sp * PARTIAL_R:
            partial_ret = (c / entry - 1) * 100 * PARTIAL_FRAC  # 1/3 확정
            partial_done = True; phase, peak = "trail", c
            stop = max(stop, peak * (1 - trail_w / 100))
        elif partial_done and c > peak:
            peak = c; stop = max(stop, peak * (1 - trail_w / 100))
        exit_now = c <= stop or (held >= TIME_STOP_DAYS and gain <= TIME_STOP_THRESH)
        if exit_now:
            rem = (1 - PARTIAL_FRAC) if partial_done else 1.0
            return partial_ret + (c / entry - 1) * 100 * rem - COST_RT
    rem = (1 - PARTIAL_FRAC) if partial_done else 1.0
    last = float(df["Close"].iloc[-1])
    return partial_ret + (last / entry - 1) * 100 * rem - COST_RT


def _stats(pnls):
    n = len(pnls)
    if n == 0:
        return None
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    wr = len(wins) / n * 100
    aw = np.mean(wins) if wins else 0.0
    al = np.mean(losses) if losses else 0.0
    exp = np.mean(pnls)
    payoff = (aw / abs(al)) if al != 0 else float("inf")
    cum = np.cumsum(pnls); mdd = float(np.min(cum - np.maximum.accumulate(cum)))
    return dict(n=n, wr=wr, aw=aw, al=al, exp=exp, payoff=payoff, mdd=mdd, total=sum(pnls))


def _run(ticker):
    df = fdr.DataReader(ticker, START, END)
    if len(df) < 30:
        return None
    df = df.copy()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["RVOL"] = df["Volume"] / df["Volume"].rolling(20).mean()
    df["Chg"] = df["Close"].pct_change() * 100
    old, new = [], []
    in_pos = False; exit_idx = -1
    for i in range(21, len(df)):
        if in_pos:
            if i <= exit_idx:
                continue
            in_pos = False
        c = float(df["Close"].iloc[i]); ma = df["MA20"].iloc[i]
        rv = df["RVOL"].iloc[i]; chg = df["Chg"].iloc[i]
        prev = float(df["Close"].iloc[i - 1])
        if pd.isna(ma) or pd.isna(rv) or ma <= 0:                 continue
        if rv < RVOL_MIN:                                        continue
        if (c / ma - 1) * 100 <= MA20_FLOOR:                     continue
        if chg >= CHANGE_MAX:                                    continue
        if prev > 0 and (c / prev - 1) * 100 >= GAP_BLOCK:       continue
        if c <= ma:                                              continue
        old.append(_sim_old(df, i, c)); new.append(_sim_new(df, i, c))
        in_pos = True
        # 다음 진입은 충분히 뒤로 (중복 방지: 대략 보유기간만큼 점프)
        exit_idx = i + 1
    return _stats(old), _stats(new)


print(f"■ 청산 비교 백테스트 {START}~{END} (거래비용 왕복 {COST_RT:.2f}% 반영)\n")
hdr = f"{'종목':<10}{'엔진':<6}{'거래':>4}{'승률':>7}{'평균수익':>9}{'평균손실':>9}{'기댓값':>8}{'손익비':>7}{'MDD':>8}{'누적':>9}"
print(hdr); print("-" * len(hdr))
agg = {"old": [], "new": []}
for tk, nm in TARGET_STOCKS:
    try:
        res = _run(tk)
        if not res or not res[0]:
            print(f"{nm:<10} 데이터/신호 없음"); continue
        o, n = res
        for tag, s in (("기존", o), ("신규", n)):
            print(f"{nm:<10}{tag:<6}{s['n']:>4}{s['wr']:>6.1f}%{s['aw']:>+8.2f}%{s['al']:>+8.2f}%"
                  f"{s['exp']:>+7.2f}%{s['payoff']:>6.2f}{s['mdd']:>+7.1f}%{s['total']:>+8.1f}%")
        agg["old"].append(o); agg["new"].append(n)
        print()
    except Exception as e:
        print(f"{nm:<10} 오류: {e}")

# 종합
print("=" * len(hdr))
for tag, key in (("기존", "old"), ("신규", "new")):
    rows = agg[key]
    if not rows:
        continue
    allpnl = []
    tot_n = sum(r["n"] for r in rows)
    wexp = sum(r["exp"] * r["n"] for r in rows) / tot_n if tot_n else 0
    avg_payoff = np.mean([r["payoff"] for r in rows if r["payoff"] != float("inf")])
    avg_wr = sum(r["wr"] * r["n"] for r in rows) / tot_n if tot_n else 0
    tot = sum(r["total"] for r in rows)
    print(f"{'종합':<10}{tag:<6}{tot_n:>4}{avg_wr:>6.1f}%{'':>9}{'':>9}{wexp:>+7.2f}%{avg_payoff:>6.2f}{'':>8}{tot:>+8.1f}%")
