"""콤보 전략 — 변동성 돌파 + 합산 거부권.

변동성 돌파 신호가 발생해도 합산 점수가 -3 이하이면 매수를 거부한다.
"""

from __future__ import annotations

import logging

from config.trading_config import TradingConfig
from strategies.base import MarketContext, SignalResult, SignalStrength, SignalType
from strategies.score_strategy import ScoreStrategy
from strategies.vb_strategy import VBStrategy

logger = logging.getLogger("stock_analysis")


class ComboStrategy:
    """변동성 돌파 (주) + 합산 거부권 (보조)."""

    name = "combo"

    def __init__(self, config: TradingConfig):
        self._vb = VBStrategy(config)
        self._score = ScoreStrategy(config)

    def evaluate(self, ctx: MarketContext) -> SignalResult:
        """1차: 변동성 돌파 → 2차: 합산 거부권."""
        vb_signal = self._vb.evaluate(ctx)

        # 변동성 돌파 매수 신호가 없으면 그대로 반환
        if vb_signal.signal_type != SignalType.BUY:
            return vb_signal

        # 변동성 돌파 매수 신호 있음 → 합산 거부권 체크
        vetoed, veto_reason = self._score.should_veto(ctx)
        if vetoed:
            return SignalResult(
                signal_type=SignalType.NEUTRAL,
                strength=SignalStrength.WEAK,
                reasons=vb_signal.reasons + [f"[거부] {veto_reason}"],
                strategy_name=self.name,
            )

        # 거부권 통과 → 매수 확정
        return SignalResult(
            signal_type=SignalType.BUY,
            strength=SignalStrength.STRONG,
            score=vb_signal.score,
            reasons=vb_signal.reasons + ["합산 거부권 통과"],
            target_price=vb_signal.target_price,
            strategy_name=self.name,
        )
