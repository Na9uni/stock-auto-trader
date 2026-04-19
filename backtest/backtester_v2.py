"""통합 백테스터 v2 — 실전과 동일한 비용/청산 모델.

변동성 돌파 + 합산 전략을 동일 프레임워크에서 테스트한다.
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


class BacktesterV2:
    """통합 백테스트 엔진."""

    def __init__(self, config: TradingConfig | None = None):
        self._config = config or TradingConfig.from_env()
        self._cost = CostModel(self._config)
        self._exit = ExitManager(self._config)
        self._indicators = TechnicalIndicators()

    def run_vb(
        self,
        ticker: str,
        df: pd.DataFrame,
        use_high_point_filters: bool = False,
        filter_ma10_deviation_max: float = 0.03,
        filter_volume_ratio_min: float = 1.2,
        filter_rsi_max: float = 70.0,
    ) -> dict:
        """변동성 돌파 전략 백테스트.

        실전과 동일한 비용 모델 적용:
        - 매수: 목표가 + 보호마진
        - 매도: 익일 시가 - 보호마진 OR 장중 손절/트레일링

        Args:
            use_high_point_filters: True면 고점 매수 회피 필터 적용
            filter_ma10_deviation_max: MA10 이격도 허용 최대치 (기본 3%)
            filter_volume_ratio_min: 전일 거래량 vs 5일 평균 최소 배수 (기본 1.2)
            filter_rsi_max: 전일 RSI 허용 최대치 (기본 70, 과매수 차단)
        """
        df = df.copy().reset_index(drop=True)
        df["range"] = df["high"] - df["low"]
        df["ma10"] = df["close"].rolling(10).mean()
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean()
        df["vol_avg5"] = df["volume"].rolling(5).mean()

        # RSI 계산 (과매수 트레일링용)
        df_ind = self._indicators.get_all_indicators(df)

        k = self._config.vb_k if self._config.is_etf(ticker) else self._config.vb_k_individual
        capital = 1_000_000
        cash = capital
        position = None  # {qty, buy_price, high_price, trailing_activated, partial_sold}
        trades = []
        equity_curve = []

        for i in range(max(11, 61), len(df)):  # MA60 워밍업 대기
            prev = df.iloc[i - 1]
            today = df.iloc[i]
            today_date = str(today.get("datetime", i))
            today_open = int(today["open"])
            today_high = int(today["high"])
            today_low = int(today["low"])
            today_close = int(today["close"])
            prev_range = int(prev["range"])
            prev_ma10 = float(prev["ma10"]) if not pd.isna(prev["ma10"]) else 0

            target = today_open + int(prev_range * k)

            # RSI + ATR
            rsi = 50.0
            atr = 0.0
            if i < len(df_ind):
                r = df_ind.iloc[i].get("rsi", 50.0)
                if not pd.isna(r):
                    rsi = float(r)
                a = df_ind.iloc[i].get("atr", 0.0)
                if not pd.isna(a):
                    atr = float(a)

            # ── 보유 중: 청산 판단 ──
            if position is not None:
                exit_actions, new_trail, new_partial = self._exit.check(
                    buy_price=position["buy_price"],
                    qty=position["qty"],
                    high_price=position["high_price"],
                    current_low=today_low,
                    current_high=today_high,
                    current_close=today_close,
                    rsi=rsi,
                    trailing_activated=position["trailing_activated"],
                    partial_sold=position["partial_sold"],
                    atr=atr,
                )
                position["trailing_activated"] = new_trail
                position["partial_sold"] = new_partial
                if today_high > position["high_price"]:
                    position["high_price"] = today_high

                sold_all = False
                for action in exit_actions:
                    if action.reason == ExitReason.PARTIAL_TAKE_PROFIT:
                        # 분할 익절
                        sell_price = self._cost.sell_execution_price(action.price, today_close, ticker)
                        comm, tax = self._cost.sell_cost(sell_price, action.qty, ticker)
                        revenue = sell_price * action.qty - comm - tax
                        # 매수 수수료 비례 배분 + 잔여분 감소
                        buy_comm_portion = int(position.get("buy_comm", 0) * action.qty / position["qty"])
                        pnl = (sell_price - position["buy_price"]) * action.qty - comm - tax - buy_comm_portion
                        position["buy_comm"] = position.get("buy_comm", 0) - buy_comm_portion
                        cash += revenue
                        position["qty"] -= action.qty
                        trades.append({
                            "date": today_date, "side": "sell", "price": sell_price,
                            "qty": action.qty, "pnl": pnl,
                            "reason": f"분할익절({action.pct:+.1f}%)",
                        })
                    else:
                        # 전량 매도 (손절/트레일링/과매수)
                        sell_qty = position["qty"]
                        sell_price = self._cost.sell_execution_price(action.price, action.price, ticker)
                        comm, tax = self._cost.sell_cost(sell_price, sell_qty, ticker)
                        revenue = sell_price * sell_qty - comm - tax
                        buy_comm_remaining = position.get("buy_comm", 0)
                        pnl = (sell_price - position["buy_price"]) * sell_qty - comm - tax - buy_comm_remaining
                        cash += revenue
                        trades.append({
                            "date": today_date, "side": "sell", "price": sell_price,
                            "qty": sell_qty, "pnl": pnl,
                            "reason": f"{action.reason.value}({action.pct:+.1f}%)",
                        })
                        position = None
                        sold_all = True
                        break

                # 손절/트레일링으로 전량 매도된 경우 → 매수 로직 스킵
                if sold_all:
                    equity = cash
                    equity_curve.append((today_date, equity))
                    continue

                # 트레일링 스탑 전용: 익일 시가 매도 안 함 (보유 유지)
                # 손절/트레일링으로만 청산 → 거래 빈도 대폭 감소

            # ── 매수 판단 ──
            if position is None:
                # 시장 레짐 필터: MA20 > MA60 (상승장만 진입)
                prev_ma20 = float(prev.get("ma20", 0)) if not pd.isna(prev.get("ma20", 0)) else 0
                prev_ma60 = float(prev.get("ma60", 0)) if not pd.isna(prev.get("ma60", 0)) else 0
                if prev_ma20 > 0 and prev_ma60 > 0 and prev_ma20 < prev_ma60:
                    equity_curve.append((today_date, cash))
                    continue
                # 마켓 필터
                if prev_ma10 <= 0 or today_open <= prev_ma10:
                    equity_curve.append((today_date, cash))
                    continue
                # 변동성 필터
                if prev_range < today_open * 0.005:
                    equity_curve.append((today_date, cash))
                    continue

                # ── 고점 매수 회피 필터 (옵션, 임계값 파라미터화) ──
                if use_high_point_filters:
                    # 1. MA10 이격도
                    if prev_ma10 > 0 and (today_open - prev_ma10) / prev_ma10 > filter_ma10_deviation_max:
                        equity_curve.append((today_date, cash))
                        continue
                    # 2. 돌파 거래량
                    prev_vol = float(prev.get("volume", 0)) if not pd.isna(prev.get("volume", 0)) else 0
                    prev_vol_avg5 = float(prev.get("vol_avg5", 0)) if not pd.isna(prev.get("vol_avg5", 0)) else 0
                    if prev_vol_avg5 > 0 and prev_vol < prev_vol_avg5 * filter_volume_ratio_min:
                        equity_curve.append((today_date, cash))
                        continue
                    # 3. RSI 과매수 차단
                    prev_rsi = 50.0
                    if i - 1 < len(df_ind):
                        r = df_ind.iloc[i - 1].get("rsi", 50.0)
                        if not pd.isna(r):
                            prev_rsi = float(r)
                    if prev_rsi >= filter_rsi_max:
                        equity_curve.append((today_date, cash))
                        continue

                # 목표가 돌파
                if today_high >= target:
                    # 갭업 시 시가가 목표가 위면 시가 기준 체결 (목표가 이하 매수 불가)
                    fill_base = max(target, today_open)
                    buy_price = self._cost.buy_execution_price(fill_base, fill_base, ticker)
                    # 수수료를 미리 감안하여 수량 산정 (현금 음수 방지)
                    cost_per_share = buy_price * (1 + self._config.commission_rate)
                    qty = int(cash // cost_per_share)
                    if qty > 0:
                        buy_comm = self._cost.buy_cost(buy_price, qty)
                        cash -= (buy_price * qty + buy_comm)
                        position = {
                            "qty": qty,
                            "buy_price": buy_price,
                            "buy_comm": buy_comm,  # PnL 계산 시 차감용
                            "high_price": today_high,
                            "trailing_activated": False,
                            "partial_sold": False,
                        }
                        trades.append({
                            "date": today_date, "side": "buy", "price": buy_price,
                            "qty": qty, "pnl": 0, "reason": "돌파매수",
                        })

            # 에쿼티 기록
            equity = cash
            if position is not None:
                equity += position["qty"] * today_close
            equity_curve.append((today_date, equity))

        # 마지막 보유분 청산
        if position is not None:
            last_close = int(df.iloc[-1]["close"])
            sell_price = self._cost.sell_execution_price(last_close, last_close, ticker)
            sell_qty = position["qty"]
            comm, tax = self._cost.sell_cost(sell_price, sell_qty, ticker)
            revenue = sell_price * sell_qty - comm - tax
            pnl = (sell_price - position["buy_price"]) * sell_qty - comm - tax - position.get("buy_comm", 0)
            cash += revenue
            trades.append({
                "date": str(df.iloc[-1].get("datetime", "last")),
                "side": "sell", "price": sell_price,
                "qty": sell_qty, "pnl": pnl, "reason": "기간종료",
            })
            # 청산 후 에쿼티 반영 (MDD/Sharpe가 최종 상태를 포함하도록)
            equity_curve.append((str(df.iloc[-1].get("datetime", "last")), cash))

        return self._calc_stats(capital, cash, trades, equity_curve)

    def _calc_stats(self, capital, cash, trades, equity_curve) -> dict:
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
        max_dd = 0
        for _, eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100
            if dd > max_dd:
                max_dd = dd

        # MDD 회복 기간 (거래일)
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

        # Sharpe ratio (일별)
        daily_returns = []
        for i in range(1, len(equity_curve)):
            prev_eq = equity_curve[i - 1][1]
            curr_eq = equity_curve[i][1]
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

        # 거래당 평균 비용률 추정
        avg_cost_pct = 0.0
        buy_trades = [t for t in trades if t["side"] == "buy"]
        if buy_trades:
            total_buy_amount = sum(t["price"] * t["qty"] for t in buy_trades)
            if total_buy_amount > 0:
                # 추정: 보호마진 + 수수료 + 세금
                avg_cost_pct = (gross_loss + gross_profit - (cash - capital)) / total_buy_amount * 100

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

    def print_report(self, stats: dict, ticker_name: str = ""):
        """백테스트 결과 출력."""
        print("=" * 55)
        if ticker_name:
            print(f"  {ticker_name} — 백테스트 v2 결과")
        else:
            print("  백테스트 v2 결과")
        print("=" * 55)
        print(f"총 수익률:        {stats.get('total_return_pct', 0):+.2f}%")
        print(f"최대 낙폭(MDD):   {stats.get('max_drawdown_pct', 0):.2f}%")
        print(f"MDD 회복 기간:    {stats.get('max_drawdown_recovery_days', 0)}거래일")
        print(f"승률:             {stats.get('win_rate_pct', 0):.1f}%")
        print(f"Profit Factor:    {stats.get('profit_factor', 0):.2f}")
        print(f"Sharpe Ratio:     {stats.get('sharpe_ratio', 0):.2f}")
        print(f"총 거래:          {stats.get('total_trades', 0)}회")
        print(f"승/패:            {stats.get('wins', 0)}/{stats.get('losses', 0)}")
        print(f"평균 수익:        {stats.get('avg_win', 0):,}원")
        print(f"평균 손실:        {stats.get('avg_loss', 0):,}원")
        print(f"1회 최대 손실:    {stats.get('max_single_loss', 0):,}원 ({stats.get('max_single_loss_pct', 0):.1f}%)")
        print(f"최종 자본:        {stats.get('final_capital', 0):,}원")
        print("=" * 55)

        # 소자본 경고
        warnings = []
        if stats.get("max_single_loss_pct", 0) < -5:
            warnings.append(f"[경고] 1회 최대 손실 {stats['max_single_loss_pct']:.1f}% → 포지션 사이즈 축소 권장")
        if stats.get("max_drawdown_pct", 0) > 15:
            warnings.append(f"[경고] MDD {stats['max_drawdown_pct']:.1f}% > 15% → 심리적 지속 가능성 낮음")
        if stats.get("max_drawdown_recovery_days", 0) > 40:
            warnings.append(f"[경고] MDD 회복 {stats['max_drawdown_recovery_days']}일 → 2개월 초과")
        if stats.get("total_trades", 0) < 20:
            warnings.append(f"[경고] 거래 {stats['total_trades']}회 → 통계적 유의성 부족 (최소 20회)")
        if stats.get("sharpe_ratio", 0) < 1.0:
            warnings.append(f"[경고] Sharpe {stats['sharpe_ratio']:.2f} < 1.0 → 위험 대비 수익 부족")

        if warnings:
            print()
            for w in warnings:
                print(w)


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


def main():
    """변동성 돌파 전략 백테스트 실행 (비용 현실화)."""
    config = TradingConfig.from_env()
    bt = BacktesterV2(config)

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
        print(f"\n{'='*60}")
        print(f"  {label} — 변동성 돌파 v2 (비용 현실화)")
        print(f"  손절={config.stoploss_pct}% | 트레일링={config.trailing_activate_pct}%→{config.trailing_stop_pct}%")
        print(f"{'='*60}")

        total_pnl = 0
        for yf_ticker, name in tickers.items():
            df = download(yf_ticker, **period_args)
            if len(df) < 30:
                print(f"  {name}: 데이터 부족")
                continue

            # ticker 코드 추출
            code = yf_ticker.split(".")[0]
            stats = bt.run_vb(code, df)

            # 바이앤홀드 비교
            first_p = int(df.iloc[11]["close"])
            last_p = int(df.iloc[-1]["close"])
            bnh = (last_p - first_p) / first_p * 100

            total_pnl += stats["final_capital"] - 1_000_000
            marker = ">" if stats["total_return_pct"] > 0 else " "
            cost_pct = CostModel(config).roundtrip_cost_pct(first_p, code)

            print(
                f" {marker} {name:12s} "
                f"전략{stats['total_return_pct']:>+7.1f}% "
                f"BnH{bnh:>+7.1f}% "
                f"{stats['total_trades']:>3}거래 "
                f"승률{stats['win_rate_pct']:>4.0f}% "
                f"PF{stats['profit_factor']:>4.1f} "
                f"MDD{stats['max_drawdown_pct']:>5.1f}% "
                f"Sharpe{stats['sharpe_ratio']:>5.2f} "
                f"비용{cost_pct:.1f}%"
            )

        print(f"\n  합산 전략 손익: {total_pnl:>+,}원")


if __name__ == "__main__":
    main()
