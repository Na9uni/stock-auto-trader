"""변동성 돌파 전략 (래리 윌리엄스)

목표가 = 당일 시가 + (전일 고가 - 전일 저가) × K
마켓 필터: 시가 > 10일 이동평균
장중 목표가 돌파 시 매수 → 익일 시가 매도
"""
import logging
import pandas as pd
import numpy as np
from dataclasses import dataclass

logger = logging.getLogger("stock_analysis")


@dataclass
class BreakoutSignal:
    """변동성 돌파 신호 결과."""
    should_buy: bool
    target_price: int
    current_price: int
    open_price: int
    prev_range: int
    ma10: float
    reason: str


def calc_target_price(candles_1d: list[dict], k: float = 0.5) -> dict:
    """일봉 데이터에서 당일 목표가 계산.

    Returns:
        {target_price, open_price, prev_range, ma10, valid}
    """
    if not candles_1d or len(candles_1d) < 12:
        return {"valid": False}

    # 최신 데이터가 먼저 오는 경우 대비 (정렬)
    df = pd.DataFrame(candles_1d)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # datetime 기준 정렬
    if "datetime" in df.columns:
        df = df.sort_values("datetime").reset_index(drop=True)

    today = df.iloc[-1]
    yesterday = df.iloc[-2]

    today_open = int(today["open"])
    prev_high = int(yesterday["high"])
    prev_low = int(yesterday["low"])
    prev_range = prev_high - prev_low

    target_price = today_open + int(prev_range * k)

    # 마켓 필터: 10일 이동평균
    ma10 = float(df["close"].tail(11).head(10).mean())  # 전일까지의 10일 MA

    return {
        "target_price": target_price,
        "open_price": today_open,
        "prev_range": prev_range,
        "ma10": ma10,
        "valid": True,
    }


def check_breakout(
    current_price: int,
    candles_1d: list[dict],
    k: float = 0.5,
) -> BreakoutSignal:
    """변동성 돌파 매수 신호 확인.

    Args:
        current_price: 현재가 (실시간 or 최신)
        candles_1d: 일봉 캔들 리스트
        k: 변동성 계수 (기본 0.5)

    Returns:
        BreakoutSignal
    """
    result = calc_target_price(candles_1d, k)
    if not result.get("valid"):
        return BreakoutSignal(
            should_buy=False, target_price=0, current_price=current_price,
            open_price=0, prev_range=0, ma10=0, reason="데이터 부족",
        )

    target = result["target_price"]
    open_price = result["open_price"]
    prev_range = result["prev_range"]
    ma10 = result["ma10"]

    # 마켓 필터: 시가 > 10일 MA
    if open_price <= ma10:
        return BreakoutSignal(
            should_buy=False, target_price=target, current_price=current_price,
            open_price=open_price, prev_range=prev_range, ma10=ma10,
            reason=f"마켓필터 실패 (시가 {open_price:,} <= MA10 {ma10:,.0f})",
        )

    # 변동성 너무 작으면 스킵 (전일 범위가 시가의 0.5% 미만)
    if prev_range < open_price * 0.005:
        return BreakoutSignal(
            should_buy=False, target_price=target, current_price=current_price,
            open_price=open_price, prev_range=prev_range, ma10=ma10,
            reason=f"변동성 부족 (range={prev_range:,} < 0.5%)",
        )

    # 목표가 돌파 확인
    if current_price >= target:
        return BreakoutSignal(
            should_buy=True, target_price=target, current_price=current_price,
            open_price=open_price, prev_range=prev_range, ma10=ma10,
            reason=f"돌파! 현재가 {current_price:,} >= 목표가 {target:,} "
                   f"(시가{open_price:,} + range{prev_range:,}×{k})",
        )

    return BreakoutSignal(
        should_buy=False, target_price=target, current_price=current_price,
        open_price=open_price, prev_range=prev_range, ma10=ma10,
        reason=f"미돌파 (현재가 {current_price:,} < 목표가 {target:,})",
    )
