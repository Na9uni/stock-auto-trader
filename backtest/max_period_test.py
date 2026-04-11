"""최장 기간 백테스트 — 전 종목 4.7년 + 삼성전자 26년."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import yfinance as yf


def download(ticker, start, end):
    raw = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
    if raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.reset_index()
    dc = [c for c in df.columns if "date" in str(c).lower()]
    if dc:
        df = df.rename(columns={dc[0]: "datetime"})
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y%m%d")
    return df[["datetime", "open", "high", "low", "close", "volume"]]


def run_auto(df, k=0.5, capital=1_000_000):
    df = df.copy().reset_index(drop=True)
    if len(df) >= 7:
        ranges = df["high"].iloc[:-1] - df["low"].iloc[:-1]
        df["atr5"] = ranges.rolling(5).mean()
    else:
        df["atr5"] = df["high"] - df["low"]
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    cash = capital
    position = None
    trades = []
    vb_trades = 0
    trend_trades = 0
    bull_days = 0
    bear_days = 0
    peak = capital
    mdd = 0

    for i in range(61, len(df)):
        prev = df.iloc[i - 1]
        prev2 = df.iloc[i - 2]
        today = df.iloc[i]
        to = float(today["open"])
        th = float(today["high"])
        tl = float(today["low"])
        tc = float(today["close"])
        ma20 = float(prev["ma20"]) if not pd.isna(prev["ma20"]) else 0
        ma60 = float(prev["ma60"]) if not pd.isna(prev["ma60"]) else 0
        ma5 = float(prev["ma5"]) if not pd.isna(prev["ma5"]) else 0
        ma5_2 = float(prev2["ma5"]) if not pd.isna(prev2["ma5"]) else 0
        ma20_2 = float(prev2["ma20"]) if not pd.isna(prev2["ma20"]) else 0
        atr = float(prev["atr5"]) if not pd.isna(prev.get("atr5", float("nan"))) else float(prev["high"] - prev["low"])

        regime = "bull" if (ma20 > 0 and ma60 > 0 and ma20 > ma60) else "bear"
        if regime == "bull":
            bull_days += 1
        else:
            bear_days += 1

        if regime == "bull":
            if position and position.get("mode") == "vb":
                bp = position["buy_price"]
                hp = max(position["high_price"], th)
                position["high_price"] = hp
                if (tl - bp) / bp <= -0.02:
                    cash += bp * 0.98 * position["qty"]
                    trades.append({"pnl": (bp * 0.98 - bp) * position["qty"]})
                    position = None
                    continue
                if (hp - bp) / bp >= 0.025:
                    position["trailing"] = True
                if position.get("trailing") and (tl - hp) / hp <= -0.01:
                    sp = hp * 0.99
                    cash += sp * position["qty"]
                    trades.append({"pnl": (sp - bp) * position["qty"]})
                    position = None
                    continue
                cash += tc * position["qty"]
                trades.append({"pnl": (tc - bp) * position["qty"]})
                position = None
                continue

            if position is None:
                target = to + atr * k
                if atr >= to * 0.005 and th >= target:
                    qty = int(cash // target)
                    if qty > 0:
                        cash -= target * qty
                        position = {"buy_price": target, "qty": qty, "high_price": th,
                                    "trailing": False, "mode": "vb"}
                        vb_trades += 1
        else:
            if position and position.get("mode") == "trend":
                bp = position["buy_price"]
                if (tc - bp) / bp <= -0.05:
                    cash += tc * position["qty"]
                    trades.append({"pnl": (tc - bp) * position["qty"]})
                    position = None
                    continue
                if ma5 > 0 and ma20 > 0 and ma5_2 >= ma20_2 and ma5 < ma20:
                    cash += tc * position["qty"]
                    trades.append({"pnl": (tc - bp) * position["qty"]})
                    position = None
                    continue

            if position is None and ma5 > 0 and ma20 > 0 and ma5_2 <= ma20_2 and ma5 > ma20:
                if ma60 > 0 and tc > ma60:
                    qty = int(cash // tc)
                    if qty > 0:
                        cash -= tc * qty
                        position = {"buy_price": tc, "qty": qty, "mode": "trend"}
                        trend_trades += 1

        eq = cash + (position["qty"] * tc if position else 0)
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > mdd:
            mdd = dd

    if position:
        cash += float(df.iloc[-1]["close"]) * position["qty"]
        trades.append({"pnl": 0})

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    total_win = sum(t["pnl"] for t in wins)
    total_loss = abs(sum(t["pnl"] for t in losses))
    pf = total_win / total_loss if total_loss > 0 else 999

    first_p = float(df.iloc[61]["close"])
    last_p = float(df.iloc[-1]["close"])
    bnh = (last_p - first_p) / first_p * 100

    return {
        "pnl": cash - capital,
        "return_pct": (cash - capital) / capital * 100,
        "bnh": bnh,
        "trades": len(trades),
        "vb": vb_trades,
        "trend": trend_trades,
        "wins": len(wins),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "pf": pf,
        "mdd": mdd,
        "bull_pct": bull_days / (bull_days + bear_days) * 100 if (bull_days + bear_days) > 0 else 0,
    }


def main():
    k_map = {
        "229200.KS": 0.6, "131890.KS": 0.3, "108450.KS": 0.3,
        "395160.KS": 0.5, "005930.KS": 0.6, "261220.KS": 0.5, "132030.KS": 0.7,
    }

    tickers = {
        "229200.KS": "KODEX코스닥150",
        "131890.KS": "ACE삼성동일가중",
        "108450.KS": "ACE삼성섹터가중",
        "395160.KS": "KODEX AI반도체",
        "005930.KS": "삼성전자",
        "261220.KS": "KODEX WTI원유",
        "132030.KS": "KODEX골드선물",
    }

    # ============================
    # 테스트 1: 전 종목 공통 (2021.08~현재)
    # ============================
    print("=" * 80)
    print("  전 종목 공통 (2021.08~2026.04) -- 4.7년, 시드 100만원")
    print("=" * 80)
    print(
        f" {'종목':>18s} | {'수익률':>7s} {'BnH':>7s}"
        f" | {'거래':>3s} {'VB':>3s} {'추세':>3s}"
        f" | {'승률':>4s} {'PF':>4s} {'MDD':>5s}"
        f" | {'상승장':>5s}"
    )
    print(f" {'-' * 18} | {'-' * 15} | {'-' * 13} | {'-' * 15} | {'-' * 5}")

    total_pnl = 0
    for yf_t, name in tickers.items():
        df = download(yf_t, "2021-08-01", "2026-04-11")
        if len(df) < 70:
            continue
        k = k_map.get(yf_t, 0.5)
        r = run_auto(df, k=k)
        total_pnl += r["pnl"]
        print(
            f" {name:>18s} |"
            f" {r['return_pct']:>+6.1f}% {r['bnh']:>+6.1f}%"
            f" | {r['trades']:>3} {r['vb']:>3} {r['trend']:>3}"
            f" | {r['win_rate']:>3.0f}% {r['pf']:>4.1f} {r['mdd']:>4.1f}%"
            f" | {r['bull_pct']:>4.0f}%"
        )
    print(f"\n  합산 손익: {total_pnl:>+12,.0f}원 (시드 100만원 x 7종목)")

    # ============================
    # 테스트 2: 종목별 최장 기간
    # ============================
    long_tickers = {
        "005930.KS": ("삼성전자", "2000-01-01", 0.6),
        "108450.KS": ("ACE삼성섹터가중", "2009-05-01", 0.3),
        "131890.KS": ("ACE삼성동일가중", "2010-10-01", 0.3),
        "132030.KS": ("KODEX골드선물", "2010-10-01", 0.7),
        "229200.KS": ("KODEX코스닥150", "2015-10-01", 0.6),
        "261220.KS": ("KODEX WTI원유", "2017-01-01", 0.5),
        "395160.KS": ("KODEX AI반도체", "2021-08-01", 0.5),
    }

    print(f"\n{'=' * 80}")
    print("  종목별 최장 기간 백테스트 -- 시드 100만원")
    print("=" * 80)

    for yf_t, (name, start, k) in long_tickers.items():
        df = download(yf_t, start, "2026-04-11")
        if len(df) < 70:
            continue
        years = len(df) / 252
        r = run_auto(df, k=k)
        ann_ret = r["return_pct"] / years if years > 0 else 0
        print(
            f"  {name:>18s} ({start[:4]}~현재, {years:.1f}년)"
            f" | 전략 {r['return_pct']:>+8.1f}% (연 {ann_ret:>+5.1f}%)"
            f" | BnH {r['bnh']:>+8.1f}%"
            f" | {r['trades']:>4}거래"
            f" | 승률 {r['win_rate']:>3.0f}%"
            f" | PF {r['pf']:>4.1f}"
            f" | MDD {r['mdd']:>5.1f}%"
        )

    # ============================
    # 테스트 3: 삼성전자 5년 단위 분석
    # ============================
    print(f"\n{'=' * 80}")
    print("  삼성전자 5년 단위 분석 (2000~2026) -- 시드 100만원")
    print("=" * 80)

    for label, start, end in [
        ("2000~2005", "2000-01-01", "2005-12-31"),
        ("2006~2010", "2006-01-01", "2010-12-31"),
        ("2011~2015", "2011-01-01", "2015-12-31"),
        ("2016~2020", "2016-01-01", "2020-12-31"),
        ("2021~현재", "2021-01-01", "2026-04-11"),
        ("전체 26년", "2000-01-01", "2026-04-11"),
    ]:
        df = download("005930.KS", start, end)
        if len(df) < 70:
            print(f"  {label}: 데이터 부족")
            continue
        r = run_auto(df, k=0.6)
        years = len(df) / 252
        ann_ret = r["return_pct"] / years if years > 0 else 0
        print(
            f"  {label:>10s}"
            f" | 전략 {r['return_pct']:>+9.1f}% (연 {ann_ret:>+6.1f}%)"
            f" | BnH {r['bnh']:>+9.1f}%"
            f" | {r['trades']:>4}거래"
            f" | 승률 {r['win_rate']:>3.0f}%"
            f" | PF {r['pf']:>4.1f}"
            f" | MDD {r['mdd']:>5.1f}%"
        )


if __name__ == "__main__":
    main()
