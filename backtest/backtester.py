"""간단한 백테스트 프레임워크"""
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from analysis.indicators import TechnicalIndicators
from alerts.signal_detector import detect, detect_daily, SignalType, SignalStrength

ROOT = Path(__file__).parent.parent


class Backtester:
    """단일 종목 백테스트 실행기"""

    def __init__(
        self,
        initial_capital: int = 1_000_000,
        stoploss_pct: float = 2.0,
        trailing_activate_pct: float = 2.0,
        trailing_stop_pct: float = 1.0,
        commission_rate: float = 0.00015,  # 편도 0.015%
        tax_rate: float = 0.0018,          # 매도 세금 0.18%
        max_slots: int = 3,
        strong_threshold: int = 4,  # STRONG 판정 최소 점수
        use_daily: bool = True,     # True: detect_daily, False: detect
    ):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.stoploss_pct = stoploss_pct
        self.trailing_activate_pct = trailing_activate_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.commission_rate = commission_rate
        self.tax_rate = tax_rate
        self.max_slots = max_slots
        self.strong_threshold = strong_threshold
        self.use_daily = use_daily

        self.positions = {}  # {ticker: {qty, buy_price, high_price, trailing_activated}}
        self.trades = []     # [{ticker, side, price, qty, pnl, date, reason}]
        self.equity_curve = []  # [(date, equity)]

    def run(self, ticker: str, df: pd.DataFrame) -> dict:
        """
        단일 종목 백테스트 실행.

        df: OHLCV DataFrame (datetime, open, high, low, close, volume)
        Returns: {total_return, max_drawdown, win_rate, profit_factor, trades, equity_curve}
        """
        indicators = TechnicalIndicators()
        df_ind = indicators.get_all_indicators(df)

        for i in range(60, len(df_ind)):  # 60봉 이후부터 (지표 안정화)
            current = df_ind.iloc[i]
            lookback = df_ind.iloc[max(0, i-120):i+1]

            current_price = int(current["close"])
            current_date = str(current.get("datetime", i))

            # 1) 보유 포지션 손절/트레일링 체크
            self._check_positions(ticker, current_price, current_date)

            # 2) 신호 감지 (일봉 or 5분봉)
            if self.use_daily:
                signal = detect_daily(lookback)
            else:
                signal = detect(lookback)

            # 3) 매수 판단 (strong_threshold 기반)
            is_strong = abs(signal.score) >= self.strong_threshold
            if (signal.signal_type == SignalType.BUY
                    and is_strong
                    and ticker not in self.positions
                    and len(self.positions) < self.max_slots):
                self._buy(ticker, current_price, current_date, signal.reasons)

            # 4) 신호 기반 매도
            elif (signal.signal_type == SignalType.SELL
                    and abs(signal.score) >= 3
                    and ticker in self.positions):
                self._sell(ticker, current_price, current_date, f"신호매도(score={signal.score})")

            # 5) 에쿼티 기록
            equity = self.capital
            for t, pos in self.positions.items():
                equity += pos["qty"] * current_price
            self.equity_curve.append((current_date, equity))

        return self._calc_stats()

    def _buy(self, ticker, price, date, reasons):
        slots_free = self.max_slots - len(self.positions)
        if slots_free <= 0:
            return
        amount = self.capital // slots_free
        qty = amount // price
        if qty <= 0:
            return
        cost = price * qty
        commission = int(cost * self.commission_rate)
        self.capital -= (cost + commission)
        self.positions[ticker] = {
            "qty": qty,
            "buy_price": price,
            "high_price": price,
            "trailing_activated": False,
        }
        self.trades.append({
            "ticker": ticker, "side": "buy", "price": price,
            "qty": qty, "date": date, "reason": ", ".join(reasons[:2]),
            "pnl": 0,
        })

    def _sell(self, ticker, price, date, reason):
        if ticker not in self.positions:
            return
        pos = self.positions[ticker]
        qty = pos["qty"]
        revenue = price * qty
        commission = int(revenue * self.commission_rate)
        tax = int(revenue * self.tax_rate)
        self.capital += (revenue - commission - tax)
        pnl = (price - pos["buy_price"]) * qty - commission - tax
        self.trades.append({
            "ticker": ticker, "side": "sell", "price": price,
            "qty": qty, "date": date, "reason": reason,
            "pnl": pnl,
        })
        del self.positions[ticker]

    def _check_positions(self, ticker, current_price, date):
        if ticker not in self.positions:
            return
        pos = self.positions[ticker]
        buy_price = pos["buy_price"]

        # high_price 갱신
        if current_price > pos["high_price"]:
            pos["high_price"] = current_price

        pct = (current_price - buy_price) / buy_price * 100
        drop = (pos["high_price"] - current_price) / pos["high_price"] * 100

        # 트레일링 활성화
        if not pos["trailing_activated"] and pct >= self.trailing_activate_pct:
            pos["trailing_activated"] = True

        # 손절
        if pct <= -self.stoploss_pct:
            self._sell(ticker, current_price, date, f"손절({pct:.1f}%)")
        # 트레일링 스탑
        elif pos["trailing_activated"] and drop >= self.trailing_stop_pct:
            self._sell(ticker, current_price, date, f"트레일링({drop:.1f}%)")

    def _calc_stats(self) -> dict:
        sell_trades = [t for t in self.trades if t["side"] == "sell"]
        if not sell_trades:
            return {"total_return": 0, "trades": 0}

        wins = [t for t in sell_trades if t["pnl"] > 0]
        losses = [t for t in sell_trades if t["pnl"] <= 0]

        total_return = (self.capital - self.initial_capital) / self.initial_capital * 100
        win_rate = len(wins) / len(sell_trades) * 100 if sell_trades else 0

        gross_profit = sum(t["pnl"] for t in wins) if wins else 0
        gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 1
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # MDD
        peak = self.initial_capital
        max_dd = 0
        for _, eq in self.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100
            if dd > max_dd:
                max_dd = dd

        return {
            "total_return_pct": round(total_return, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "win_rate_pct": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2),
            "total_trades": len(sell_trades),
            "wins": len(wins),
            "losses": len(losses),
            "avg_win": round(gross_profit / len(wins)) if wins else 0,
            "avg_loss": round(-gross_loss / len(losses)) if losses else 0,
            "gross_profit": gross_profit,
            "gross_loss": -gross_loss,
        }

    def print_report(self, stats: dict):
        print("=" * 50)
        print("백테스트 결과")
        print("=" * 50)
        print(f"총 수익률:    {stats.get('total_return_pct', 0):+.2f}%")
        print(f"최대 낙폭:    {stats.get('max_drawdown_pct', 0):.2f}%")
        print(f"승률:         {stats.get('win_rate_pct', 0):.1f}%")
        print(f"Profit Factor: {stats.get('profit_factor', 0):.2f}")
        print(f"총 거래:      {stats.get('total_trades', 0)}회")
        print(f"승/패:        {stats.get('wins', 0)}/{stats.get('losses', 0)}")
        print(f"평균 수익:    {stats.get('avg_win', 0):,}원")
        print(f"평균 손실:    {stats.get('avg_loss', 0):,}원")
        print("=" * 50)
