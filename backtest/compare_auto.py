"""자동 전환(auto) vs 데이트레이딩 vs 추세추종 — 3종 비교."""
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


def run_auto(df, k=0.5, capital=1_000_000, stoploss_bull=0.02,
             trailing_act=0.025, trailing_stop=0.01, stoploss_bear=0.05):
    """자동 전환: 상승장=데이트레이딩, 하락장=추세추종."""
    df = df.copy().reset_index(drop=True)
    df["range"] = df["high"] - df["low"]
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()

    cash = capital
    position = None
    trades = []
    mode_log = {"bull": 0, "bear": 0}

    for i in range(61, len(df)):
        prev = df.iloc[i - 1]
        prev2 = df.iloc[i - 2]
        today = df.iloc[i]
        today_open = float(today["open"])
        today_high = float(today["high"])
        today_low = float(today["low"])
        today_close = float(today["close"])
        prev_range = float(prev["range"])

        ma20 = float(prev["ma20"]) if not pd.isna(prev["ma20"]) else 0
        ma60 = float(prev["ma60"]) if not pd.isna(prev["ma60"]) else 0
        regime = "bull" if (ma20 > 0 and ma60 > 0 and ma20 > ma60) else "bear"
        mode_log[regime] = mode_log.get(regime, 0) + 1

        # ── 상승장: 데이트레이딩 (변동성 돌파) ──
        if regime == "bull":
            if position is not None and position.get("mode") == "trend":
                # 추세추종 포지션 → 상승장 전환 시 유지 (트레일링으로 관리)
                pass

            if position is not None:
                buy_price = position["buy_price"]
                high_price = max(position["high_price"], today_high)
                position["high_price"] = high_price

                pct_from_buy = (today_low - buy_price) / buy_price
                if pct_from_buy <= -stoploss_bull:
                    sell_price = buy_price * (1 - stoploss_bull)
                    pnl = (sell_price - buy_price) * position["qty"]
                    cash += sell_price * position["qty"]
                    trades.append({"pnl": pnl, "mode": "bull"})
                    position = None
                    continue

                pct_high = (high_price - buy_price) / buy_price
                if pct_high >= trailing_act:
                    position["trailing"] = True

                if position.get("trailing"):
                    pct_from_high = (today_low - high_price) / high_price
                    if pct_from_high <= -trailing_stop:
                        sell_price = high_price * (1 - trailing_stop)
                        pnl = (sell_price - buy_price) * position["qty"]
                        cash += sell_price * position["qty"]
                        trades.append({"pnl": pnl, "mode": "bull"})
                        position = None
                        continue

            if position is None:
                target = today_open + prev_range * k
                if prev_range < today_open * 0.005:
                    continue
                if today_high >= target:
                    buy_price = target
                    qty = int(cash // buy_price)
                    if qty > 0:
                        cash -= buy_price * qty
                        position = {"buy_price": buy_price, "qty": qty,
                                    "high_price": today_high, "trailing": False, "mode": "bull"}

        # ── 하락장: 추세추종 (골든/데드크로스) ──
        else:
            ma5 = float(prev["ma5"]) if not pd.isna(prev["ma5"]) else 0
            ma5_2 = float(prev2["ma5"]) if not pd.isna(prev2["ma5"]) else 0
            ma20_2 = float(prev2["ma20"]) if not pd.isna(prev2["ma20"]) else 0

            if position is not None:
                buy_price = position["buy_price"]
                pct = (today_close - buy_price) / buy_price

                if pct <= -stoploss_bear:
                    pnl = (today_close - buy_price) * position["qty"]
                    cash += today_close * position["qty"]
                    trades.append({"pnl": pnl, "mode": "bear"})
                    position = None
                    continue

                if ma5 > 0 and ma20 > 0 and ma5_2 >= ma20_2 and ma5 < ma20:
                    pnl = (today_close - buy_price) * position["qty"]
                    cash += today_close * position["qty"]
                    trades.append({"pnl": pnl, "mode": "bear"})
                    position = None
                    continue

            if position is None:
                if ma5 > 0 and ma20 > 0 and ma5_2 <= ma20_2 and ma5 > ma20:
                    if today_close > ma60:
                        buy_price = today_close
                        qty = int(cash // buy_price)
                        if qty > 0:
                            cash -= buy_price * qty
                            position = {"buy_price": buy_price, "qty": qty,
                                        "high_price": today_high, "trailing": False, "mode": "trend"}

    if position is not None:
        sell_price = float(df.iloc[-1]["close"])
        pnl = (sell_price - position["buy_price"]) * position["qty"]
        cash += sell_price * position["qty"]
        trades.append({"pnl": pnl, "mode": position.get("mode", "?")})

    wins = [t for t in trades if t["pnl"] > 0]
    return {
        "final": cash,
        "return_pct": (cash - capital) / capital * 100,
        "pnl": cash - capital,
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "bull_days": mode_log.get("bull", 0),
        "bear_days": mode_log.get("bear", 0),
    }


def run_daytrading(df, k=0.5, capital=1_000_000):
    """데이트레이딩 (기존)."""
    df = df.copy().reset_index(drop=True)
    df["range"] = df["high"] - df["low"]
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
        prev_range = float(prev["range"])
        target = today_open + prev_range * k
        if position is not None:
            bp = position["buy_price"]
            hp = max(position["high_price"], today_high)
            position["high_price"] = hp
            if (today_low - bp) / bp <= -0.02:
                cash += bp * 0.98 * position["qty"]
                trades.append({"pnl": (bp * 0.98 - bp) * position["qty"]})
                position = None
                continue
            if (hp - bp) / bp >= 0.025:
                position["trailing"] = True
            if position.get("trailing") and (today_low - hp) / hp <= -0.01:
                sp = hp * 0.99
                cash += sp * position["qty"]
                trades.append({"pnl": (sp - bp) * position["qty"]})
                position = None
                continue
        if position is None:
            ma20 = float(prev["ma20"]) if not pd.isna(prev["ma20"]) else 0
            ma60 = float(prev["ma60"]) if not pd.isna(prev["ma60"]) else 0
            if ma20 > 0 and ma60 > 0 and ma20 < ma60:
                continue
            if prev_range < today_open * 0.005:
                continue
            if today_high >= target:
                qty = int(cash // target)
                if qty > 0:
                    cash -= target * qty
                    position = {"buy_price": target, "qty": qty,
                                "high_price": today_high, "trailing": False}
    if position:
        cash += float(df.iloc[-1]["close"]) * position["qty"]
        trades.append({"pnl": 0})
    wins = [t for t in trades if t["pnl"] > 0]
    return {"final": cash, "return_pct": (cash - capital) / capital * 100,
            "pnl": cash - capital, "trades": len(trades),
            "win_rate": len(wins) / len(trades) * 100 if trades else 0}


def run_trend(df, capital=1_000_000):
    """추세추종 (기존)."""
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
    return {"final": cash, "return_pct": (cash - capital) / capital * 100,
            "pnl": cash - capital, "trades": len(trades),
            "win_rate": len(wins) / len(trades) * 100 if trades else 0}


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

    for label, start, end in [
        ("최근 6개월 (상승장)", "2025-10-01", "2026-04-07"),
        ("2022년 (하락장)", "2022-01-01", "2022-12-31"),
    ]:
        print(f"\n{'='*70}")
        print(f"  시드 {capital:,}원 — {label}")
        print(f"{'='*70}")
        print(f" {'종목':>10s}  | {'데이트레이딩':>10s} | {'추세추종':>8s} | {'AUTO':>10s} | 승자")
        print(f" {'-'*10}  | {'-'*10} | {'-'*8} | {'-'*10} | ----")

        dt, tt, at = 0, 0, 0
        for yf_t, name in tickers.items():
            df = download(yf_t, start=start, end=end)
            if len(df) < 70:
                continue
            d = run_daytrading(df, capital=capital)
            t = run_trend(df, capital=capital)
            a = run_auto(df, capital=capital)
            dt += d["pnl"]
            tt += t["pnl"]
            at += a["pnl"]

            results = {"데이트레이딩": d["return_pct"], "추세추종": t["return_pct"], "AUTO": a["return_pct"]}
            winner = max(results, key=results.get)

            print(
                f" {name:>10s}  | "
                f"{d['return_pct']:>+8.1f}% | "
                f"{t['return_pct']:>+6.1f}% | "
                f"{a['return_pct']:>+8.1f}% | "
                f"{winner}"
            )

        print(f"\n  합산 손익:")
        print(f"    데이트레이딩: {dt:>+12,.0f}원")
        print(f"    추세추종:     {tt:>+12,.0f}원")
        print(f"    AUTO:         {at:>+12,.0f}원")

        results = {"데이트레이딩": dt, "추세추종": tt, "AUTO": at}
        winner = max(results, key=results.get)
        print(f"\n  >>> {winner} 승! <<<")


if __name__ == "__main__":
    main()
