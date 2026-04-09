"""추세 추종 전략 — 이동평균 골든/데드크로스 기반.

5일선이 20일선을 위로 뚫으면 매수 (골든크로스)
5일선이 20일선을 아래로 뚫으면 매도 (데드크로스)
추가 필터: 60일선 위에서만 매수 (상승 추세 확인)
"""

from __future__ import annotations

import logging

import pandas as pd

from config.trading_config import TradingConfig
from strategies.base import MarketContext, SignalResult, SignalStrength, SignalType

logger = logging.getLogger("stock_analysis")


class TrendStrategy:
    """추세 추종 전략 — 골든/데드크로스."""

    name = "trend_following"

    def __init__(self, config: TradingConfig):
        self._config = config

    def evaluate(self, ctx: MarketContext) -> SignalResult:
        """추세 추종 신호 평가."""
        candles = ctx.candles_1d_raw
        if not candles or len(candles) < 61:
            return self._neutral("데이터 부족 (60일 이상 필요)")

        df = pd.DataFrame(candles)
        for col in ("open", "high", "low", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # 정렬
        sort_col = None
        for candidate in ("datetime", "date", "Date"):
            if candidate in df.columns:
                sort_col = candidate
                break
        if sort_col is not None:
            df = df.sort_values(sort_col).reset_index(drop=True)

        # 이동평균 계산
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean()

        if len(df) < 3:
            return self._neutral("데이터 부족")

        today = df.iloc[-1]
        yesterday = df.iloc[-2]
        day_before = df.iloc[-3]

        ma5_today = float(today["ma5"]) if not pd.isna(today["ma5"]) else 0
        ma20_today = float(today["ma20"]) if not pd.isna(today["ma20"]) else 0
        ma60_today = float(today["ma60"]) if not pd.isna(today["ma60"]) else 0

        ma5_yesterday = float(yesterday["ma5"]) if not pd.isna(yesterday["ma5"]) else 0
        ma20_yesterday = float(yesterday["ma20"]) if not pd.isna(yesterday["ma20"]) else 0

        current_price = ctx.current_price

        if ma5_today == 0 or ma20_today == 0 or ma60_today == 0:
            return self._neutral("이동평균 계산 불가")

        # 2일 확인용 이동평균
        ma5_day_before = float(day_before["ma5"]) if not pd.isna(day_before.get("ma5", float("nan"))) else 0
        ma20_day_before = float(day_before["ma20"]) if not pd.isna(day_before.get("ma20", float("nan"))) else 0

        # ── 매도 신호: 데드크로스 (5일선이 20일선 아래로, 2일 확인) ──
        if (ma5_day_before >= ma20_day_before   # 2일전: MA5 >= MA20 (크로스 이전)
            and ma5_yesterday < ma20_yesterday   # 어제: MA5 < MA20 (크로스 시작)
            and ma5_today < ma20_today):          # 오늘: MA5 < MA20 (크로스 확인)
            return SignalResult(
                signal_type=SignalType.SELL,
                strength=SignalStrength.STRONG,
                score=-10.0,
                reasons=[
                    f"데드크로스! MA5 {ma5_today:,.0f} < MA20 {ma20_today:,.0f}",
                    f"전일 MA5 {ma5_yesterday:,.0f} >= MA20 {ma20_yesterday:,.0f}",
                ],
                strategy_name=self.name,
            )

        # ── 매수 신호: 골든크로스 (5일선이 20일선 위로, 2일 확인) ──
        if (ma5_day_before <= ma20_day_before  # 2일전: MA5 <= MA20 (크로스 이전)
            and ma5_yesterday > ma20_yesterday  # 어제: MA5 > MA20 (크로스 시작)
            and ma5_today > ma20_today):        # 오늘: MA5 > MA20 (크로스 확인)
            # 추가 필터: 현재가가 60일선 위에 있어야 함 (큰 추세 확인)
            if current_price <= ma60_today:
                return self._neutral(
                    f"골든크로스이나 60일선 아래 "
                    f"(현재가 {current_price:,} <= MA60 {ma60_today:,.0f})"
                )

            return SignalResult(
                signal_type=SignalType.BUY,
                strength=SignalStrength.STRONG,
                score=10.0,
                reasons=[
                    f"골든크로스! MA5 {ma5_today:,.0f} > MA20 {ma20_today:,.0f}",
                    f"현재가 {current_price:,} > MA60 {ma60_today:,.0f}",
                    f"전일 MA5 {ma5_yesterday:,.0f} <= MA20 {ma20_yesterday:,.0f}",
                ],
                strategy_name=self.name,
            )

        # ── 추세 유지 중 (보유 지속 신호) ──
        if ma5_today > ma20_today and current_price > ma60_today:
            return SignalResult(
                signal_type=SignalType.NEUTRAL,
                strength=SignalStrength.MEDIUM,
                score=5.0,
                reasons=[
                    f"상승 추세 유지 중 (MA5 {ma5_today:,.0f} > MA20 {ma20_today:,.0f})",
                ],
                strategy_name=self.name,
            )

        return self._neutral(
            f"신호 없음 (MA5={ma5_today:,.0f}, MA20={ma20_today:,.0f}, MA60={ma60_today:,.0f})"
        )

    def _neutral(self, reason: str) -> SignalResult:
        return SignalResult(
            signal_type=SignalType.NEUTRAL,
            strength=SignalStrength.WEAK,
            reasons=[reason],
            strategy_name=self.name,
        )
