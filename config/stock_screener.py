"""종목 품질 스크리너 — 최소한의 불량주 필터링.

단순하고 일관된 전략을 위해 핵심 2개만 유지.
나머지 필터링은 VB 전략 + 레짐 엔진이 담당.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("stock_analysis")


def screen_ticker(ticker: str, info: dict, candles_1d: list[dict]) -> tuple[bool, str]:
    """종목 품질 검사. (통과 여부, 사유) 반환.

    필터 (핵심 2겹만):
      1. 거래대금: 일 거래대금 1억 미만 제외 (유동성 부족)
      2. 급등/급락: 전일 등락률 ±15% 초과 제외 (비정상 변동)
    """
    current_price = int(info.get("current_price", 0))
    if current_price <= 0:
        return False, "현재가 없음"

    # ── 1. 거래대금 필터: 일 거래대금 1억 미만 → 유동성 부족 ──
    volume = int(info.get("volume", 0))
    if volume > 0 and current_price > 0:
        trading_value = volume * current_price
        if trading_value < 100_000_000:  # 1억원 미만
            return False, f"거래대금 부족 ({trading_value / 100_000_000:.1f}억 < 1억)"

    # ── 2. 급등/급락 필터: 전일 등락률 ±15% → 비정상 변동 ──
    change_rate = float(info.get("change_rate", 0))
    if abs(change_rate) > 15.0:
        return False, f"전일 등락 과대 ({change_rate:+.1f}% > ±15%)"

    return True, "통과"
