"""합산 전략 래퍼 — 기존 signal_detector를 거부권(veto) 역할로 사용.

단독으로 매수 신호를 내지 않고, score < -3이면 다른 전략의 매수를 거부한다.
"""

from __future__ import annotations

import logging

from config.trading_config import TradingConfig
from strategies.base import MarketContext, SignalResult, SignalStrength, SignalType

logger = logging.getLogger("stock_analysis")

VETO_THRESHOLD = -3  # 이 점수 이하이면 매수 거부


class ScoreStrategy:
    """합산 전략 (거부권 모드)."""

    name = "score_veto"

    def __init__(self, config: TradingConfig):
        self._config = config

    def evaluate(self, ctx: MarketContext) -> SignalResult:
        """합산 점수 평가. 거부권으로만 사용."""
        from alerts.signal_detector import detect

        if ctx.candles_5m is None or ctx.candles_5m.empty:
            return self._neutral("5분봉 데이터 없음")

        signal = detect(
            ctx.candles_5m,
            exec_strength=ctx.exec_strength,
            change_rate=ctx.change_rate,
            orderbook=ctx.orderbook,
        )

        return SignalResult(
            signal_type=signal.signal_type,
            strength=signal.strength,
            score=signal.score,
            reasons=signal.reasons,
            warnings=signal.warnings,
            rsi=signal.rsi,
            macd_cross=signal.macd_cross,
            vol_ratio=signal.vol_ratio,
            strategy_name=self.name,
        )

    def should_veto(self, ctx: MarketContext) -> tuple[bool, str]:
        """매수 거부 여부 판단. (True, 사유) 또는 (False, "")."""
        result = self.evaluate(ctx)
        if result.score <= VETO_THRESHOLD:
            reason = f"합산 점수 {result.score} <= {VETO_THRESHOLD} (거부권 발동)"
            logger.info("[거부권] %s: %s", ctx.name, reason)
            return True, reason
        return False, ""

    def _neutral(self, reason: str) -> SignalResult:
        return SignalResult(
            signal_type=SignalType.NEUTRAL,
            strength=SignalStrength.WEAK,
            reasons=[reason],
            strategy_name=self.name,
        )
