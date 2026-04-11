"""자동 전환 전략 — 시장 상황에 따라 데이트레이딩/추세추종 자동 전환.

상승장 (MA20 > MA60): 변동성 돌파 (데이트레이딩) → 적극 매매
하락장 (MA20 < MA60): 추세추종 (골든크로스만) → 안전 매매
"""

from __future__ import annotations

import logging

import pandas as pd

from config.trading_config import TradingConfig
from strategies.base import MarketContext, SignalResult, SignalStrength, SignalType
from strategies.vb_strategy import VBStrategy
from strategies.score_strategy import ScoreStrategy, VETO_THRESHOLD
from strategies.trend_strategy import TrendStrategy

logger = logging.getLogger("stock_analysis")


class AutoStrategy:
    """시장 상황에 따라 자동으로 전략을 전환한다."""

    name = "auto"

    def __init__(self, config: TradingConfig):
        self._config = config
        # 두 전략을 모두 준비
        self._vb = VBStrategy(config)
        self._score = ScoreStrategy(config)
        self._trend = TrendStrategy(config)

    def _detect_regime(self, ctx: MarketContext) -> str:
        """시장 레짐 판단: 상승장 / 하락장 / 급락.

        1차: MA20 vs MA60 (기본 레짐)
        2차: 현재가 vs MA20 이격도 (급락 감지 보조 필터)
             → 현재가가 MA20보다 10% 이상 아래면 급락으로 판단, bear 전환
        """
        # 시스템 레짐이 DEFENSE/CASH/SWING이면 per-stock 판단 무시
        try:
            from strategies.regime_engine import get_regime_engine, RegimeState
            system_regime = get_regime_engine().state
            if system_regime == RegimeState.CASH:
                return "bear"
            if system_regime == RegimeState.DEFENSE:
                return "bear"
            if system_regime == RegimeState.SWING:
                return "bear"  # 스윙 모드에서는 보수적으로
            # NORMAL: 기존 per-stock MA20/MA60 로직 사용
        except Exception:
            pass  # 레짐 엔진 실패 시 기존 로직으로 폴백

        candles = ctx.candles_1d_raw
        if not candles or len(candles) < 61:
            return "unknown"

        df = pd.DataFrame(candles)
        for col in ("open", "high", "low", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        sort_col = None
        for candidate in ("datetime", "date", "Date"):
            if candidate in df.columns:
                sort_col = candidate
                break
        if sort_col is not None:
            df = df.sort_values(sort_col).reset_index(drop=True)

        ma20 = float(df["close"].tail(21).head(20).mean()) if len(df) >= 21 else 0
        ma60 = float(df["close"].tail(61).head(60).mean()) if len(df) >= 61 else 0

        if ma20 <= 0 or ma60 <= 0:
            return "unknown"

        # 급락 감지: 현재가가 MA20보다 10% 이상 아래면 → bear 강제 전환
        if ma20 > 0 and ctx.current_price > 0:
            deviation = (ctx.current_price - ma20) / ma20
            if deviation < -0.10:
                logger.info(
                    "[AUTO 레짐] %s 급락 감지 (현재가 %s vs MA20 %.0f, 이격 %.1f%%) → bear",
                    ctx.name, ctx.current_price, ma20, deviation * 100,
                )
                return "bear"

        # 추가 방어 1: 5일 연속 하락 + MA20 대비 -5% → bear 강제
        if ma20 > 0 and ctx.current_price > 0:
            deviation = (ctx.current_price - ma20) / ma20
            if len(df) >= 6:
                last_5_closes = [float(df.iloc[-(i+1)]["close"]) for i in range(5)]
                if all(last_5_closes[i] > last_5_closes[i+1] for i in range(len(last_5_closes)-1)):
                    # 5일 연속 하락
                    if deviation < -0.05:
                        logger.info("[AUTO 레짐] %s 5일 연속 하락 + MA20 대비 %.1f%% → bear", ctx.name, deviation * 100)
                        return "bear"

        # 추가 방어 2: MA20 기울기 하락 → 상승장 약화 경고
        ma20_5ago = float(df["close"].tail(26).head(20).mean()) if len(df) >= 26 else 0
        if ma20_5ago > 0 and ma20 > 0 and ma20 < ma20_5ago * 0.99:
            # MA20이 5일간 1% 이상 하락 → bear 전환
            if ma20 <= ma60:
                return "bear"
            # MA20 > MA60이지만 하락 중이면 추가 경고만
            logger.debug("[AUTO 레짐] %s MA20 하락 추세 (%.0f → %.0f)", ctx.name, ma20_5ago, ma20)

        # 횡보장 감지: MA20과 MA60 차이 1% 미만
        if abs(ma20 - ma60) / ma60 < 0.01:
            logger.info("[AUTO 레짐] %s 횡보장 감지 (MA20 %.0f ≈ MA60 %.0f) → bear 모드", ctx.name, ma20, ma60)
            return "bear"  # 횡보장에서는 보수적으로 → 추세추종(거의 안 삼)

        if ma20 > ma60:
            return "bull"
        else:
            return "bear"

    def evaluate(self, ctx: MarketContext) -> SignalResult:
        """시장 상황에 따라 적절한 전략으로 평가."""
        regime = self._detect_regime(ctx)

        if regime == "bull":
            # 상승장: 변동성 돌파 (데이트레이딩) + 합산 거부권
            vb_signal = self._vb.evaluate(ctx)

            if vb_signal.signal_type == SignalType.BUY:
                # 거부권 체크 (5분봉 데이터가 있을 때만)
                if ctx.candles_5m is not None and len(ctx.candles_5m) > 0:
                    score_signal = self._score.evaluate(ctx)
                    if (score_signal.signal_type == SignalType.SELL
                            and score_signal.score <= VETO_THRESHOLD):
                        return SignalResult(
                            signal_type=SignalType.NEUTRAL,
                            strength=SignalStrength.WEAK,
                            reasons=[
                                f"[AUTO-상승장] VB 매수 but 거부권 발동 "
                                f"(합산={score_signal.score:.0f})",
                            ],
                            strategy_name=self.name,
                        )

                vb_signal.reasons.insert(0, "[AUTO-상승장] 데이트레이딩 모드")
                vb_signal.strategy_name = self.name
                return vb_signal

            return SignalResult(
                signal_type=SignalType.NEUTRAL,
                strength=SignalStrength.WEAK,
                reasons=[f"[AUTO-상승장] {vb_signal.reasons[0] if vb_signal.reasons else '대기'}"],
                strategy_name=self.name,
            )

        elif regime == "bear":
            # 하락장: 추세추종 (골든크로스만 매수)
            trend_signal = self._trend.evaluate(ctx)

            if trend_signal.signal_type != SignalType.NEUTRAL:
                trend_signal.reasons.insert(0, "[AUTO-하락장] 추세추종 모드")
                trend_signal.strategy_name = self.name
                return trend_signal

            return SignalResult(
                signal_type=SignalType.NEUTRAL,
                strength=SignalStrength.WEAK,
                reasons=["[AUTO-하락장] 추세추종 대기 (골든크로스 미발생)"],
                strategy_name=self.name,
            )

        else:
            return SignalResult(
                signal_type=SignalType.NEUTRAL,
                strength=SignalStrength.WEAK,
                reasons=["[AUTO] 레짐 판단 불가 (데이터 부족)"],
                strategy_name=self.name,
            )
