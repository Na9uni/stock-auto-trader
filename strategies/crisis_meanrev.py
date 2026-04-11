"""위기장 평균회귀 전략 — 일봉 RSI(2) 기반.

Connors & Alvarez (2009) 원논문과 동일한 타임프레임(일봉).
실전과 백테스트가 같은 조건을 사용한다.

진입: 일봉 RSI(2) < 15 + 반등 확인 + 당일 -2% 하락 + 종가 매수
청산: 트레일링 스탑 / 손절 -2% / RSI2>=80 과매수 / 시간청산 48h
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import yfinance as yf

from config.trading_config import TradingConfig
from strategies.base import MarketContext, SignalResult, SignalStrength, SignalType

logger = logging.getLogger("stock_analysis")


class CrisisMeanRevStrategy:
    """위기장 평균회귀 전략 — Strategy Protocol 구현.

    일봉 RSI(2) < 15 + 당일 -2% 하락 + 반등 확인 시 BUY.
    ETF 전용으로 AUTO 하락장 모드에서 2차 신호로 사용.
    """

    name = "crisis_meanrev"

    def __init__(self, config: TradingConfig):
        self._config = config
        self._rsi_entry: float = 15
        self._drop_threshold: float = -2.0

    def evaluate(self, ctx: MarketContext) -> SignalResult:
        """일봉 RSI(2) 기반 위기 평균회귀 신호 판단."""
        neutral = SignalResult(
            signal_type=SignalType.NEUTRAL,
            strength=SignalStrength.WEAK,
            reasons=["[위기MR] 조건 미충족"],
            strategy_name=self.name,
        )

        candles = ctx.candles_1d_raw
        if not candles or len(candles) < 6:
            neutral.reasons = ["[위기MR] 일봉 데이터 부족"]
            return neutral

        df = pd.DataFrame(candles)
        for col in ("open", "high", "low", "close"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        sort_col = None
        for candidate in ("datetime", "date", "Date"):
            if candidate in df.columns:
                sort_col = candidate
                break
        if sort_col is not None:
            df = df.sort_values(sort_col).reset_index(drop=True)

        # RSI(2) 계산
        df["rsi2"] = rsi_2(df["close"])
        df["change_pct"] = df["close"].pct_change() * 100

        today = df.iloc[-1]
        prev = df.iloc[-2]

        today_rsi2 = float(today["rsi2"]) if not pd.isna(today["rsi2"]) else 50
        prev_rsi2 = float(prev["rsi2"]) if not pd.isna(prev["rsi2"]) else 50
        today_change = float(today["change_pct"]) if not pd.isna(today["change_pct"]) else 0

        # 조건 1: RSI(2) < 15 (과매도)
        oversold = today_rsi2 < self._rsi_entry
        # 조건 2: 당일 -2% 이하 하락
        drop = today_change <= self._drop_threshold
        # 조건 3: 반등 확인 (전일 RSI<10 & 오늘 반등, 또는 RSI<5 극단)
        bounce = (prev_rsi2 < 10 and today_rsi2 > prev_rsi2) or today_rsi2 < 5

        if oversold and drop and bounce:
            logger.info(
                "[위기MR] %s 매수 신호: RSI2=%.0f(전일%.0f), 등락=%.1f%%",
                ctx.name, today_rsi2, prev_rsi2, today_change,
            )
            return SignalResult(
                signal_type=SignalType.BUY,
                strength=SignalStrength.STRONG,
                reasons=[
                    f"RSI2={today_rsi2:.0f}(전일{prev_rsi2:.0f})",
                    f"등락={today_change:+.1f}%",
                    "반등 확인",
                ],
                strategy_name=self.name,
            )

        return neutral


def rsi_2(prices: pd.Series) -> pd.Series:
    """RSI(2) — Wilder EMA, period=2."""
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=0.5, min_periods=2, adjust=False).mean()
    avg_loss = loss.ewm(alpha=0.5, min_periods=2, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi[(avg_gain == 0) & (avg_loss == 0)] = 50
    return rsi


def backtest_crisis_meanrev(
    df: pd.DataFrame,
    capital: int = 700_000,
    rsi_entry: float = 15,          # RSI(2) 이 이하 + 반등 확인
    rsi_exit: float = 80,           # RSI(2) 이 이상이면 매도 (수익 중만)
    drop_threshold: float = -2.0,   # 당일 등락률 이 이하면 매수
    trailing_activate: float = 2.0, # +N%에서 트레일링 활성화
    trailing_drop: float = 1.5,     # 고점 대비 -N%에서 청산
    stop_loss: float = -2.0,        # 손절
    max_hold_days: int = 2,         # 최대 보유일 (48h)
    early_exit_days: int = 1,       # 손실 중 N일 후 조기 청산
    slippage: float = 0.0005,       # 0.05% (ETF)
    commission: float = 0.00015,    # 편도 0.015%
) -> dict:
    """위기장 평균회귀 백테스트 — 실전과 동일한 로직."""
    import numpy as np

    df = df.copy().reset_index(drop=True)
    df["rsi2"] = rsi_2(df["close"])
    df["change_pct"] = df["close"].pct_change() * 100

    cash = capital
    position = None  # {qty, buy_price, entry_day, high_price, trailing_activated}
    trades = []
    equity_curve = []

    for i in range(5, len(df)):
        today = df.iloc[i]
        prev = df.iloc[i - 1]
        today_date = str(today.get("datetime", i))
        today_close = float(today["close"])
        today_high = float(today["high"])
        today_low = float(today["low"])
        today_rsi2 = float(today["rsi2"]) if not pd.isna(today["rsi2"]) else 50
        prev_rsi2 = float(prev["rsi2"]) if not pd.isna(prev["rsi2"]) else 50
        today_change = float(today["change_pct"]) if not pd.isna(today["change_pct"]) else 0

        # ── 보유 중: 청산 ──
        if position is not None:
            days_held = i - position["entry_day"]
            pct = (today_close - position["buy_price"]) / position["buy_price"] * 100
            pct_low = (today_low - position["buy_price"]) / position["buy_price"] * 100

            # 고점 갱신
            if today_high > position["high_price"]:
                position["high_price"] = today_high
            drop_from_high = (position["high_price"] - today_low) / position["high_price"] * 100

            sell = False
            sell_price = today_close
            reason = ""

            # 1) 손절 (장중 저가 기준)
            if pct_low <= stop_loss:
                sell = True
                sell_price = position["buy_price"] * (1 + stop_loss / 100)
                reason = f"손절({pct_low:+.1f}%)"

            # 2) 트레일링: +N% 도달 후 활성 → 고점 대비 -M% 하락 시 청산
            elif pct >= trailing_activate or position.get("trailing_activated"):
                position["trailing_activated"] = True
                if drop_from_high >= trailing_drop:
                    sell = True
                    sell_price = position["high_price"] * (1 - trailing_drop / 100)
                    reason = f"트레일링({pct:+.1f}%, 고점-{drop_from_high:.1f}%)"

            # 3) RSI(2) >= 80 + 수익 중
            elif today_rsi2 >= rsi_exit and pct > 0:
                sell = True
                reason = f"RSI과매수({today_rsi2:.0f})"

            # 4) 조기청산: N일 + 손실 중
            elif days_held >= early_exit_days and pct < 0:
                sell = True
                reason = f"조기청산({days_held}d, {pct:+.1f}%)"

            # 5) 시간청산
            elif days_held >= max_hold_days:
                sell = True
                reason = f"시간청산({days_held}d)"

            if sell:
                actual_sell = int(sell_price * (1 - slippage))
                qty = position["qty"]
                comm = int(actual_sell * qty * commission)
                revenue = actual_sell * qty - comm
                pnl = (actual_sell - position["buy_price"]) * qty - comm
                cash += revenue
                trades.append({
                    "date": today_date, "side": "sell", "price": actual_sell,
                    "qty": qty, "pnl": pnl, "reason": reason,
                    "days_held": days_held,
                })
                position = None

        # ── 매수 판단 (종가 매수) ──
        if position is None:
            # 조건 1: 일봉 RSI(2) < entry (과매도)
            # 조건 2: 당일 하락
            # 조건 3: 반등 확인 (실전과 동일 조건)
            oversold = today_rsi2 < rsi_entry
            drop = today_change <= drop_threshold
            bounce = (prev_rsi2 < 10 and today_rsi2 > prev_rsi2) or today_rsi2 < 5

            if oversold and drop and bounce:
                buy_price = int(today_close * (1 + slippage))
                qty = cash // buy_price if buy_price > 0 else 0
                if qty > 0:
                    comm = int(buy_price * qty * commission)
                    cash -= (buy_price * qty + comm)
                    position = {
                        "qty": qty,
                        "buy_price": buy_price,
                        "entry_day": i,
                        "high_price": buy_price,
                        "trailing_activated": False,
                    }
                    trades.append({
                        "date": today_date, "side": "buy", "price": buy_price,
                        "qty": qty, "pnl": 0,
                        "reason": f"RSI2={today_rsi2:.0f}(전일{prev_rsi2:.0f}),등락={today_change:+.1f}%",
                    })

        # 에쿼티
        equity = cash
        if position is not None:
            equity += position["qty"] * today_close
        equity_curve.append((today_date, equity))

    # 최종 청산
    if position is not None:
        last_close = float(df.iloc[-1]["close"])
        sell_price = int(last_close * (1 - slippage))
        qty = position["qty"]
        comm = int(sell_price * qty * commission)
        pnl = (sell_price - position["buy_price"]) * qty - comm
        cash += sell_price * qty - comm
        trades.append({
            "date": str(df.iloc[-1].get("datetime", "last")),
            "side": "sell", "price": sell_price, "qty": qty, "pnl": pnl,
            "reason": "기간종료", "days_held": 0,
        })

    # 통계
    sell_trades = [t for t in trades if t["side"] == "sell"]
    wins = [t for t in sell_trades if t["pnl"] > 0]
    losses = [t for t in sell_trades if t["pnl"] <= 0]

    total_return = (cash - capital) / capital * 100
    win_rate = len(wins) / len(sell_trades) * 100 if sell_trades else 0
    gross_profit = sum(t["pnl"] for t in wins) if wins else 0
    gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 1
    pf = gross_profit / gross_loss if gross_loss > 0 else 0
    avg_hold = np.mean([t.get("days_held", 0) for t in sell_trades]) if sell_trades else 0
    avg_win = gross_profit / len(wins) if wins else 0
    avg_loss = -gross_loss / len(losses) if losses else 0

    # MDD
    peak = capital
    max_dd = 0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Sharpe
    sharpe = 0.0
    if len(equity_curve) > 1:
        daily_ret = []
        for j in range(1, len(equity_curve)):
            p = equity_curve[j-1][1]
            c = equity_curve[j][1]
            if p > 0:
                daily_ret.append((c - p) / p)
        if daily_ret:
            arr = np.array(daily_ret)
            if arr.std() > 0:
                sharpe = (arr.mean() / arr.std()) * np.sqrt(252)

    # 실질 손익비
    actual_ratio = (avg_win / abs(avg_loss)) if avg_loss != 0 else 0

    return {
        "total_return_pct": round(total_return, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(pf, 2),
        "sharpe_ratio": round(sharpe, 2),
        "total_trades": len(sell_trades),
        "wins": len(wins),
        "losses": len(losses),
        "avg_hold_days": round(avg_hold, 1),
        "avg_win": round(avg_win),
        "avg_loss": round(avg_loss),
        "actual_ratio": round(actual_ratio, 2),
        "final_capital": int(cash),
        "trades": trades,
    }


def download(ticker, **kwargs):
    raw = yf.download(ticker, auto_adjust=True, progress=False, **kwargs)
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
        "069500.KS": "KODEX200",
        "229200.KS": "KODEX코스닥150",
    }

    print("=" * 65)
    print("  위기장 평균회귀 v2 백테스트")
    print("  일봉 RSI(2) + 반등확인 + 트레일링 + 손절-2%")
    print("=" * 65)

    for label, kwargs in [
        ("2020 코로나", {"start": "2019-01-01", "end": "2020-12-31"}),
        ("2022 하락장", {"start": "2021-01-01", "end": "2022-12-31"}),
        ("2025~현재 (이란 전쟁)", {"start": "2024-01-01", "end": "2026-04-03"}),
        ("5년 전체", {"period": "5y"}),
    ]:
        print(f"\n--- {label} ---")
        for yf_t, name in tickers.items():
            df = download(yf_t, **kwargs)
            if len(df) < 30:
                print(f"  {name}: 데이터 부족")
                continue

            stats = backtest_crisis_meanrev(df, capital=700_000)
            bnh = 0
            if len(df) > 5:
                fp = float(df.iloc[5]["close"])
                lp = float(df.iloc[-1]["close"])
                bnh = (lp - fp) / fp * 100

            m = ">" if stats["total_return_pct"] > 0 else " "
            print(
                f" {m} {name:14s} "
                f"전략{stats['total_return_pct']:>+7.1f}% "
                f"BnH{bnh:>+7.1f}% "
                f"{stats['total_trades']:>3}거래 "
                f"승률{stats['win_rate_pct']:>4.0f}% "
                f"PF{stats['profit_factor']:>4.1f} "
                f"MDD{stats['max_drawdown_pct']:>5.1f}% "
                f"Sharpe{stats['sharpe_ratio']:>5.2f} "
                f"손익비{stats['actual_ratio']:>4.1f} "
                f"평균{stats['avg_hold_days']:.0f}일 "
                f"최종{stats['final_capital']:>+,}"
            )

            # 최근 거래 상세
            recent = [t for t in stats["trades"] if t["side"] == "sell"][-3:]
            for t in recent:
                print(f"      {t['date']} {t['reason']:20s} pnl={t['pnl']:>+,} ({t['days_held']}일)")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    main()
