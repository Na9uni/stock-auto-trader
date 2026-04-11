"""AUTO 통합 백테스터 — VB + 추세추종 + 위기MR + 4-모드 레짐 + 거부권 전체 시뮬레이션.

backtester_v2.py 가 VB 단독 테스트인 반면,
이 모듈은 auto_strategy.py 의 전체 로직을 재현한다:

1. 레짐 판단:
   - 1차: 지수(KOSPI proxy) 기반 4-모드 레짐 (NORMAL/SWING/DEFENSE/CASH)
   - 2차: 종목별 MA20/MA60 bull/bear 판단
2. 상승장(NORMAL+bull) → 변동성 돌파(VB) + 거부권(score <= -3 시 매수 차단)
3. 하락장(bear) → 추세추종(골든/데드크로스) + 위기 평균회귀(ETF, RSI2<15)
4. DEFENSE/CASH → 신규 매수 차단
5. VB 포지션은 EOD 강제 청산, 추세 포지션은 데드크로스까지 보유
6. 위기MR 포지션은 RSI2>=80 / 트레일링 / 손절 / 시간청산(2일)
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


# ── 4-모드 레짐 시뮬레이션 ─────────────────────────────────────
class _RegimeSimulator:
    """백테스트 전용 레짐 시뮬레이터.

    실전 regime_engine.py 의 판별 로직을 일봉 지수 데이터로 재현.
    지수 일봉의 일일 등락률 + 누적 하락 + ATR 급팽창을 사용.

    RegimeState: NORMAL / SWING / DEFENSE / CASH
    """

    NORMAL = "NORMAL"
    SWING = "SWING"
    DEFENSE = "DEFENSE"
    CASH = "CASH"

    _SEVERITY = {
        "NORMAL": 0,
        "SWING": 1,
        "DEFENSE": 2,
        "CASH": 3,
    }

    def __init__(
        self,
        defense_trigger_pct: float = -2.0,
        cash_trigger_pct: float = -3.0,
        swing_trigger_pct: float = -1.5,
        cumul_defense_pct: float = -8.0,
        cumul_swing_pct: float = -5.0,
        cooldown_days: int = 2,
    ):
        self._defense_trigger = defense_trigger_pct
        self._cash_trigger = cash_trigger_pct
        self._swing_trigger = swing_trigger_pct
        self._cumul_defense = cumul_defense_pct
        self._cumul_swing = cumul_swing_pct
        self._cooldown_days = cooldown_days

        self._state = self.NORMAL
        self._defense_price: float | None = None
        self._recent_changes: list[float] = []
        self._cooldown_remaining = 0

    @property
    def state(self) -> str:
        return self._state

    def detect(
        self,
        index_change_pct: float,
        index_close: float,
        atr_ratio: float = 1.0,
    ) -> str:
        """일봉 1개를 먹고 레짐 갱신.

        Args:
            index_change_pct: 지수 일일 등락률 (%)
            index_close: 지수 종가
            atr_ratio: 당일 range / 10일 평균 range

        Returns:
            현재 레짐 문자열
        """
        # 누적 추적
        self._recent_changes.append(index_change_pct)
        self._recent_changes = self._recent_changes[-5:]

        cumul_5d = sum(self._recent_changes) if len(self._recent_changes) >= 3 else 0

        new_state = self.NORMAL
        # -- CASH 조건 --
        if (
            self._state == self.DEFENSE
            and self._defense_price is not None
            and index_close > 0
        ):
            drop_from_defense = (
                (index_close - self._defense_price) / self._defense_price * 100
            )
            if drop_from_defense <= self._cash_trigger:
                new_state = self.CASH

        # -- 누적 하락 --
        if cumul_5d <= self._cumul_defense and new_state == self.NORMAL:
            new_state = self.DEFENSE
        elif cumul_5d <= self._cumul_swing and new_state == self.NORMAL:
            new_state = self.SWING

        # -- 당일 급락 --
        if new_state == self.NORMAL:
            if index_change_pct <= self._defense_trigger:
                new_state = self.DEFENSE
            elif index_change_pct <= self._swing_trigger:
                new_state = self.SWING

        # -- ATR 급팽창 --
        if new_state == self.NORMAL:
            if atr_ratio >= 2.5:
                new_state = self.DEFENSE
            elif atr_ratio >= 1.8:
                new_state = self.SWING

        # -- Anti-oscillation --
        new_sev = self._SEVERITY[new_state]
        cur_sev = self._SEVERITY[self._state]

        if new_sev > cur_sev:
            # 위험 상승 -> 즉시 전환
            if new_state == self.DEFENSE and index_close > 0:
                self._defense_price = index_close
            self._state = new_state
            self._cooldown_remaining = 0
        elif new_sev < cur_sev:
            # 위험 하강 -> 쿨다운
            if self._cooldown_remaining <= 0:
                self._cooldown_remaining = self._cooldown_days
            self._cooldown_remaining -= 1
            if self._cooldown_remaining <= 0:
                if new_state != self.DEFENSE:
                    self._defense_price = None
                self._state = new_state
        # 동일 레짐이면 변경 없음 (쿨다운 카운트도 안 줄임)

        return self._state


class BacktesterAuto:
    """AUTO 통합 백테스트 엔진."""

    def __init__(self, config: TradingConfig | None = None):
        self._config = config or TradingConfig.from_env()
        self._cost = CostModel(self._config)
        self._exit = ExitManager(self._config)
        self._indicators = TechnicalIndicators()

    # ── 종목별 bull/bear 판단 ─────────────────────────────────
    @staticmethod
    def _stock_regime(ma20: float, ma60: float) -> str:
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
        RSI/MACD 기반 간이 점수를 계산한다.
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

    # ── RSI(2) 계산 ───────────────────────────────────────────
    @staticmethod
    def _rsi2(prices: pd.Series) -> pd.Series:
        """RSI(2) - Wilder EMA, period=2."""
        delta = prices.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=0.5, min_periods=2, adjust=False).mean()
        avg_loss = loss.ewm(alpha=0.5, min_periods=2, adjust=False).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        rsi[(avg_gain == 0) & (avg_loss == 0)] = 50
        return rsi

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
    def run(
        self,
        ticker: str,
        df: pd.DataFrame,
        index_df: pd.DataFrame | None = None,
        is_etf: bool = False,
    ) -> dict:
        """AUTO 통합 백테스트 실행.

        Args:
            ticker: 종목 코드
            df: 종목 일봉 데이터
            index_df: 지수(KOSPI proxy) 일봉 데이터 (레짐 판별용).
                      None이면 종목 데이터로 대체.
            is_etf: ETF 여부 (위기 평균회귀 허용 판단)
        """
        df = df.copy().reset_index(drop=True)

        # 이동평균 / 보조지표 계산
        df["range"] = df["high"] - df["low"]
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma10"] = df["close"].rolling(10).mean()
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean()

        # RSI(2) for crisis mean-reversion
        df["rsi2"] = self._rsi2(df["close"])
        df["change_pct"] = df["close"].pct_change() * 100

        # RSI/ATR (ExitManager 용)
        df_ind = self._indicators.get_all_indicators(df)

        k = self._config.vb_k if self._config.is_etf(ticker) else self._config.vb_k_individual
        capital = 1_000_000
        cash = capital

        # 포지션 상태
        vb_pos = None       # VB 포지션 (당일 매수 -> EOD 청산)
        trend_pos = None    # 추세 포지션 (데드크로스까지 보유)
        mr_pos = None       # 위기MR 포지션 (RSI2>=80/트레일링/시간청산)

        trades: list[dict] = []
        equity_curve: list[tuple] = []

        # 통계
        regime_days = {"NORMAL": 0, "SWING": 0, "DEFENSE": 0, "CASH": 0}
        stock_regime_days = {"bull": 0, "bear": 0, "unknown": 0}
        vb_trades = 0
        trend_trades = 0
        mr_trades = 0
        veto_count = 0
        eod_liquidation_count = 0
        regime_blocked_count = 0    # DEFENSE/CASH 로 매수 차단된 횟수

        # ── 지수 데이터 정렬 + ATR 준비 ──
        if index_df is not None and not index_df.empty:
            idx_df = index_df.copy().reset_index(drop=True)
            idx_df["idx_range"] = idx_df["high"] - idx_df["low"]
            idx_df["idx_change_pct"] = idx_df["close"].pct_change() * 100
            # 10일 평균 range
            idx_df["idx_avg_range_10"] = idx_df["idx_range"].rolling(10).mean()
        else:
            idx_df = None

        # 레짐 시뮬레이터 초기화
        regime_sim = _RegimeSimulator(
            defense_trigger_pct=self._config.regime_defense_trigger_pct,
            cash_trigger_pct=self._config.regime_cash_trigger_pct,
        )

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

            # ── 4-모드 레짐 판단 (지수 기반) ──
            if idx_df is not None and i < len(idx_df):
                idx_change = float(idx_df.iloc[i]["idx_change_pct"]) if not pd.isna(idx_df.iloc[i]["idx_change_pct"]) else 0
                idx_close = float(idx_df.iloc[i]["close"])
                avg_r = float(idx_df.iloc[i]["idx_avg_range_10"]) if not pd.isna(idx_df.iloc[i].get("idx_avg_range_10", float("nan"))) else 0
                cur_r = float(idx_df.iloc[i]["idx_range"])
                atr_ratio = cur_r / avg_r if avg_r > 0 else 1.0
                system_regime = regime_sim.detect(idx_change, idx_close, atr_ratio)
            else:
                # 지수 데이터 없으면 종목 등락률로 대체
                chg = float(today["change_pct"]) if not pd.isna(today.get("change_pct", float("nan"))) else 0
                system_regime = regime_sim.detect(chg, float(today_close))

            regime_days[system_regime] += 1

            # ── 종목별 bull/bear 판단 (전일 기준) ──
            stock_reg = self._stock_regime(prev_ma20, prev_ma60)
            stock_regime_days[stock_reg] += 1

            # ── 실효 레짐 결정 ──
            # DEFENSE/CASH -> bear 강제 (production auto_strategy.py 와 동일)
            if system_regime in (_RegimeSimulator.DEFENSE, _RegimeSimulator.CASH):
                effective_regime = "bear"
                buy_allowed = False  # DEFENSE/CASH 에서는 신규 매수 차단
            elif system_regime == _RegimeSimulator.SWING:
                effective_regime = "bear"  # 스윙 = 보수적
                buy_allowed = True
            else:
                effective_regime = stock_reg
                buy_allowed = True

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

            # RSI(2) for crisis MR
            today_rsi2 = float(df.iloc[i]["rsi2"]) if not pd.isna(df.iloc[i]["rsi2"]) else 50
            prev_rsi2 = float(df.iloc[i - 1]["rsi2"]) if not pd.isna(df.iloc[i - 1]["rsi2"]) else 50
            today_change_pct = float(df.iloc[i]["change_pct"]) if not pd.isna(df.iloc[i]["change_pct"]) else 0

            # ================================================================
            # VB 포지션 처리 (보유 중이면 장중 손절/트레일링 체크 -> EOD 청산)
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
                    eod_liquidation_count += 1

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
            # 위기MR 포지션 처리 (RSI2>=80 수익중 / 트레일링 / 손절 / 시간청산)
            # ================================================================
            if mr_pos is not None:
                days_held = i - mr_pos["entry_idx"]
                pct_from_buy = (today_close - mr_pos["buy_price"]) / mr_pos["buy_price"] * 100
                pct_low = (today_low - mr_pos["buy_price"]) / mr_pos["buy_price"] * 100
                if today_high > mr_pos["high_price"]:
                    mr_pos["high_price"] = today_high
                drop_from_high = (mr_pos["high_price"] - today_low) / mr_pos["high_price"] * 100

                sell_mr = False
                mr_sell_price = today_close
                mr_reason = ""

                # 손절 -2%
                if pct_low <= -2.0:
                    sell_mr = True
                    mr_sell_price = int(mr_pos["buy_price"] * 0.98)
                    mr_reason = f"위기MR-손절({pct_low:+.1f}%)"

                # 트레일링: +2% 활성 -> 고점 대비 -1.5% 청산
                elif pct_from_buy >= 2.0 or mr_pos.get("trailing_activated"):
                    mr_pos["trailing_activated"] = True
                    if drop_from_high >= 1.5:
                        sell_mr = True
                        mr_sell_price = int(mr_pos["high_price"] * 0.985)
                        mr_reason = f"위기MR-트레일링({pct_from_buy:+.1f}%)"

                # RSI(2) >= 80 + 수익 중
                elif today_rsi2 >= 80 and pct_from_buy > 0:
                    sell_mr = True
                    mr_reason = f"위기MR-RSI과매수({today_rsi2:.0f})"

                # 조기청산: 1일 + 손실 중
                elif days_held >= 1 and pct_from_buy < 0:
                    sell_mr = True
                    mr_reason = f"위기MR-조기청산({days_held}d,{pct_from_buy:+.1f}%)"

                # 시간청산: 2일
                elif days_held >= 2:
                    sell_mr = True
                    mr_reason = f"위기MR-시간청산({days_held}d)"

                if sell_mr:
                    sell_qty = mr_pos["qty"]
                    actual_sell = self._cost.sell_execution_price(mr_sell_price, mr_sell_price, ticker)
                    comm, tax = self._cost.sell_cost(actual_sell, sell_qty, ticker)
                    revenue = actual_sell * sell_qty - comm - tax
                    buy_comm_remaining = mr_pos.get("buy_comm", 0)
                    pnl = (actual_sell - mr_pos["buy_price"]) * sell_qty - comm - tax - buy_comm_remaining
                    cash += revenue
                    trades.append({
                        "date": today_date, "side": "sell", "price": actual_sell,
                        "qty": sell_qty, "pnl": pnl,
                        "reason": mr_reason,
                        "strategy": "CRISIS_MR",
                    })
                    mr_pos = None

            # ================================================================
            # 매수 판단 (포지션 없을 때)
            # ================================================================
            has_any_position = vb_pos is not None or trend_pos is not None or mr_pos is not None

            if not has_any_position:
                if effective_regime == "bull" and buy_allowed:
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

                elif effective_regime == "bear":
                    if not buy_allowed:
                        # DEFENSE/CASH 에서 VB 신호가 있었을 경우 카운트
                        if (
                            stock_reg == "bull"
                            and prev_ma10 > 0
                            and today_open > prev_ma10
                            and prev_range >= today_open * 0.005
                            and today_high >= target
                        ):
                            regime_blocked_count += 1
                    else:
                        # ── 하락장: 1차 골든크로스 매수 ──
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

                        # ── 하락장: 2차 위기 평균회귀 (ETF만, buy_allowed 시) ──
                        elif is_etf and buy_allowed:
                            oversold = today_rsi2 < 15
                            drop = today_change_pct <= -2.0
                            bounce = (prev_rsi2 < 10 and today_rsi2 > prev_rsi2) or today_rsi2 < 5

                            if oversold and drop and bounce:
                                buy_price = self._cost.buy_execution_price(today_close, today_close, ticker)
                                cost_per_share = buy_price * (1 + self._config.commission_rate)
                                qty = int(cash // cost_per_share)
                                if qty > 0:
                                    buy_comm = self._cost.buy_cost(buy_price, qty)
                                    cash -= (buy_price * qty + buy_comm)
                                    mr_pos = {
                                        "qty": qty,
                                        "buy_price": buy_price,
                                        "buy_comm": buy_comm,
                                        "high_price": today_high,
                                        "entry_idx": i,
                                        "trailing_activated": False,
                                    }
                                    trades.append({
                                        "date": today_date, "side": "buy", "price": buy_price,
                                        "qty": qty, "pnl": 0,
                                        "reason": f"위기MR(RSI2={today_rsi2:.0f},{today_change_pct:+.1f}%)",
                                        "strategy": "CRISIS_MR",
                                    })
                                    mr_trades += 1

            # 에쿼티 기록
            equity = cash
            if vb_pos is not None:
                equity += vb_pos["qty"] * today_close
            if trend_pos is not None:
                equity += trend_pos["qty"] * today_close
            if mr_pos is not None:
                equity += mr_pos["qty"] * today_close
            equity_curve.append((today_date, equity))

        # ── 기간 종료: 잔여 포지션 청산 ──
        last_close = int(df.iloc[-1]["close"])
        for label, pos in [("VB", vb_pos), ("TREND", trend_pos), ("CRISIS_MR", mr_pos)]:
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
        stats["stock_regime_days"] = stock_regime_days
        stats["vb_trades"] = vb_trades
        stats["trend_trades"] = trend_trades
        stats["mr_trades"] = mr_trades
        stats["veto_count"] = veto_count
        stats["eod_liquidation_count"] = eod_liquidation_count
        stats["regime_blocked_count"] = regime_blocked_count
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
            print(f"  {ticker_name} -- AUTO 통합 백테스트 결과")
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
        print(f"  위기MR 매수:    {stats.get('mr_trades', 0)}회")
        print(f"  거부권 발동:    {stats.get('veto_count', 0)}회")
        print(f"  EOD 강제청산:   {stats.get('eod_liquidation_count', 0)}회")
        print(f"  레짐 매수차단:  {stats.get('regime_blocked_count', 0)}회")
        print(f"승/패:            {stats.get('wins', 0)}/{stats.get('losses', 0)}")
        print(f"평균 수익:        {stats.get('avg_win', 0):,}원")
        print(f"평균 손실:        {stats.get('avg_loss', 0):,}원")
        print(f"1회 최대 손실:    {stats.get('max_single_loss', 0):,}원 ({stats.get('max_single_loss_pct', 0):.1f}%)")
        print(f"최종 자본:        {stats.get('final_capital', 0):,}원")

        rd = stats.get("regime_days", {})
        total_days = sum(rd.values())
        if total_days > 0:
            print(f"레짐 분포 (4-모드):")
            for state in ["NORMAL", "SWING", "DEFENSE", "CASH"]:
                d = rd.get(state, 0)
                pct = d / total_days * 100
                print(f"  {state:8s}: {d:>4}일 ({pct:>5.1f}%)")
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

    # ETF 코드 세트 (위기 MR 허용 판단)
    etf_codes = {"069500", "229200", "133690", "131890", "108450", "395160", "261220", "132030"}

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

    # KOSPI 지수 proxy (레짐 판별용)
    index_ticker = "^KS11"

    for label, period_args in [
        ("최근 1년", {"period": "1y"}),
        ("2022 하락장", {"start": "2022-01-01", "end": "2022-12-31"}),
    ]:
        print(f"\n{'=' * 75}")
        print(f"  {label} -- AUTO 통합 (VB + 추세추종 + 위기MR + 4-모드 레짐 + 거부권)")
        print(f"  손절={config.stoploss_pct}% | 트레일링={config.trailing_activate_pct}%"
              f"->{config.trailing_stop_pct}% | VB-K={config.vb_k}/{config.vb_k_individual}"
              f" | DEFENSE={config.regime_defense_trigger_pct}% CASH={config.regime_cash_trigger_pct}%")
        print(f"{'=' * 75}")

        # 지수 다운로드
        index_df = download(index_ticker, **period_args)
        if len(index_df) < 62:
            print(f"  KOSPI 지수 데이터 부족 ({len(index_df)}일) -- 종목 데이터로 대체")
            index_df = None

        total_pnl = 0
        total_vb = 0
        total_trend = 0
        total_mr = 0
        total_veto = 0
        total_eod = 0
        total_blocked = 0
        combined_regime = {"NORMAL": 0, "SWING": 0, "DEFENSE": 0, "CASH": 0}

        for yf_ticker, name in tickers.items():
            df = download(yf_ticker, **period_args)
            if len(df) < 62:
                print(f"  {name}: 데이터 부족")
                continue

            code = yf_ticker.split(".")[0]
            ticker_is_etf = code in etf_codes

            stats = bt.run(code, df, index_df=index_df, is_etf=ticker_is_etf)

            # 바이앤홀드 비교
            first_p = int(df.iloc[61]["close"])
            last_p = int(df.iloc[-1]["close"])
            bnh = (last_p - first_p) / first_p * 100

            pnl = stats["final_capital"] - 1_000_000
            total_pnl += pnl
            total_vb += stats.get("vb_trades", 0)
            total_trend += stats.get("trend_trades", 0)
            total_mr += stats.get("mr_trades", 0)
            total_veto += stats.get("veto_count", 0)
            total_eod += stats.get("eod_liquidation_count", 0)
            total_blocked += stats.get("regime_blocked_count", 0)
            rd = stats.get("regime_days", {})
            for s in combined_regime:
                combined_regime[s] += rd.get(s, 0)

            marker = ">" if stats["total_return_pct"] > 0 else " "

            print(
                f" {marker} {name:12s} "
                f"전략{stats['total_return_pct']:>+7.1f}% "
                f"BnH{bnh:>+7.1f}% "
                f"{stats['total_trades']:>3}거래 "
                f"(VB{stats.get('vb_trades', 0):>2}"
                f"/추세{stats.get('trend_trades', 0):>2}"
                f"/MR{stats.get('mr_trades', 0):>2}"
                f"/거부{stats.get('veto_count', 0):>2}"
                f"/EOD{stats.get('eod_liquidation_count', 0):>2}) "
                f"승률{stats['win_rate_pct']:>4.0f}% "
                f"MDD{stats['max_drawdown_pct']:>5.1f}% "
                f"Sharpe{stats['sharpe_ratio']:>5.2f}"
            )

        # ── 합산 서머리 ──
        total_regime_days = sum(combined_regime.values())
        print(f"\n  === 합산 서머리 ===")
        print(f"  합산 손익: {total_pnl:>+,}원")
        print(f"  VB: {total_vb}회 | 추세: {total_trend}회 | 위기MR: {total_mr}회")
        print(f"  거부권 발동: {total_veto}회 | EOD 강제청산: {total_eod}회 | 레짐 차단: {total_blocked}회")
        if total_regime_days > 0:
            print(f"  레짐 분포 (4-모드, 전 종목 합산):")
            for state in ["NORMAL", "SWING", "DEFENSE", "CASH"]:
                d = combined_regime[state]
                pct = d / total_regime_days * 100
                bar = "#" * int(pct / 2)
                print(f"    {state:8s}: {d:>5}일 ({pct:>5.1f}%) {bar}")


if __name__ == "__main__":
    main()
