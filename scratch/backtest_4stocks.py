"""4개 종목 1년 백테스트 — 시스템 룰 적용."""
import FinanceDataReader as fdr
import pandas as pd
import numpy as np
from collections import defaultdict
from datetime import datetime, timedelta

TARGET_STOCKS = [
    ("000100", "유한양행"),
    ("090350", "노루페인트"),
    ("196700", "웹스"),
    ("459550", "알트"),
]

end   = datetime.today()
start = end - timedelta(days=365)
START = start.strftime("%Y-%m-%d")
END   = end.strftime("%Y-%m-%d")

# ── 시스템 룰 파라미터 ───────────────────────────────────────
STOP_LOSS_PCT     = 3.0   # Phase1 고정 손절
BREAKEVEN_TRIGGER = 2.0   # Phase2 본전 전환
PROFIT_TRIGGER    = 5.0   # Phase3 trailing 활성화
PROFIT_TRAIL_PCT  = 2.5   # trailing stop 폭
RVOL_MIN          = 1.5   # 최소 거래량 비율
MA20_FLOOR        = -2.0  # MA20 대비 최소 위치
CHANGE_PCT_MAX    = 5.0   # 당일 등락 상한 (추격 차단)
GAP_UP_BLOCK      = 3.0   # 갭업 진입 차단
TIME_STOP_DAYS    = 10    # 시간 손절 기준 일수
TIME_STOP_THRESH  = -1.0  # 시간 손절 수익률 임계


def run_backtest(ticker, name):
    df = fdr.DataReader(ticker, START, END)
    if len(df) < 30:
        return None

    df = df.copy()
    df["MA20"]      = df["Close"].rolling(20).mean()
    df["Vol20"]     = df["Volume"].rolling(20).mean()
    df["RVOL"]      = df["Volume"] / df["Vol20"].replace(0, np.nan)
    df["Change"]    = df["Close"].pct_change() * 100
    df["PrevClose"] = df["Close"].shift(1)

    trades = []
    in_position = False
    entry_price = stop_price = peak_price = 0.0
    phase = ""
    entry_date = None

    for i in range(21, len(df)):
        row   = df.iloc[i]
        close = float(row["Close"])
        ma20  = float(row["MA20"])   if not pd.isna(row["MA20"])   else 0
        rvol  = float(row["RVOL"])   if not pd.isna(row["RVOL"])   else 0
        chg   = float(row["Change"]) if not pd.isna(row["Change"]) else 0
        prev  = float(row["PrevClose"]) if not pd.isna(row["PrevClose"]) else 0

        if not in_position:
            if ma20 <= 0 or close <= 0:                             continue
            if rvol < RVOL_MIN:                                     continue
            if (close / ma20 - 1) * 100 <= MA20_FLOOR:             continue
            if chg >= CHANGE_PCT_MAX:                               continue
            if prev > 0 and (close / prev - 1) * 100 >= GAP_UP_BLOCK: continue
            if close <= ma20:                                       continue

            entry_price = close
            stop_price  = entry_price * (1 - STOP_LOSS_PCT / 100)
            peak_price  = close
            phase       = "stop"
            in_position = True
            entry_date  = df.index[i]

        else:
            gain = (close / entry_price - 1) * 100
            held = (df.index[i] - entry_date).days

            if phase in ("stop", "even") and gain >= PROFIT_TRIGGER:
                phase = "trail"
                peak_price = close
                stop_price = peak_price * (1 - PROFIT_TRAIL_PCT / 100)
            elif phase == "stop" and gain >= BREAKEVEN_TRIGGER:
                phase = "even"
                stop_price = entry_price
                peak_price = close
            elif phase == "trail" and close > peak_price:
                peak_price = close
                stop_price = peak_price * (1 - PROFIT_TRAIL_PCT / 100)

            def _close(reason):
                pnl = (close / entry_price - 1) * 100
                trades.append({
                    "entry_date": entry_date.date(),
                    "exit_date":  df.index[i].date(),
                    "entry": entry_price, "exit": close,
                    "pnl": pnl, "phase": reason, "days": held,
                })

            if close <= stop_price:
                _close(phase); in_position = False
            elif held >= TIME_STOP_DAYS and gain <= TIME_STOP_THRESH:
                _close("time_stop"); in_position = False

    if in_position:
        last = float(df.iloc[-1]["Close"])
        held = (df.index[-1] - entry_date).days
        trades.append({
            "entry_date": entry_date.date(), "exit_date": df.index[-1].date(),
            "entry": entry_price, "exit": last,
            "pnl": (last / entry_price - 1) * 100, "phase": "open", "days": held,
        })

    bh_return = (float(df.iloc[-1]["Close"]) / float(df.iloc[0]["Close"]) - 1) * 100
    return trades, bh_return


# ── 실행 ─────────────────────────────────────────────────────
print(f"■ 백테스트 기간: {START} ~ {END}  (1년)")
print(f"■ 진입: RVOL≥{RVOL_MIN} | MA20 위 | 등락<{CHANGE_PCT_MAX}% | 갭업<{GAP_UP_BLOCK}%")
print(f"■ 손절: Phase1 -{STOP_LOSS_PCT}% → +{BREAKEVEN_TRIGGER}% 본전 → +{PROFIT_TRIGGER}% trailing -{PROFIT_TRAIL_PCT}%")
print()

all_rows = []

for ticker, name in TARGET_STOCKS:
    try:
        result = run_backtest(ticker, name)
        if result is None:
            print(f"[{name}] 데이터 부족\n")
            continue
        trades, bh_return = result

        if not trades:
            print(f"[{name} ({ticker})] 진입 신호 없음\n")
            continue

        pnls   = [t["pnl"] for t in trades]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        n      = len(pnls)
        wr     = len(wins) / n * 100
        avg    = np.mean(pnls)
        a_win  = np.mean(wins)  if wins   else 0.0
        a_los  = np.mean(losses) if losses else 0.0
        exp    = wr / 100 * a_win + (1 - wr / 100) * a_los

        cum      = np.cumsum(pnls)
        peak_cum = np.maximum.accumulate(cum)
        mdd      = float(np.min(cum - peak_cum))

        monthly = defaultdict(list)
        for t in trades:
            monthly[str(t["entry_date"])[:7]].append(t["pnl"])

        phase_cnt = defaultdict(int)
        for t in trades:
            phase_cnt[t["phase"]] += 1

        print("=" * 65)
        print(f"  {name} ({ticker})")
        print("=" * 65)
        print(f"  거래 횟수     : {n}건  (승 {len(wins)} / 패 {len(losses)})")
        print(f"  승    률      : {wr:.1f}%")
        print(f"  평균 손익     : {avg:+.2f}%")
        print(f"  평균 수익 (승): {a_win:+.2f}%")
        print(f"  평균 손실 (패): {a_los:+.2f}%")
        print(f"  기댓값/거래   : {exp:+.2f}%")
        print(f"  최대 수익     : {max(pnls):+.2f}%")
        print(f"  최대 손실     : {min(pnls):+.2f}%")
        print(f"  누적 MDD      : {mdd:+.2f}%")
        print(f"  Buy & Hold    : {bh_return:+.2f}%")
        print(f"  청산 유형     : {dict(sorted(phase_cnt.items()))}")
        print()

        print(f"  ▼ 전체 거래 내역")
        print(f"  {'진입일':<12} {'청산일':<12} {'진입가':>8} {'청산가':>8} {'손익':>7}  유형")
        print(f"  {'-'*12} {'-'*12} {'-'*8} {'-'*8} {'-'*7}  ----")
        for t in trades:
            flag = "★" if t["pnl"] > 0 else "✗"
            print(f"  {str(t['entry_date']):<12} {str(t['exit_date']):<12} "
                  f"{t['entry']:>8,.0f} {t['exit']:>8,.0f} "
                  f"{t['pnl']:>+6.1f}%  {flag} {t['phase']}({t['days']}일)")
        print()

        print(f"  ▼ 월별 손익")
        for ym in sorted(monthly):
            m_pnls = monthly[ym]
            bar = "█" * len([p for p in m_pnls if p > 0]) + "░" * len([p for p in m_pnls if p <= 0])
            print(f"  {ym}  {bar:<10}  합계 {sum(m_pnls):>+6.1f}%  ({len(m_pnls)}건)")
        print()

        all_rows.append({
            "name": name, "ticker": ticker, "n": n, "wr": wr,
            "avg": avg, "exp": exp, "mdd": mdd, "bh": bh_return,
        })

    except Exception as e:
        print(f"[{name}] 오류: {e}\n")

# ── 종합 비교 ─────────────────────────────────────────────────
print("=" * 65)
print("  종합 비교 (시스템 누적 vs Buy & Hold)")
print("=" * 65)
print(f"  {'종목':<10} {'거래':>4} {'승률':>6} {'기댓값':>8} {'MDD':>7}  {'시스템누적':>10}  {'B&H':>8}  판정")
print(f"  {'-'*10} {'-'*4} {'-'*6} {'-'*8} {'-'*7}  {'-'*10}  {'-'*8}  ----")
for r in all_rows:
    sys_cum = r["n"] * r["avg"]
    verdict = "✅ 시스템 우위" if sys_cum > r["bh"] else "⚠️  B&H 우위"
    print(f"  {r['name']:<10} {r['n']:>4}건 {r['wr']:>5.1f}% {r['exp']:>+7.2f}% "
          f"{r['mdd']:>+6.1f}%  {sys_cum:>+9.1f}%  {r['bh']:>+7.1f}%  {verdict}")
