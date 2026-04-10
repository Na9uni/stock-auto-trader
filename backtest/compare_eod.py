"""장마감 강제 청산 효과 비교 — 당일 청산 vs 오버나이트 보유."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import yfinance as yf


def download(ticker, period="1y"):
    raw = yf.download(ticker, period=period, auto_adjust=True, progress=False)
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


def run_vb(df, k=0.5, capital=1_000_000, stoploss=0.02, trailing_act=0.025,
           trailing_stop=0.01, force_eod=False):
    """변동성 돌파 백테스트. force_eod=True면 당일 종가 강제 청산."""
    df = df.copy().reset_index(drop=True)
    if len(df) >= 7:
        ranges = df["high"].iloc[:-1] - df["low"].iloc[:-1]
        df["atr5"] = ranges.rolling(5).mean()
    else:
        df["atr5"] = df["high"] - df["low"]
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()

    cash = capital
    position = None
    trades = []

    for i in range(61, len(df)):
        prev = df.iloc[i - 1]
        today = df.iloc[i]
        today_open = float(today["open"])
        today_high = float(today["high"])
        today_low = float(today["low"])
        today_close = float(today["close"])
        prev_atr = float(prev["atr5"]) if not pd.isna(prev.get("atr5", float("nan"))) else float(prev["high"] - prev["low"])
        target = today_open + prev_atr * k

        if position is not None:
            buy_price = position["buy_price"]
            high_price = max(position["high_price"], today_high)
            position["high_price"] = high_price

            # 손절
            if (today_low - buy_price) / buy_price <= -stoploss:
                sell_price = buy_price * (1 - stoploss)
                pnl = (sell_price - buy_price) * position["qty"]
                cash += sell_price * position["qty"]
                trades.append({"pnl": pnl, "type": "손절"})
                position = None
                continue

            # 트레일링
            if (high_price - buy_price) / buy_price >= trailing_act:
                position["trailing"] = True
            if position.get("trailing") and (today_low - high_price) / high_price <= -trailing_stop:
                sell_price = high_price * (1 - trailing_stop)
                pnl = (sell_price - buy_price) * position["qty"]
                cash += sell_price * position["qty"]
                trades.append({"pnl": pnl, "type": "트레일링"})
                position = None
                continue

            # 장마감 강제 청산 (force_eod 모드)
            if force_eod:
                pnl = (today_close - buy_price) * position["qty"]
                cash += today_close * position["qty"]
                trades.append({"pnl": pnl, "type": "장마감청산"})
                position = None
                continue

        # 매수
        if position is None:
            ma20 = float(prev["ma20"]) if not pd.isna(prev["ma20"]) else 0
            ma60 = float(prev["ma60"]) if not pd.isna(prev["ma60"]) else 0
            if ma20 > 0 and ma60 > 0 and ma20 < ma60:
                continue
            if prev_atr < today_open * 0.005:
                continue
            if today_high >= target:
                buy_price = target
                qty = int(cash // buy_price)
                if qty > 0:
                    cash -= buy_price * qty
                    position = {"buy_price": buy_price, "qty": qty,
                                "high_price": today_high, "trailing": False}

    if position:
        cash += float(df.iloc[-1]["close"]) * position["qty"]
        trades.append({"pnl": 0, "type": "마감"})

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    total_pnl = cash - capital
    return {
        "return_pct": total_pnl / capital * 100,
        "pnl": total_pnl,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "types": {t: len([x for x in trades if x["type"] == t]) for t in set(x["type"] for x in trades)},
    }


def main():
    tickers = {
        "069500.KS": "KODEX200",
        "229200.KS": "KODEX코스닥150",
        "133690.KS": "TIGER나스닥100",
        "005930.KS": "삼성전자",
        "105560.KS": "KB금융",
        "103590.KS": "일진전기",
    }

    for label, period in [("최근 1년", "1y"), ("2022 하락장", None)]:
        print(f"\n{'='*70}")
        print(f"  {label} — 당일 청산 vs 오버나이트 보유")
        print(f"{'='*70}")
        print(f" {'종목':>12s}  | {'오버나이트':>10s} {'거래':>4s} | {'당일청산':>8s} {'거래':>4s} | 차이")
        print(f" {'-'*12}  | {'-'*10} {'-'*4} | {'-'*8} {'-'*4} | ----")

        ov_total, eod_total = 0, 0
        for yf_t, name in tickers.items():
            if period:
                df = download(yf_t, period=period)
            else:
                raw = yf.download(yf_t, start="2022-01-01", end="2022-12-31",
                                  auto_adjust=True, progress=False)
                if raw.empty:
                    continue
                df = raw.copy()
                df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
                df = df.reset_index()
                dc = [c for c in df.columns if "date" in str(c).lower()]
                if dc:
                    df = df.rename(columns={dc[0]: "datetime"})
                df["datetime"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y%m%d")
                df = df[["datetime", "open", "high", "low", "close", "volume"]]

            if len(df) < 70:
                continue

            ov = run_vb(df, force_eod=False)
            eod = run_vb(df, force_eod=True)
            ov_total += ov["pnl"]
            eod_total += eod["pnl"]

            diff = eod["return_pct"] - ov["return_pct"]
            better = "당일청산👍" if diff > 0 else "오버나이트" if diff < 0 else "동일"

            print(
                f" {name:>12s}  | "
                f"{ov['return_pct']:>+8.1f}% {ov['trades']:>4}회 | "
                f"{eod['return_pct']:>+6.1f}% {eod['trades']:>4}회 | "
                f"{diff:>+5.1f}% {better}"
            )

        print(f"\n  합산 — 오버나이트: {ov_total:>+12,.0f}원 | 당일청산: {eod_total:>+12,.0f}원")
        diff_total = eod_total - ov_total
        print(f"  차이: {diff_total:>+12,.0f}원 {'(당일청산 유리)' if diff_total > 0 else '(오버나이트 유리)'}")


if __name__ == "__main__":
    main()
