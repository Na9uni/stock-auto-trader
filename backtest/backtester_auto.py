"""AUTO 통합 백테스터 — VB + 추세추종 + 레짐 전환 + 거부권 전체 시뮬레이션.

backtester_v2.py 가 VB 단독 테스트인 반면,
이 모듈은 auto_strategy.py 의 전체 로직을 재현한다:

1. 레짐 판단: MA20 vs MA60 (상승/하락)
2. 상승장 → 변동성 돌파(VB) + 거부권(score <= -3 시 매수 차단)
3. 하락장 → 추세추종(골든/데드크로스)
4. VB 포지션은 EOD 강제 청산, 추세 포지션은 데드크로스까지 보유
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import yfinance as yf

from analysis.indicators import TechnicalIndicators
from backtest.cost_model import CostModel
from backtest.exit_manager import ExitManager, ExitReason
from config.trading_config import TradingConfig


# 거부권 기준 (score_strategy.py 와 동일)
VETO_THRESHOLD = -3


class BacktesterAuto:
    """AUTO 통합 백테스트 엔진."""

    def __init__(self, config: TradingConfig | None = None):
        self._config = config or TradingConfig.from_env()
        self._cost = CostModel(self._config)
        self._exit = ExitManager(self._config)
        self._indicators = TechnicalIndicators()

    # ── 레짐 판단 ──────────────────────────────────────────
    @staticmethod
    def _regime(ma20: float, ma60: float) -> str:
        """MA20 vs MA60 으로 bull / bear 판단."""
        if ma20 <= 0 or ma60 <= 0:
            return "unknown"
        if ma20 > ma60:
            return "bull"
        return "bear"

    # ── 거부권 점수 (간이) ─────────────────────────────────
    @staticmethod
    def _score_veto(df: pd.DataFrame, idx: int) -> bool:
        """최근 20봉 기반 간이 합산 점수로 거부권 여부 판단.

        실전에서는 5분봉 기반이지만 백테스트는 일봉만 있으므로
        RSI·MACD 기반 간이 점수를 계산한다.
        score <= -3 이면 거부권 발동 (True).
        """
        if idx < 20:
            return False

        window = df.iloc[idx - 14 : idx + 1]
        close = window["close"].values.astype(float)
        if len(close) < 14:
            return False

        # RSI 계산 (14일)
        delta = np.diff(close)
        gains = np.where(delta > 0, delta, 0)
        losses = np.where(delta < 0, -delta, 0)
        avg_gain = gains.mean()
        avg_loss = losses.mean()
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - 100.0 / (1.0 + rs)

        # MACD 방향 (12-26 EMA, 간이)
        if idx >= 26:
            ema12 = float(df["close"].iloc[idx - 11 : idx + 1].ewm(span=12, adjust=False).mean().iloc[-1])
            ema26 = float(df["close"].iloc[idx - 25 : idx + 1].ewm(span=26, adjust=False).mean().iloc[-1])
            macd = ema12 - ema26
        else:
            macd = 0.0

        # 간이 점수
        score = 0
        if rsi < 30:
            score -= 3
        elif rsi < 40:
            score -= 1
        elif rsi > 70:
            score += 2

        if macd < 0:
            score -= 2
        elif macd > 0:
            score += 1

        # 최근 3일 연속 하락 감점
        if idx >= 3:
            last3 = [float(df.iloc[idx - j]["close"]) for j in range(3)]
            if last3[0] < last3[1] < last3[2]:
                score -= 2

        return score <= VETO_THRESHOLD

    # ── 골든/데드크로스 판단 ─────────────────────────────────
    @staticmethod
    def _golden_cross(df: pd.DataFrame, idx: int) -> bool:
        """2일 확인 골든크로스: MA5 가 MA20 위로 돌파."""
        if idx < 2:
            return False
        ma5_2ago = float(df.iloc[idx - 2]["ma5"])
        ma20_2ago = float(df.iloc[idx - 2]["ma20"])
        ma5_yest = float(df.iloc[idx - 1]["ma5"])
        ma20_yest = float(df.iloc[idx - 1]["ma20"])
        ma5_today = float(df.iloc[idx]["ma5"])
        ma20_today = float(df.iloc[idx]["ma20"])

        if pd.isna(ma5_2ago) or pd.isna(ma20_2ago):
            return False
        if pd.isna(ma5_yest) or pd.isna(ma20_yest):
            return False
        if pd.isna(ma5_today) or pd.isna(ma20_today):
            return False

        return (
            ma5_2ago <= ma20_2ago
            and ma5_yest > ma20_yest
            and ma5_today > ma20_today
        )

    @staticmethod
    def _dead_cross(df: pd.DataFrame, idx: int) -> bool:
        """2일 확인 데드크로스: MA5 가 MA20 아래로 돌파."""
        if idx < 2:
            return False
        ma5_2ago = float(df.iloc[idx - 2]["ma5"])
        ma20_2ago = float(df.iloc[idx - 2]["ma20"])
        ma5_yest = float(df.iloc[idx - 1]["ma5"])
        ma20_yest = float(df.iloc[idx - 1]["ma20"])
        ma5_today = float(df.iloc[idx]["ma5"])
        ma20_today = float(df.iloc[idx]["ma20"])

        if pd.isna(ma5_2ago) or pd.isna(ma20_2ago):
            return False
        if pd.isna(ma5_yest) or pd.isna(ma20_yest):
            return False
        if pd.isna(ma5_today) or pd.isna(ma20_today):
            return False

        return (
            ma5_2ago >= ma20_2ago
            and ma5_yest < ma20_yest
            and ma5_today < ma20_today
        )

    # ── 메인 백테스트 ──────────────────────────────────────
    def run(self, ticker: str, df: pd.DataFrame) -> dict:
        """AUTO 통합 백테스트 실행."""
        df = df.copy().reset_index(drop=True)

        # 이동평균 / 보조지표 계산
        df["range"] = df["high"] - df["low"]
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma10"] = df["close"].rolling(10).mean()
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean()

        # RSI·ATR (ExitManager 용)
        df_ind = self._indicators.get_all_indicators(df)

        k = self._config.vb_k if self._config.is_etf(ticker) else self._config.vb_k_individual
        capital = 1_000_000
        cash = capital

        # 포지션 상태
        vb_pos = None     # VB 포지션 (당일 매수→EOD 청산)
        trend_pos = None  # 추세 포지션 (데드크로스까지 보유)

        trades: list[dict] = []
        equity_curve: list[tuple] = []

        # 통계
        regime_days = {"bull": 0, "bear": 0, "unknown": 0}
        vb_trades = 0
        trend_trades = 0
        veto_count = 0

        start_idx = max(61, 11)  # MA60 워밍업

        for i in range(start_idx, len(df)):
            prev = df.iloc[i - 1]
            today = df.iloc[i]
            today_date = str(today.get("datetime", i))
            today_open = int(today["open"])
            today_high = int(today["high"])
            today_low = int(today["low"])
            today_close = int(today["close"])
            prev_range = int(prev["range"])

            prev_ma20 = float(prev["ma20"]) if not pd.isna(prev.get("ma20", float("nan"))) else 0
            prev_ma60 = float(prev["ma60"]) if not pd.isna(prev.get("ma60", float("nan"))) else 0
            prev_ma10 = float(prev["ma10"]) if not pd.isna(prev.get("ma10", float("nan"))) else 0

            # 레짐 판단 (전일 기준)
            regime = self._regime(prev_ma20, prev_ma60)
            regime_days[regime] += 1

            # RSI / ATR
            rsi = 50.0
            atr = 0.0
            if i < len(df_ind):
                r = df_ind.iloc[i].get("rsi", 50.0)
                if not pd.isna(r):
                    rsi = float(r)
                a = df_ind.iloc[i].get("atr", 0.0)
                if not pd.isna(a):
                    atr = float(a)

            target = today_open + int(prev_range * k)

            # ================================================================
            # VB 포지션 처리 (보유 중이면 장중 손절/트레일링 체크 → EOD 청산)
            # ================================================================
            if vb_pos is not None:
                exit_actions, new_trail, new_partial = self._exit.check(
                    buy_price=vb_pos["buy_price"],
                    qty=vb_pos["qty"],
                    high_price=vb_pos["high_price"],
                    current_low=today_low,
                    current_high=today_high,
                    current_close=today_close,
                    rsi=rsi,
                    trailing_activated=vb_pos["trailing_activated"],
                    partial_sold=vb_pos["partial_sold"],
                    atr=atr,
                )
                vb_pos["trailing_activated"] = new_trail
                vb_pos["partial_sold"] = new_partial
                if today_high > vb_pos["high_price"]:
                    vb_pos["high_price"] = today_high

                sold_all = False
                for action in exit_actions:
                    if action.reason == ExitReason.PARTIAL_TAKE_PROFIT:
                        sell_price = self._cost.sell_execution_price(action.price, today_close, ticker)
                        comm, tax = self._cost.sell_cost(sell_price, action.qty, ticker)
                        revenue = sell_price * action.qty - comm - tax
                        buy_comm_portion = int(vb_pos.get("buy_comm", 0) * action.qty / vb_pos["qty"])
                        pnl = (sell_price - vb_pos["buy_price"]) * action.qty - comm - tax - buy_comm_portion
                        vb_pos["buy_comm"] = vb_pos.get("buy_comm", 0) - buy_comm_portion
                        cash += revenue
                        vb_pos["qty"] -= action.qty
                        trades.append({
                            "date": today_date, "side": "sell", "price": sell_price,
                            "qty": action.qty, "pnl": pnl,
                            "reason": f"VB-분할익절({action.pct:+.1f}%)",
                            "strategy": "VB",
                        })
                    else:
                        sell_qty = vb_pos["qty"]
                        sell_price = self._cost.sell_execution_price(action.price, action.price, ticker)
                        comm, tax = self._cost.sell_cost(sell_price, sell_qty, ticker)
                        revenue = sell_price * sell_qty - comm - tax
                        buy_comm_remaining = vb_pos.get("buy_comm", 0)
                        pnl = (sell_price - vb_pos["buy_price"]) * sell_qty - comm - tax - buy_comm_remaining
                        cash += revenue
                        trades.append({
                            "date": today_date, "side": "sell", "price": sell_price,
                            "qty": sell_qty, "pnl": pnl,
                            "reason": f"VB-{action.reason.value}({action.pct:+.1f}%)",
                            "strategy": "VB",
                        })
                        vb_pos = None
                        sold_all = True
                        break

                # 장중 청산 안 됐으면 EOD 강제 청산
                if not sold_all and vb_pos is not None:
                    sell_qty = vb_pos["qty"]
                    sell_price = self._cost.sell_execution_price(today_close, today_close, ticker)
                    comm, tax = self._cost.sell_cost(sell_price, sell_qty, ticker)
                    revenue = sell_price * sell_qty - comm - tax
                    buy_comm_remaining = vb_pos.get("buy_comm", 0)
                    pnl = (sell_price - vb_pos["buy_price"]) * sell_qty - comm - tax - buy_comm_remaining
                    cash += revenue
                    pct = (sell_price - vb_pos["buy_price"]) / vb_pos["buy_price"] * 100
                    trades.append({
                        "date": today_date, "side": "sell", "price": sell_price,
                        "qty": sell_qty, "pnl": pnl,
                        "reason": f"VB-EOD청산({pct:+.1f}%)",
                        "strategy": "VB",
                    })
                    vb_pos = None

            # ================================================================
            # 추세 포지션 처리 (데드크로스 시 청산)
            # ================================================================
            if trend_pos is not None:
                if today_high > trend_pos["high_price"]:
                    trend_pos["high_price"] = today_high

                # 데드크로스 매도
                if self._dead_cross(df, i):
                    sell_qty = trend_pos["qty"]
                    sell_price = self._cost.sell_execution_price(today_close, today_close, ticker)
                    comm, tax = self._cost.sell_cost(sell_price, sell_qty, ticker)
                    revenue = sell_price * sell_qty - comm - tax
                    buy_comm_remaining = trend_pos.get("buy_comm", 0)
                    pnl = (sell_price - trend_pos["buy_price"]) * sell_qty - comm - tax - buy_comm_remaining
                    cash += revenue
                    pct = (sell_price - trend_pos["buy_price"]) / trend_pos["buy_price"] * 100
                    trades.append({
                        "date": today_date, "side": "sell", "price": sell_price,
                        "qty": sell_qty, "pnl": pnl,
                        "reason": f"추세-데드크로스({pct:+.1f}%)",
                        "strategy": "TREND",
                    })
                    trend_pos = None

            # ================================================================
            # 매수 판단 (포지션 없을 때)
            # ================================================================
            has_any_position = vb_pos is not None or trend_pos is not None

            if not has_any_position:
                if regime == "bull":
                    # ── 상승장: VB 매수 ──
                    if prev_ma10 <= 0 or today_open <= prev_ma10:
                        pass  # 마켓 필터 미충족
                    elif prev_range < today_open * 0.005:
                        pass  # 변동성 필터 미충족
                    elif today_high >= target:
                        # 거부권 체크
                        if self._score_veto(df, i):
                            veto_count += 1
                        else:
                            fill_base = max(target, today_open)
                            buy_price = self._cost.buy_execution_price(fill_base, fill_base, ticker)
                            cost_per_share = buy_price * (1 + self._config.commission_rate)
                            qty = int(cash // cost_per_share)
                            if qty > 0:
                                buy_comm = self._cost.buy_cost(buy_price, qty)
                                cash -= (buy_price * qty + buy_comm)
                                vb_pos = {
                                    "qty": qty,
                                    "buy_price": buy_price,
                                    "buy_comm": buy_comm,
                                    "high_price": today_high,
                                    "trailing_activated": False,
                                    "partial_sold": False,
                                }
                                trades.append({
                                    "date": today_date, "side": "buy", "price": buy_price,
                                    "qty": qty, "pnl": 0,
                                    "reason": "VB-돌파매수",
                                    "strategy": "VB",
                                })
                                vb_trades += 1

                elif regime == "bear":
                    # ── 하락장: 골든크로스 매수 ──
                    ma60_today = float(today["ma60"]) if not pd.isna(today.get("ma60", float("nan"))) else 0
                    if self._golden_cross(df, i) and today_close > ma60_today > 0:
                        buy_price = self._cost.buy_execution_price(today_close, today_close, ticker)
                        cost_per_share = buy_price * (1 + self._config.commission_rate)
                        qty = int(cash // cost_per_share)
                        if qty > 0:
                            buy_comm = self._cost.buy_cost(buy_price, qty)
                            cash -= (buy_price * qty + buy_comm)
                            trend_pos = {
                                "qty": qty,
                                "buy_price": buy_price,
                                "buy_comm": buy_comm,
                                "high_price": today_high,
                            }
                            trades.append({
                                "date": today_date, "side": "buy", "price": buy_price,
                                "qty": qty, "pnl": 0,
                                "reason": "추세-골든크로스",
                                "strategy": "TREND",
                            })
                            trend_trades += 1

            # 에쿼티 기록
            equity = cash
            if vb_pos is not None:
                equity += vb_pos["qty"] * today_close
            if trend_pos is not None:
                equity += trend_pos["qty"] * today_close
            equity_curve.append((today_date, equity))

        # ── 기간 종료: 잔여 포지션 청산 ──
        last_close = int(df.iloc[-1]["close"])
        for label, pos in [("VB", vb_pos), ("TREND", trend_pos)]:
            if pos is not None:
                sell_price = self._cost.sell_execution_price(last_close, last_close, ticker)
                sell_qty = pos["qty"]
                comm, tax = self._cost.sell_cost(sell_price, sell_qty, ticker)
                revenue = sell_price * sell_qty - comm - tax
                pnl = (sell_price - pos["buy_price"]) * sell_qty - comm - tax - pos.get("buy_comm", 0)
                cash += revenue
                pct = (sell_price - pos["buy_price"]) / pos["buy_price"] * 100
                trades.append({
                    "date": str(df.iloc[-1].get("datetime", "last")),
                    "side": "sell", "price": sell_price,
                    "qty": sell_qty, "pnl": pnl,
                    "reason": f"{label}-기간종료({pct:+.1f}%)",
                    "strategy": label,
                })
                equity_curve.append((str(df.iloc[-1].get("datetime", "last")), cash))

        stats = self._calc_stats(capital, cash, trades, equity_curve)
        stats["regime_days"] = regime_days
        stats["vb_trades"] = vb_trades
        stats["trend_trades"] = trend_trades
        stats["veto_count"] = veto_count
        return stats

    # ── 통계 계산 (backtester_v2 와 동일 로직) ─────────────
    def _calc_stats(self, capital: int, cash: float,
                    trades: list[dict], equity_curve: list[tuple]) -> dict:
        sell_trades = [t for t in trades if t["side"] == "sell"]
        if not sell_trades:
            return {
                "total_return_pct": round((cash - capital) / capital * 100, 2),
                "max_drawdown_pct": 0, "max_drawdown_recovery_days": 0,
                "win_rate_pct": 0, "profit_factor": 0, "sharpe_ratio": 0,
                "total_trades": 0, "wins": 0, "losses": 0,
                "avg_win": 0, "avg_loss": 0, "gross_profit": 0, "gross_loss": 0,
                "max_single_loss": 0, "max_single_loss_pct": 0,
                "final_capital": int(cash),
                "trades_detail": [], "equity_curve": equity_curve,
            }

        wins = [t for t in sell_trades if t["pnl"] > 0]
        losses = [t for t in sell_trades if t["pnl"] <= 0]

        total_return = (cash - capital) / capital * 100
        win_rate = len(wins) / len(sell_trades) * 100

        gross_profit = sum(t["pnl"] for t in wins) if wins else 0
        gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 0
        pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0)

        # MDD
        peak = capital
        max_dd = 0.0
        for _, eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100
            if dd > max_dd:
                max_dd = dd

        # MDD 회복 기간
        peak_val = capital
        dd_start = 0
        max_recovery = 0
        in_drawdown = False
        for idx, (_, eq) in enumerate(equity_curve):
            if eq >= peak_val:
                if in_drawdown:
                    recovery_days = idx - dd_start
                    max_recovery = max(max_recovery, recovery_days)
                    in_drawdown = False
                peak_val = eq
            else:
                if not in_drawdown:
                    dd_start = idx
                    in_drawdown = True

        # Sharpe ratio
        daily_returns: list[float] = []
        for j in range(1, len(equity_curve)):
            prev_eq = equity_curve[j - 1][1]
            curr_eq = equity_curve[j][1]
            if prev_eq > 0:
                daily_returns.append((curr_eq - prev_eq) / prev_eq)
        sharpe = 0.0
        if daily_returns:
            arr = np.array(daily_returns)
            if arr.std() > 0:
                sharpe = (arr.mean() / arr.std()) * np.sqrt(252)

        # 1회 최대 손실
        max_single_loss = min((t["pnl"] for t in sell_trades), default=0)
        max_single_loss_pct = max_single_loss / capital * 100 if capital > 0 else 0

        return {
            "total_return_pct": round(total_return, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "max_drawdown_recovery_days": max_recovery,
            "win_rate_pct": round(win_rate, 1),
            "profit_factor": round(pf, 2),
            "sharpe_ratio": round(sharpe, 2),
            "total_trades": len(sell_trades),
            "wins": len(wins),
            "losses": len(losses),
            "avg_win": round(gross_profit / len(wins)) if wins else 0,
            "avg_loss": round(-gross_loss / len(losses)) if losses else 0,
            "gross_profit": gross_profit,
            "gross_loss": -gross_loss,
            "max_single_loss": max_single_loss,
            "max_single_loss_pct": round(max_single_loss_pct, 2),
            "final_capital": int(cash),
            "trades_detail": sell_trades,
            "equity_curve": equity_curve,
        }

    # ── 리포트 출력 ────────────────────────────────────────
    def print_report(self, stats: dict, ticker_name: str = "") -> None:
        """AUTO 백테스트 결과 출력."""
        print("=" * 60)
        if ticker_name:
            print(f"  {ticker_name} — AUTO 통합 백테스트 결과")
        else:
            print("  AUTO 통합 백테스트 결과")
        print("=" * 60)
        print(f"총 수익률:        {stats.get('total_return_pct', 0):+.2f}%")
        print(f"최대 낙폭(MDD):   {stats.get('max_drawdown_pct', 0):.2f}%")
        print(f"MDD 회복 기간:    {stats.get('max_drawdown_recovery_days', 0)}거래일")
        print(f"승률:             {stats.get('win_rate_pct', 0):.1f}%")
        print(f"Profit Factor:    {stats.get('profit_factor', 0):.2f}")
        print(f"Sharpe Ratio:     {stats.get('sharpe_ratio', 0):.2f}")
        print(f"총 거래:          {stats.get('total_trades', 0)}회")
        print(f"  VB 매수:        {stats.get('vb_trades', 0)}회")
        print(f"  추세 매수:      {stats.get('trend_trades', 0)}회")
        print(f"  거부권 발동:    {stats.get('veto_count', 0)}회")
        print(f"승/패:            {stats.get('wins', 0)}/{stats.get('losses', 0)}")
        print(f"평균 수익:        {stats.get('avg_win', 0):,}원")
        print(f"평균 손실:        {stats.get('avg_loss', 0):,}원")
        print(f"1회 최대 손실:    {stats.get('max_single_loss', 0):,}원 ({stats.get('max_single_loss_pct', 0):.1f}%)")
        print(f"최종 자본:        {stats.get('final_capital', 0):,}원")

        rd = stats.get("regime_days", {})
        total_days = sum(rd.values())
        if total_days > 0:
            bull_pct = rd.get("bull", 0) / total_days * 100
            bear_pct = rd.get("bear", 0) / total_days * 100
            print(f"레짐 분포:        상승 {rd.get('bull', 0)}일({bull_pct:.0f}%) / "
                  f"하락 {rd.get('bear', 0)}일({bear_pct:.0f}%)")
        print("=" * 60)


def download(ticker: str, period: str = "1y",
             start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """yfinance 일봉 다운로드."""
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


def main() -> None:
    """AUTO 통합 백테스트 실행."""
    config = TradingConfig.from_env()
    bt = BacktesterAuto(config)

    tickers = {
        "069500.KS": "KODEX200",
        "229200.KS": "KODEX코스닥150",
        "133690.KS": "TIGER나스닥100",
        "005930.KS": "삼성전자",
        "105560.KS": "KB금융",
        "034020.KS": "두산에너빌",
        "103590.KS": "일진전기",
        "006910.KS": "보성파워텍",
    }

    for label, period_args in [
        ("최근 1년", {"period": "1y"}),
        ("2022 하락장", {"start": "2022-01-01", "end": "2022-12-31"}),
    ]:
        print(f"\n{'=' * 65}")
        print(f"  {label} — AUTO 통합 (VB + 추세추종 + 거부권 + 레짐 전환)")
        print(f"  손절={config.stoploss_pct}% | 트레일링={config.trailing_activate_pct}%"
              f"→{config.trailing_stop_pct}% | VB-K={config.vb_k}/{config.vb_k_individual}")
        print(f"{'=' * 65}")

        total_pnl = 0
        total_vb = 0
        total_trend = 0
        total_veto = 0
        combined_bull = 0
        combined_bear = 0

        for yf_ticker, name in tickers.items():
            df = download(yf_ticker, **period_args)
            if len(df) < 62:
                print(f"  {name}: 데이터 부족")
                continue

            code = yf_ticker.split(".")[0]
            stats = bt.run(code, df)

            # 바이앤홀드 비교
            first_p = int(df.iloc[61]["close"])
            last_p = int(df.iloc[-1]["close"])
            bnh = (last_p - first_p) / first_p * 100

            pnl = stats["final_capital"] - 1_000_000
            total_pnl += pnl
            total_vb += stats.get("vb_trades", 0)
            total_trend += stats.get("trend_trades", 0)
            total_veto += stats.get("veto_count", 0)
            rd = stats.get("regime_days", {})
            combined_bull += rd.get("bull", 0)
            combined_bear += rd.get("bear", 0)

            marker = ">" if stats["total_return_pct"] > 0 else " "
            cost_pct = CostModel(config).roundtrip_cost_pct(first_p, code)

            print(
                f" {marker} {name:12s} "
                f"전략{stats['total_return_pct']:>+7.1f}% "
                f"BnH{bnh:>+7.1f}% "
                f"{stats['total_trades']:>3}거래 "
                f"(VB{stats.get('vb_trades', 0):>2}/추세{stats.get('trend_trades', 0):>2}/거부{stats.get('veto_count', 0):>2}) "
                f"승률{stats['win_rate_pct']:>4.0f}% "
                f"MDD{stats['max_drawdown_pct']:>5.1f}% "
                f"Sharpe{stats['sharpe_ratio']:>5.2f}"
            )

        total_days = combined_bull + combined_bear
        bull_pct = combined_bull / total_days * 100 if total_days > 0 else 0
        bear_pct = combined_bear / total_days * 100 if total_days > 0 else 0
        print(f"\n  합산 손익: {total_pnl:>+,}원")
        print(f"  VB 매수: {total_vb}회 | 추세 매수: {total_trend}회 | 거부권: {total_veto}회")
        print(f"  레짐 분포 (평균): 상승 {bull_pct:.0f}% / 하락 {bear_pct:.0f}%")


if __name__ == "__main__":
    main()
