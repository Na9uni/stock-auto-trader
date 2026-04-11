"""2021~현재 — 데이트레이딩 vs 스윙 vs 추세추종 연도별 비교."""
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


def run_daytrading(df, k=0.5, capital=1_000_000):
    df = df.copy().reset_index(drop=True)
    if len(df) >= 7:
        ranges = df["high"].iloc[:-1] - df["low"].iloc[:-1]
        df["atr5"] = ranges.rolling(5).mean()
    else:
        df["atr5"] = df["high"] - df["low"]
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    cash = capital
    position = None
    trades = []
    for i in range(61, len(df)):
        prev = df.iloc[i - 1]
        today = df.iloc[i]
        to = float(today["open"])
        th = float(today["high"])
        tl = float(today["low"])
        tc = float(today["close"])
        atr = float(prev["atr5"]) if not pd.isna(prev.get("atr5", float("nan"))) else float(prev["high"] - prev["low"])
        target = to + atr * k
        if position:
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
            # EOD 강제 청산
            cash += tc * position["qty"]
            trades.append({"pnl": (tc - bp) * position["qty"]})
            position = None
            continue
        if position is None:
            ma20 = float(prev["ma20"]) if not pd.isna(prev["ma20"]) else 0
            ma60 = float(prev["ma60"]) if not pd.isna(prev["ma60"]) else 0
            if ma20 > 0 and ma60 > 0 and ma20 < ma60:
                continue
            if atr < to * 0.005:
                continue
            if th >= target:
                qty = int(cash // target)
                if qty > 0:
                    cash -= target * qty
                    position = {"buy_price": target, "qty": qty, "high_price": th, "trailing": False}
    if position:
        cash += float(df.iloc[-1]["close"]) * position["qty"]
        trades.append({"pnl": 0})
    wins = [t for t in trades if t["pnl"] > 0]
    return {
        "pnl": cash - capital,
        "return_pct": (cash - capital) / capital * 100,
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
    }


def run_swing(df, capital=1_000_000):
    df = df.copy().reset_index(drop=True)
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    cash = capital
    position = None
    trades = []
    for i in range(61, len(df)):
        prev = df.iloc[i - 1]
        prev2 = df.iloc[i - 2]
        today = df.iloc[i]
        tc = float(today["close"])
        ma20 = float(prev["ma20"]) if not pd.isna(prev["ma20"]) else 0
        ma60 = float(prev["ma60"]) if not pd.isna(prev["ma60"]) else 0
        ma20_2 = float(prev2["ma20"]) if not pd.isna(prev2["ma20"]) else 0
        if position:
            bp = position["buy_price"]
            if (tc - bp) / bp <= -0.03:
                cash += tc * position["qty"]
                trades.append({"pnl": (tc - bp) * position["qty"]})
                position = None
                continue
            if ma20 > 0 and ma20 < ma20_2:
                cash += tc * position["qty"]
                trades.append({"pnl": (tc - bp) * position["qty"]})
                position = None
                continue
        if position is None and ma20 > 0 and ma60 > 0:
            if ma20 > ma60 and tc > ma20:
                qty = int(cash // tc)
                if qty > 0:
                    cash -= tc * qty
                    position = {"buy_price": tc, "qty": qty}
    if position:
        cash += float(df.iloc[-1]["close"]) * position["qty"]
        trades.append({"pnl": 0})
    wins = [t for t in trades if t["pnl"] > 0]
    return {
        "pnl": cash - capital,
        "return_pct": (cash - capital) / capital * 100,
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
    }


def run_trend(df, capital=1_000_000):
    df = df.copy().reset_index(drop=True)
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    cash = capital
    position = None
    trades = []
    for i in range(61, len(df)):
        prev = df.iloc[i - 1]
        prev2 = df.iloc[i - 2]
        today = df.iloc[i]
        tc = float(today["close"])
        ms = float(prev["ma5"]) if not pd.isna(prev["ma5"]) else 0
        ml = float(prev["ma20"]) if not pd.isna(prev["ma20"]) else 0
        ms2 = float(prev2["ma5"]) if not pd.isna(prev2["ma5"]) else 0
        ml2 = float(prev2["ma20"]) if not pd.isna(prev2["ma20"]) else 0
        m60 = float(prev["ma60"]) if not pd.isna(prev["ma60"]) else 0
        if position:
            if (tc - position["buy_price"]) / position["buy_price"] <= -0.05:
                cash += tc * position["qty"]
                trades.append({"pnl": (tc - position["buy_price"]) * position["qty"]})
                position = None
                continue
            if ms > 0 and ml > 0 and ms2 >= ml2 and ms < ml:
                cash += tc * position["qty"]
                trades.append({"pnl": (tc - position["buy_price"]) * position["qty"]})
                position = None
                continue
        if not position and ms > 0 and ml > 0 and ms2 <= ml2 and ms > ml:
            if m60 > 0 and tc > m60:
                qty = int(cash // tc)
                if qty > 0:
                    cash -= tc * qty
                    position = {"buy_price": tc, "qty": qty}
    if position:
        cash += float(df.iloc[-1]["close"]) * position["qty"]
        trades.append({"pnl": 0})
    wins = [t for t in trades if t["pnl"] > 0]
    return {
        "pnl": cash - capital,
        "return_pct": (cash - capital) / capital * 100,
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
    }


def main():
    tickers = {
        "069500.KS": "KODEX200",
        "005930.KS": "삼성전자",
        "105560.KS": "KB금융",
        "103590.KS": "일진전기",
    }

    periods = [
        ("2021", "2021-01-01", "2021-12-31"),
        ("2022 (하락장)", "2022-01-01", "2022-12-31"),
        ("2023 (횡보장)", "2023-01-01", "2023-12-31"),
        ("2024", "2024-01-01", "2024-12-31"),
        ("2025~현재", "2025-01-01", "2026-04-11"),
        ("전체 (2021~현재)", "2021-01-01", "2026-04-11"),
    ]

    for label, start, end in periods:
        print(f"\n{'=' * 75}")
        print(f"  {label} -- 시드 100만원")
        print(f"{'=' * 75}")
        print(
            f" {'종목':>10s} | {'데이트레이딩':>10s} {'거래':>3s} {'승률':>4s}"
            f" | {'스윙':>8s} {'거래':>3s} {'승률':>4s}"
            f" | {'추세추종':>8s} {'거래':>3s} {'승률':>4s}"
        )
        print(f" {'-' * 10} | {'-' * 18} | {'-' * 16} | {'-' * 16}")

        dt_t, sw_t, tr_t = 0, 0, 0
        for yf_t, name in tickers.items():
            df = download(yf_t, start, end)
            if len(df) < 70:
                print(f" {name:>10s} | 데이터 부족")
                continue
            d = run_daytrading(df)
            s = run_swing(df)
            t = run_trend(df)
            dt_t += d["pnl"]
            sw_t += s["pnl"]
            tr_t += t["pnl"]
            print(
                f" {name:>10s} |"
                f" {d['return_pct']:>+8.1f}% {d['trades']:>3} {d['win_rate']:>3.0f}%"
                f" | {s['return_pct']:>+6.1f}% {s['trades']:>3} {s['win_rate']:>3.0f}%"
                f" | {t['return_pct']:>+6.1f}% {t['trades']:>3} {t['win_rate']:>3.0f}%"
            )

        print(
            f"\n  합산: 데이트레이딩 {dt_t:>+12,.0f}원"
            f" | 스윙 {sw_t:>+12,.0f}원"
            f" | 추세추종 {tr_t:>+12,.0f}원"
        )
        results = {"데이트레이딩": dt_t, "스윙": sw_t, "추세추종": tr_t}
        winner = max(results, key=results.get)
        print(f"  >>> {winner} 승!")


if __name__ == "__main__":
    main()
