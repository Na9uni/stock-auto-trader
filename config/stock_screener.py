"""종목 품질 스크리너 — 불량주 실시간 필터링.

화이트리스트 종목이라도 당일 조건 미충족 시 매매 대상에서 제외.
signal_runner.py의 _build_market_context()에서 호출.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger("stock_analysis")


def screen_ticker(ticker: str, info: dict, candles_1d: list[dict]) -> tuple[bool, str]:
    """종목 품질 검사. (통과 여부, 사유) 반환.

    조건 미충족 시 (False, 사유) → 매매 대상에서 제외.

    필터 목록:
      1. 시가총액: 너무 작은 종목 제외 (유동성 부족)
      2. 거래대금: 일 거래대금 1억 미만 제외
      3. 이동평균 정배열: MA5 > MA20 > MA60 확인 (상승 추세)
      4. 스프레드: 호가 스프레드 1% 초과 제외
      5. 급등/급락 필터: 전일 등락률 ±15% 초과 제외 (비정상 변동)
      6. 연속 하락: 5일 연속 하락 제외
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

    # ── 2. 호가 스프레드 필터: 1% 초과 → 슬리피지 위험 ──
    orderbook = info.get("orderbook")
    if orderbook:
        try:
            bids = orderbook.get("bid", [])
            asks = orderbook.get("ask", [])
            if bids and asks:
                best_bid = int(bids[0].get("price", 0))
                best_ask = int(asks[0].get("price", 0))
                if best_bid > 0 and best_ask > 0:
                    spread_pct = (best_ask - best_bid) / best_bid * 100
                    if spread_pct > 1.0:
                        return False, f"스프레드 과다 ({spread_pct:.1f}% > 1.0%)"
        except (ValueError, TypeError, KeyError, IndexError):
            pass

    # ── 3. 급등/급락 필터: 전일 등락률 ±15% → 비정상 변동 ──
    change_rate = float(info.get("change_rate", 0))
    if abs(change_rate) > 15.0:
        return False, f"전일 등락 과대 ({change_rate:+.1f}% > ±15%)"

    # ── 일봉 기반 필터 (candles_1d 필요) ──
    if not candles_1d or len(candles_1d) < 10:
        return True, "일봉 부족 — 기본 통과"

    df = pd.DataFrame(candles_1d)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # ── 4. 연속 하락 필터: 5일 연속 음봉 → 추세 약화 ──
    if len(df) >= 6:
        last_5_closes = [float(df.iloc[-(i + 1)]["close"]) for i in range(5)]
        last_5_opens = [float(df.iloc[-(i + 1)]["open"]) for i in range(5)]
        consecutive_down = all(c < o for c, o in zip(last_5_closes, last_5_opens))
        if consecutive_down:
            return False, "5일 연속 음봉 (하락 추세)"

    # ── 5. 이동평균 역배열 필터: 제거 ──
    # AUTO 전략의 _detect_regime()이 이미 MA20/MA60 레짐 판단을 하므로
    # 스크리너에서 중복 체크 시 시장 조정기에 전 종목 차단되는 문제 발생.
    # 레짐 필터는 전략 레벨에서만 적용.

    # ── 6. 거래대금 트렌드: 5일 평균 거래대금 감소 추세 → 관심 이탈 ──
    if len(df) >= 10 and "volume" in df.columns:
        vol_5d = float(df["volume"].tail(6).head(5).mean())
        vol_10d = float(df["volume"].tail(11).head(10).mean())
        if vol_10d > 0 and vol_5d < vol_10d * 0.3:
            return False, f"거래량 급감 (5일평균/10일평균 = {vol_5d / vol_10d:.1f})"

    return True, "통과"
