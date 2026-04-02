"""변동성 돌파 전략 백테스트 - 상승장+하락장 검증"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
import yfinance as yf


def backtest_vb(df: pd.DataFrame, k: float = 0.5, capital: int = 1_000_000,
                commission: float = 0.00015, tax: float = 0.0018) -> dict:
    """변동성 돌파 전략 백테스트.

    매수: 장중 현재가 >= 시가 + 전일레인지 * K, 시가 > MA10
    매도: 익일 시가
    """
    df = df.copy().reset_index(drop=True)
    df["range"] = df["high"] - df["low"]
    df["ma10"] = df["close"].rolling(10).mean()

    cash = capital
    holding = 0
    buy_price = 0
    trades = []

    for i in range(11, len(df)):
        prev = df.iloc[i - 1]
        today = df.iloc[i]

        today_open = int(today["open"])
        today_high = int(today["high"])
        today_close = int(today["close"])
        prev_range = int(prev["range"])
        prev_ma10 = float(prev["ma10"]) if not np.isnan(prev["ma10"]) else 0

        target = today_open + int(prev_range * k)

        # 보유 중이면 시가에 매도
        if holding > 0:
            revenue = today_open * holding
            cost = int(revenue * commission) + int(revenue * tax)
            pnl = (today_open - buy_price) * holding - cost - int(buy_price * holding * commission)
            cash += revenue - cost
            trades.append({
                "date": today.get("datetime", str(i)),
                "side": "sell", "price": today_open,
                "qty": holding, "pnl": pnl,
            })
            holding = 0

        # 마켓 필터: 시가 > 전일 MA10
        if prev_ma10 <= 0 or today_open <= prev_ma10:
            continue

        # 변동성 부족 필터
        if prev_range < today_open * 0.005:
            continue

        # 목표가 돌파: 장중 고가가 목표가 이상이면 매수 (목표가에 체결 가정)
        if today_high >= target and holding == 0:
            qty = cash // target
            if qty <= 0:
                continue
            cost = target * qty
            buy_commission = int(cost * commission)
            cash -= (cost + buy_commission)
            holding = qty
            buy_price = target
            trades.append({
                "date": today.get("datetime", str(i)),
                "side": "buy", "price": target, "qty": qty, "pnl": 0,
            })

    # 마지막 보유분 청산
    if holding > 0:
        last_close = int(df.iloc[-1]["close"])
        revenue = last_close * holding
        cost = int(revenue * commission) + int(revenue * tax)
        pnl = (last_close - buy_price) * holding - cost
        cash += revenue - cost
        trades.append({
            "date": str(df.iloc[-1].get("datetime", "last")),
            "side": "sell", "price": last_close, "qty": holding, "pnl": pnl,
        })

    sell_trades = [t for t in trades if t["side"] == "sell"]
    wins = [t for t in sell_trades if t["pnl"] > 0]
    losses = [t for t in sell_trades if t["pnl"] <= 0]
    total_return = (cash - capital) / capital * 100
    win_rate = len(wins) / len(sell_trades) * 100 if sell_trades else 0
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 1
    pf = gross_profit / gross_loss if gross_loss > 0 else 0

    return {
        "total_return": round(total_return, 2),
        "trades": len(sell_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "pf": round(pf, 2),
        "final_capital": cash,
        "sell_trades": sell_trades,
    }


def download(ticker, period="1y", start=None, end=None):
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


def main():
    tickers = {
        "005930.KS": "삼성전자", "069500.KS": "KODEX200",
        "006910.KS": "보성파워텍", "034020.KS": "두산에너빌",
        "078600.KS": "대주전자재료", "019180.KS": "티에이치엔",
        "103590.KS": "일진전기", "009420.KS": "한올바이오",
    }

    for label, period_args in [
        ("2025~2026 상승장", {"period": "1y"}),
        ("2022 하락장", {"start": "2022-01-01", "end": "2022-12-31"}),
    ]:
        print(f"\n{'='*60}")
        print(f"  {label} - 변동성 돌파 (K=0.5, 마켓필터 MA10)")
        print(f"{'='*60}")

        total_pnl = 0
        total_bnh = 0

        for t, n in tickers.items():
            df = download(t, **period_args)
            if len(df) < 30:
                continue

            # 변동성 돌파
            r = backtest_vb(df, k=0.5)
            # 바이앤홀드
            first = int(df.iloc[11]["close"])
            last = int(df.iloc[-1]["close"])
            bnh = (last - first) / first * 100

            total_pnl += r["final_capital"] - 1000000
            total_bnh += bnh

            marker = ">" if r["total_return"] > 0 else " "
            print(f" {marker} {n:10s} 전략{r['total_return']:>+7.1f}% BnH{bnh:>+7.1f}% "
                  f"{r['trades']:>3}거래 승률{r['win_rate']:>4.0f}% PF{r['pf']:>4.1f}")

        print(f"\n  합산 전략 손익: {total_pnl:>+,}원")


if __name__ == "__main__":
    main()
