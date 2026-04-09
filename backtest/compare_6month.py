"""시드 100만원 × 6개월 — 데이트레이딩 vs 추세추종 비교."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import yfinance as yf


def download(ticker: str, start: str, end: str) -> pd.DataFrame:
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


def run_daytrading(df: pd.DataFrame, k: float = 0.5, capital: int = 1_000_000,
                   stoploss: float = 0.02, trailing_act: float = 0.025,
                   trailing_stop: float = 0.01) -> dict:
    """변동성 돌파 (데이트레이딩)."""
    df = df.copy().reset_index(drop=True)
    df["range"] = df["high"] - df["low"]
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()

    cash = capital
    position = None
    trades = []
    equity_curve = []

    for i in range(61, len(df)):
        prev = df.iloc[i - 1]
        today = df.iloc[i]
        today_open = float(today["open"])
        today_high = float(today["high"])
        today_low = float(today["low"])
        today_close = float(today["close"])
        prev_range = float(prev["range"])
        target = today_open + prev_range * k

        if position is not None:
            buy_price = position["buy_price"]
            high_price = max(position["high_price"], today_high)
            position["high_price"] = high_price
            pct_from_buy = (today_low - buy_price) / buy_price
            pct_from_high = (today_low - high_price) / high_price

            if pct_from_buy <= -stoploss:
                sell_price = buy_price * (1 - stoploss)
                pnl = (sell_price - buy_price) * position["qty"]
                cash += sell_price * position["qty"]
                trades.append({"pnl": pnl, "reason": "손절", "date": today["datetime"]})
                position = None
                equity_curve.append({"date": today["datetime"], "equity": cash})
                continue

            pct_from_buy_high = (high_price - buy_price) / buy_price
            if pct_from_buy_high >= trailing_act:
                position["trailing"] = True

            if position.get("trailing") and pct_from_high <= -trailing_stop:
                sell_price = high_price * (1 - trailing_stop)
                pnl = (sell_price - buy_price) * position["qty"]
                cash += sell_price * position["qty"]
                trades.append({"pnl": pnl, "reason": "트레일링", "date": today["datetime"]})
                position = None
                equity_curve.append({"date": today["datetime"], "equity": cash})
                continue

        if position is None:
            ma20 = float(prev["ma20"]) if not pd.isna(prev["ma20"]) else 0
            ma60 = float(prev["ma60"]) if not pd.isna(prev["ma60"]) else 0
            if ma20 > 0 and ma60 > 0 and ma20 < ma60:
                equity_curve.append({"date": today["datetime"], "equity": cash})
                continue
            if prev_range < today_open * 0.005:
                equity_curve.append({"date": today["datetime"], "equity": cash})
                continue
            if today_high >= target:
                buy_price = target
                qty = int(cash // buy_price)
                if qty > 0:
                    cash -= buy_price * qty
                    position = {"buy_price": buy_price, "qty": qty,
                                "high_price": today_high, "trailing": False}

        eq = cash + (position["qty"] * today_close if position else 0)
        equity_curve.append({"date": today["datetime"], "equity": eq})

    if position is not None:
        sell_price = float(df.iloc[-1]["close"])
        pnl = (sell_price - position["buy_price"]) * position["qty"]
        cash += sell_price * position["qty"]
        trades.append({"pnl": pnl, "reason": "마감", "date": df.iloc[-1]["datetime"]})
        position = None

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_win = sum(t["pnl"] for t in wins)
    total_loss = abs(sum(t["pnl"] for t in losses))

    eq_values = [e["equity"] for e in equity_curve]
    mdd = 0
    peak = capital
    for v in eq_values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > mdd:
            mdd = dd

    return {
        "final": cash,
        "return_pct": (cash - capital) / capital * 100,
        "pnl": cash - capital,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "avg_win": total_win / len(wins) if wins else 0,
        "avg_loss": total_loss / len(losses) if losses else 0,
        "mdd": mdd,
    }


def run_trend_following(df: pd.DataFrame, capital: int = 1_000_000,
                        short_ma: int = 5, long_ma: int = 20,
                        stoploss: float = 0.05) -> dict:
    """추세 추종 (골든/데드크로스)."""
    df = df.copy().reset_index(drop=True)
    df["ma_short"] = df["close"].rolling(short_ma).mean()
    df["ma_long"] = df["close"].rolling(long_ma).mean()
    df["ma60"] = df["close"].rolling(60).mean()

    cash = capital
    position = None
    trades = []
    equity_curve = []

    for i in range(61, len(df)):
        prev = df.iloc[i - 1]
        prev2 = df.iloc[i - 2]
        today = df.iloc[i]
        today_close = float(today["close"])

        ma_s = float(prev["ma_short"]) if not pd.isna(prev["ma_short"]) else 0
        ma_l = float(prev["ma_long"]) if not pd.isna(prev["ma_long"]) else 0
        ma_s2 = float(prev2["ma_short"]) if not pd.isna(prev2["ma_short"]) else 0
        ma_l2 = float(prev2["ma_long"]) if not pd.isna(prev2["ma_long"]) else 0
        ma60 = float(prev["ma60"]) if not pd.isna(prev["ma60"]) else 0

        if position is not None:
            buy_price = position["buy_price"]
            pct = (today_close - buy_price) / buy_price

            if pct <= -stoploss:
                pnl = (today_close - buy_price) * position["qty"]
                cash += today_close * position["qty"]
                trades.append({"pnl": pnl, "reason": "손절", "date": today["datetime"]})
                position = None
                equity_curve.append({"date": today["datetime"], "equity": cash})
                continue

            if ma_s > 0 and ma_l > 0 and ma_s2 >= ma_l2 and ma_s < ma_l:
                pnl = (today_close - buy_price) * position["qty"]
                cash += today_close * position["qty"]
                trades.append({"pnl": pnl, "reason": "데드크로스", "date": today["datetime"]})
                position = None
                equity_curve.append({"date": today["datetime"], "equity": cash})
                continue

        if position is None:
            if ma_s > 0 and ma_l > 0 and ma_s2 <= ma_l2 and ma_s > ma_l:
                if ma60 > 0 and today_close > ma60:
                    buy_price = today_close
                    qty = int(cash // buy_price)
                    if qty > 0:
                        cash -= buy_price * qty
                        position = {"buy_price": buy_price, "qty": qty}

        eq = cash + (position["qty"] * today_close if position else 0)
        equity_curve.append({"date": today["datetime"], "equity": eq})

    if position is not None:
        sell_price = float(df.iloc[-1]["close"])
        pnl = (sell_price - position["buy_price"]) * position["qty"]
        cash += sell_price * position["qty"]
        trades.append({"pnl": pnl, "reason": "마감", "date": df.iloc[-1]["datetime"]})
        position = None

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_win = sum(t["pnl"] for t in wins)
    total_loss = abs(sum(t["pnl"] for t in losses))

    eq_values = [e["equity"] for e in equity_curve]
    mdd = 0
    peak = capital
    for v in eq_values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > mdd:
            mdd = dd

    return {
        "final": cash,
        "return_pct": (cash - capital) / capital * 100,
        "pnl": cash - capital,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "avg_win": total_win / len(wins) if wins else 0,
        "avg_loss": total_loss / len(losses) if losses else 0,
        "mdd": mdd,
    }


def main():
    capital = 1_000_000

    tickers = {
        "069500.KS": "KODEX200",
        "229200.KS": "KODEX코스닥150",
        "133690.KS": "TIGER나스닥100",
        "005930.KS": "삼성전자",
        "105560.KS": "KB금융",
        "034020.KS": "두산에너빌",
    }

    print(f"\n{'='*70}")
    print(f"  시드 {capital:,}원 × 6개월 — 데이트레이딩 vs 추세추종")
    print(f"  기간: 2025-10-01 ~ 2026-04-07")
    print(f"{'='*70}")

    day_total_pnl = 0
    trend_total_pnl = 0

    for yf_ticker, name in tickers.items():
        df = download(yf_ticker, start="2025-10-01", end="2026-04-07")
        if len(df) < 70:
            print(f"\n  {name}: 데이터 부족")
            continue

        day = run_daytrading(df, capital=capital)
        trend = run_trend_following(df, capital=capital)

        day_total_pnl += day["pnl"]
        trend_total_pnl += trend["pnl"]

        print(f"\n  ┌─────────────────────────────────────────────")
        print(f"  │ {name}")
        print(f"  ├─────────────────────────────────────────────")
        print(f"  │           데이트레이딩          추세추종")
        print(f"  │ 최종자산  {day['final']:>12,.0f}원   {trend['final']:>12,.0f}원")
        print(f"  │ 수익률    {day['return_pct']:>+10.1f}%     {trend['return_pct']:>+10.1f}%")
        print(f"  │ 손익      {day['pnl']:>+10,.0f}원   {trend['pnl']:>+10,.0f}원")
        print(f"  │ 거래횟수  {day['trades']:>8}회       {trend['trades']:>8}회")
        print(f"  │ 승/패     {day['wins']:>4}승 {day['losses']}패      {trend['wins']:>4}승 {trend['losses']}패")
        print(f"  │ 승률      {day['win_rate']:>9.0f}%      {trend['win_rate']:>9.0f}%")
        print(f"  │ 평균이익  {day['avg_win']:>+10,.0f}원   {trend['avg_win']:>+10,.0f}원")
        print(f"  │ 평균손실  {day['avg_loss']:>10,.0f}원    {trend['avg_loss']:>10,.0f}원")
        print(f"  │ 최대낙폭  {day['mdd']:>9.1f}%      {trend['mdd']:>9.1f}%")

        if day["return_pct"] > trend["return_pct"]:
            print(f"  │ >>> 데이트레이딩 승!")
        elif trend["return_pct"] > day["return_pct"]:
            print(f"  │ >>> 추세추종 승!")
        else:
            print(f"  │ >>> 무승부")
        print(f"  └─────────────────────────────────────────────")

    print(f"\n{'='*70}")
    print(f"  종합 결과 (시드 {capital:,}원 × 6종목)")
    print(f"{'='*70}")
    print(f"  데이트레이딩 합산 손익: {day_total_pnl:>+12,.0f}원")
    print(f"  추세추종     합산 손익: {trend_total_pnl:>+12,.0f}원")
    print(f"")
    if day_total_pnl > trend_total_pnl:
        diff = day_total_pnl - trend_total_pnl
        print(f"  >>> 데이트레이딩이 {diff:,.0f}원 더 벌었어요!")
    elif trend_total_pnl > day_total_pnl:
        diff = trend_total_pnl - day_total_pnl
        print(f"  >>> 추세추종이 {diff:,.0f}원 더 벌었어요!")
    else:
        print(f"  >>> 무승부!")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
