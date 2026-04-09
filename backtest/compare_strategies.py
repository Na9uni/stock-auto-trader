"""데이트레이딩 vs 추세추종 비교 백테스트.

변동성 돌파(데이트레이딩) vs 이동평균 추세추종(며칠 보유) 성과 비교.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import yfinance as yf


def download(ticker: str, period: str = "1y",
             start: str | None = None, end: str | None = None) -> pd.DataFrame:
    if start and end:
        raw = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
    else:
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


def run_daytrading(df: pd.DataFrame, k: float = 0.5,
                   stoploss: float = 0.02, trailing_act: float = 0.025,
                   trailing_stop: float = 0.01) -> dict:
    """변동성 돌파 (데이트레이딩 스타일) — 트레일링 스탑 청산."""
    df = df.copy().reset_index(drop=True)
    df["range"] = df["high"] - df["low"]
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()

    capital = 1_000_000
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

        # 보유 중: 청산 판단
        if position is not None:
            buy_price = position["buy_price"]
            high_price = max(position["high_price"], today_high)
            position["high_price"] = high_price

            pct_from_buy = (today_low - buy_price) / buy_price
            pct_from_high = (today_low - high_price) / high_price

            # 손절
            if pct_from_buy <= -stoploss:
                sell_price = buy_price * (1 - stoploss)
                pnl = (sell_price - buy_price) * position["qty"]
                cash += sell_price * position["qty"]
                trades.append({"pnl": pnl, "reason": "손절"})
                position = None
                continue

            # 트레일링 활성화 체크
            pct_from_buy_high = (high_price - buy_price) / buy_price
            if pct_from_buy_high >= trailing_act:
                position["trailing"] = True

            # 트레일링 스탑
            if position.get("trailing") and pct_from_high <= -trailing_stop:
                sell_price = high_price * (1 - trailing_stop)
                pnl = (sell_price - buy_price) * position["qty"]
                cash += sell_price * position["qty"]
                trades.append({"pnl": pnl, "reason": "트레일링"})
                position = None
                continue

        # 매수 판단
        if position is None:
            ma20 = float(prev["ma20"]) if not pd.isna(prev["ma20"]) else 0
            ma60 = float(prev["ma60"]) if not pd.isna(prev["ma60"]) else 0
            if ma20 > 0 and ma60 > 0 and ma20 < ma60:
                continue

            if prev_range < today_open * 0.005:
                continue

            if today_high >= target and today_open > float(prev.get("close", 0)) * 0.98:
                buy_price = target
                qty = int(cash // buy_price)
                if qty > 0:
                    cash -= buy_price * qty
                    position = {"buy_price": buy_price, "qty": qty,
                                "high_price": today_high, "trailing": False}

    # 마지막 보유분 청산
    if position is not None:
        sell_price = float(df.iloc[-1]["close"])
        pnl = (sell_price - position["buy_price"]) * position["qty"]
        cash += sell_price * position["qty"]
        trades.append({"pnl": pnl, "reason": "마감"})
        position = None

    total_return = (cash - capital) / capital * 100
    wins = [t for t in trades if t["pnl"] > 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0

    return {
        "total_return": total_return,
        "trades": len(trades),
        "win_rate": win_rate,
        "total_pnl": cash - capital,
    }


def run_trend_following(df: pd.DataFrame, short_ma: int = 5,
                        long_ma: int = 20, stoploss: float = 0.05) -> dict:
    """추세 추종 (며칠 보유) — 이동평균 골든/데드크로스 기반."""
    df = df.copy().reset_index(drop=True)
    df["ma_short"] = df["close"].rolling(short_ma).mean()
    df["ma_long"] = df["close"].rolling(long_ma).mean()
    df["ma60"] = df["close"].rolling(60).mean()

    capital = 1_000_000
    cash = capital
    position = None
    trades = []

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

        # 보유 중: 매도 판단
        if position is not None:
            buy_price = position["buy_price"]
            pct = (today_close - buy_price) / buy_price

            # 손절
            if pct <= -stoploss:
                pnl = (today_close - buy_price) * position["qty"]
                cash += today_close * position["qty"]
                trades.append({"pnl": pnl, "reason": "손절"})
                position = None
                continue

            # 데드크로스 (단기선이 장기선 아래로) → 매도
            if ma_s > 0 and ma_l > 0 and ma_s2 >= ma_l2 and ma_s < ma_l:
                pnl = (today_close - buy_price) * position["qty"]
                cash += today_close * position["qty"]
                trades.append({"pnl": pnl, "reason": "데드크로스"})
                position = None
                continue

        # 매수 판단: 골든크로스 (단기선이 장기선 위로)
        if position is None:
            if ma_s > 0 and ma_l > 0 and ma_s2 <= ma_l2 and ma_s > ma_l:
                # 추가 필터: 60일선 위에서만 매수
                if ma60 > 0 and today_close > ma60:
                    buy_price = today_close
                    qty = int(cash // buy_price)
                    if qty > 0:
                        cash -= buy_price * qty
                        position = {"buy_price": buy_price, "qty": qty}

    # 마지막 보유분 청산
    if position is not None:
        sell_price = float(df.iloc[-1]["close"])
        pnl = (sell_price - position["buy_price"]) * position["qty"]
        cash += sell_price * position["qty"]
        trades.append({"pnl": pnl, "reason": "마감"})
        position = None

    total_return = (cash - capital) / capital * 100
    wins = [t for t in trades if t["pnl"] > 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0

    return {
        "total_return": total_return,
        "trades": len(trades),
        "win_rate": win_rate,
        "total_pnl": cash - capital,
    }


def main():
    tickers = {
        "069500.KS": "KODEX200",
        "229200.KS": "KODEX코스닥150",
        "133690.KS": "TIGER나스닥100",
        "005930.KS": "삼성전자",
        "105560.KS": "KB금융",
        "034020.KS": "두산에너빌",
    }

    for label, period_args in [
        ("최근 1년", {"period": "1y"}),
        ("2022 하락장", {"start": "2022-01-01", "end": "2022-12-31"}),
    ]:
        print(f"\n{'='*70}")
        print(f"  {label} — 데이트레이딩 vs 추세추종 비교")
        print(f"{'='*70}")
        print(f" {'종목':>12s}  | {'데이트레이딩':>14s} {'거래':>4s} {'승률':>5s} | {'추세추종':>10s} {'거래':>4s} {'승률':>5s} | 승자")
        print(f" {'-'*12}  | {'-'*14} {'-'*4} {'-'*5} | {'-'*10} {'-'*4} {'-'*5} | ----")

        day_total = 0
        trend_total = 0

        for yf_ticker, name in tickers.items():
            df = download(yf_ticker, **period_args)
            if len(df) < 70:
                print(f" {name:>12s}  | 데이터 부족")
                continue

            day = run_daytrading(df)
            trend = run_trend_following(df)

            day_total += day["total_pnl"]
            trend_total += trend["total_pnl"]

            if day["total_return"] > trend["total_return"]:
                winner = "데이트레이딩"
            elif trend["total_return"] > day["total_return"]:
                winner = "추세추종"
            else:
                winner = "무승부"

            print(
                f" {name:>12s}  | "
                f"{day['total_return']:>+10.1f}% "
                f"{day['trades']:>4}회 "
                f"{day['win_rate']:>4.0f}% | "
                f"{trend['total_return']:>+7.1f}% "
                f"{trend['trades']:>4}회 "
                f"{trend['win_rate']:>4.0f}% | "
                f"{winner}"
            )

        print(f"\n  합산 손익 — 데이트레이딩: {day_total:>+,.0f}원 | 추세추종: {trend_total:>+,.0f}원")
        if day_total > trend_total:
            print(f"  >>> 데이트레이딩 승!")
        elif trend_total > day_total:
            print(f"  >>> 추세추종 승!")
        else:
            print(f"  >>> 무승부!")


if __name__ == "__main__":
    main()
