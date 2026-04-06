"""청산 로직 — ATR 기반 동적 손절 + 분할 익절 + 트레일링.

고정 손절(2%)이 아닌 ATR × 배수로 시장 변동성에 적응한다.
횡보장(ATR 작음) → 타이트한 손절, 추세장(ATR 큼) → 여유 있는 손절.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from config.trading_config import TradingConfig


class ExitReason(Enum):
    STOPLOSS = "stoploss"
    TRAILING_STOP = "trailing_stop"
    PARTIAL_TAKE_PROFIT = "partial_take_profit"
    OVERBOUGHT_TRAILING = "overbought_trailing"
    NEXT_DAY_OPEN = "next_day_open"


@dataclass
class ExitAction:
    reason: ExitReason
    qty: int
    price: int
    pct: float = 0.0


class ExitManager:
    """포지션 청산 관리 — ATR 기반 동적 손절."""

    def __init__(self, config: TradingConfig):
        self._sl_pct = config.stoploss_pct          # 고정 손절 상한 (안전망)
        self._ta_pct = config.trailing_activate_pct
        self._ts_pct = config.trailing_stop_pct
        self._atr_sl_mult = 1.5   # 손절 = ATR × 1.5
        self._atr_ts_mult = 1.0   # 트레일링 = ATR × 1.0

    def check(
        self,
        buy_price: int,
        qty: int,
        high_price: int,
        current_low: int,
        current_high: int,
        current_close: int,
        rsi: float,
        trailing_activated: bool,
        partial_sold: bool,
        atr: float = 0.0,
    ) -> tuple[list[ExitAction], bool, bool]:
        """청산 판단.

        Args:
            atr: 현재 ATR값. 0이면 고정 손절% 사용.

        Returns:
            (actions, new_trailing_activated, new_partial_sold)
        """
        actions: list[ExitAction] = []
        new_trailing = trailing_activated
        new_partial = partial_sold

        if current_high > high_price:
            high_price = current_high

        pct_from_buy = (current_close - buy_price) / buy_price * 100
        pct_high = (current_high - buy_price) / buy_price * 100
        pct_low = (current_low - buy_price) / buy_price * 100
        drop_from_high = (high_price - current_low) / high_price * 100 if high_price > 0 else 0

        # ATR 기반 동적 손절 (ATR이 있으면 사용, 없으면 고정%)
        if atr > 0 and buy_price > 0:
            atr_sl_pct = (atr * self._atr_sl_mult) / buy_price * 100
            # 상한 클리핑: 아무리 ATR이 커도 고정 손절%를 넘지 않음
            sl_pct = min(atr_sl_pct, self._sl_pct)
            # 하한: 비용보다는 커야 함 (최소 1.0%)
            sl_pct = max(sl_pct, 1.0)
        else:
            sl_pct = self._sl_pct

        # ATR 기반 트레일링 스탑
        if atr > 0 and high_price > 0:
            atr_ts_pct = (atr * self._atr_ts_mult) / high_price * 100
            ts_pct = min(atr_ts_pct, self._ts_pct * 2)  # 상한: 고정의 2배
            ts_pct = max(ts_pct, 0.5)  # 하한: 0.5%
        else:
            ts_pct = self._ts_pct

        # 1) 손절: 저가가 동적 손절선 이하
        if pct_low <= -sl_pct:
            stoploss_price = int(buy_price * (1 - sl_pct / 100))
            actions.append(ExitAction(
                reason=ExitReason.STOPLOSS,
                qty=qty,
                price=stoploss_price,
                pct=pct_low,
            ))
            return actions, new_trailing, new_partial

        # 2) 트레일링 활성화 (종가 기준 — 실전 check_auto_positions와 동일)
        if not trailing_activated and pct_from_buy >= self._ta_pct:
            new_trailing = True
            if not partial_sold:
                half_qty = qty // 2
                if half_qty > 0:
                    new_partial = True
                    actions.append(ExitAction(
                        reason=ExitReason.PARTIAL_TAKE_PROFIT,
                        qty=half_qty,
                        price=current_close,
                        pct=pct_high,
                    ))
                    # 분할 익절 발생 시 같은 봉에서 추가 매도 안 함 (실전과 동일)
                    return actions, new_trailing, new_partial

        # 3) 과매수 트레일링: RSI>=75, 수익 1%+, 고점 대비 ts*0.5 하락
        if rsi >= 75 and pct_from_buy > 1.0 and drop_from_high >= ts_pct * 0.5:
            remaining = qty if not new_partial else qty - qty // 2
            if remaining > 0:
                actions.append(ExitAction(
                    reason=ExitReason.OVERBOUGHT_TRAILING,
                    qty=remaining,
                    price=current_close,
                    pct=pct_from_buy,
                ))
                return actions, new_trailing, new_partial

        # 4) 트레일링 스탑 (동적 ATR 기반)
        if (trailing_activated or new_trailing) and drop_from_high >= ts_pct:
            remaining = qty if not new_partial else qty - qty // 2
            if remaining > 0:
                trailing_price = int(high_price * (1 - ts_pct / 100))
                actions.append(ExitAction(
                    reason=ExitReason.TRAILING_STOP,
                    qty=remaining,
                    price=trailing_price,
                    pct=pct_from_buy,
                ))

        return actions, new_trailing, new_partial
