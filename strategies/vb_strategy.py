"""변동성 돌파 전략 — 래리 윌리엄스 방식.

목표가 = 당일 시가 + 5일 ATR × K
마켓 필터: 시가 > 10일 이동평균
장중 목표가 돌파 시 매수 → 트레일링 스탑 청산
시간대별 가중치: 09:10~09:30, 점심시간 → MEDIUM (자동매매 안 함, 알림만)

TODO: 배당락일 필터 (키움 opt10059 TR), 어닝 일정 필터 (KRX 크롤링)
"""

from __future__ import annotations

import logging

from config.trading_config import TradingConfig
from config.whitelist import is_etf
from strategies.base import MarketContext, SignalResult, SignalStrength, SignalType

logger = logging.getLogger("stock_analysis")


class VBStrategy:
    """변동성 돌파 전략."""

    name = "volatility_breakout"

    def __init__(self, config: TradingConfig):
        self._config = config

    def evaluate(self, ctx: MarketContext) -> SignalResult:
        """변동성 돌파 신호 평가."""
        candles = ctx.candles_1d_raw
        if not candles or len(candles) < 12:
            return self._neutral("데이터 부족")

        # K값: 종목별 최적값 우선, 없으면 ETF/개별주 기본값
        from config.whitelist import get_ticker_k
        ticker_k = get_ticker_k(ctx.ticker)
        if ticker_k is not None:
            k = ticker_k
        else:
            k = self._config.vb_k if is_etf(ctx.ticker) else self._config.vb_k_individual

        # 일봉 데이터 추출
        import pandas as pd
        df = pd.DataFrame(candles)
        for col in ("open", "high", "low", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        # date 또는 datetime 컬럼으로 정렬 (키움 수집기는 "date" 키 사용)
        sort_col = None
        for candidate in ("datetime", "date", "Date"):
            if candidate in df.columns:
                sort_col = candidate
                break
        if sort_col is not None:
            df = df.sort_values(sort_col).reset_index(drop=True)

        today = df.iloc[-1]
        yesterday = df.iloc[-2]

        today_open = int(today["open"])
        prev_high = int(yesterday["high"])
        prev_low = int(yesterday["low"])
        # 5일 ATR (전일까지 5일간 range 평균)
        if len(df) >= 7:
            ranges = df["high"].iloc[-7:-1] - df["low"].iloc[-7:-1]  # 어제까지 6일, 상위 5일
            prev_range = int(ranges.tail(5).mean())
        else:
            prev_range = prev_high - prev_low
        target_price = today_open + int(prev_range * k)

        # MA10/20/60 계산 (전일까지)
        ma10 = float(df["close"].tail(11).head(10).mean())
        ma20 = float(df["close"].tail(21).head(20).mean()) if len(df) >= 21 else 0
        ma60 = float(df["close"].tail(61).head(60).mean()) if len(df) >= 61 else 0

        # 0) 시장 레짐 필터: MA20 > MA60 (상승장만 진입) — 백테스트와 동일
        if ma20 > 0 and ma60 > 0 and ma20 < ma60:
            logger.debug("[VB] %s 레짐필터 실패 (MA20 %.0f < MA60 %.0f)", ctx.name, ma20, ma60)
            return self._neutral(
                f"레짐필터 실패 (MA20 {ma20:,.0f} < MA60 {ma60:,.0f})"
            )
        logger.debug("[VB] %s 레짐필터 통과 (MA20 %.0f > MA60 %.0f)", ctx.name, ma20, ma60)

        # 1) 마켓 필터: 시가 > MA10
        if today_open <= ma10:
            logger.debug("[VB] %s 마켓필터 실패 (시가 %s <= MA10 %.0f)", ctx.name, f"{today_open:,}", ma10)
            return self._neutral(
                f"마켓필터 실패 (시가 {today_open:,} <= MA10 {ma10:,.0f})"
            )
        logger.debug("[VB] %s 마켓필터 통과 (시가 %s > MA10 %.0f)", ctx.name, f"{today_open:,}", ma10)

        # 2) 변동성 필터: 전일 레인지 >= 시가의 0.5%
        if prev_range < today_open * 0.005:
            logger.debug("[VB] %s 변동성필터 실패 (range=%s < 시가 %s의 0.5%%)", ctx.name, f"{prev_range:,}", f"{today_open:,}")
            return self._neutral(
                f"변동성 부족 (range={prev_range:,} < 0.5%)"
            )
        logger.debug("[VB] %s 변동성필터 통과 (range=%s)", ctx.name, f"{prev_range:,}")

        # 3) 목표가 돌파 확인 (현재가 또는 당일 장중 고가)
        # 쿨다운이 중복 진입을 방지하므로 intraday_high 사용 가능
        check_price = max(ctx.current_price, ctx.intraday_high) if ctx.intraday_high > 0 else ctx.current_price
        logger.debug("[VB] %s 목표가 체크: 현재가 %s vs 목표가 %s (K=%.2f)", ctx.name, f"{check_price:,}", f"{target_price:,}", k)
        if check_price >= target_price:
            # 거래량 확인: 전일 거래량이 5일 평균의 50% 이상이어야 함
            if "volume" in df.columns and len(df) >= 6:
                df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
                prev_vol = int(yesterday.get("volume", 0))
                avg_vol = float(df["volume"].tail(6).head(5).mean())
                if avg_vol > 0 and prev_vol < avg_vol * 0.5:
                    logger.debug("[VB] %s 거래량 부족 (전일 %s < 평균 %.0f의 50%%)", ctx.name, f"{prev_vol:,}", avg_vol)
                    return self._neutral(f"거래량 부족 (전일 {prev_vol:,} < 평균 {avg_vol:,.0f}의 50%)")
                logger.debug("[VB] %s 거래량 통과 (전일 %s / 평균 %.0f)", ctx.name, f"{prev_vol:,}", avg_vol)

            logger.debug(
                "[VB] %s *** 매수 신호 발생! K=%.2f 목표가=%s",
                ctx.name, k, f"{target_price:,}",
            )
            return SignalResult(
                signal_type=SignalType.BUY,
                strength=SignalStrength.STRONG,
                score=10.0,
                reasons=[
                    f"돌파! 현재가 {ctx.current_price:,} >= 목표가 {target_price:,}",
                    f"시가{today_open:,} + range{prev_range:,}×{k}",
                    f"MA10={ma10:,.0f}",
                ],
                target_price=target_price,
                strategy_name=self.name,
            )

        return self._neutral(
            f"미돌파 (현재가 {ctx.current_price:,} < 목표가 {target_price:,})"
        )

    def _neutral(self, reason: str) -> SignalResult:
        return SignalResult(
            signal_type=SignalType.NEUTRAL,
            strength=SignalStrength.WEAK,
            reasons=[reason],
            strategy_name=self.name,
        )
