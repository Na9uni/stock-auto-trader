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
            return self._neutral(
                f"레짐필터 실패 (MA20 {ma20:,.0f} < MA60 {ma60:,.0f})"
            )

        # 1) 마켓 필터: 시가 > MA10
        if today_open <= ma10:
            return self._neutral(
                f"마켓필터 실패 (시가 {today_open:,} <= MA10 {ma10:,.0f})"
            )

        # 2) 변동성 필터: 전일 레인지 >= 시가의 0.5%
        if prev_range < today_open * 0.005:
            return self._neutral(
                f"변동성 부족 (range={prev_range:,} < 0.5%)"
            )

        # 3) 가격-RSI 다이버전스: 가격 신고가인데 RSI 하락 → 거짓 돌파 경고
        if len(df) >= 15:
            # 간단한 RSI(14) 계산
            _delta = df["close"].diff()
            _gain = _delta.clip(lower=0).rolling(14).mean()
            _loss = (-_delta.clip(upper=0)).rolling(14).mean()
            _rs = _gain / _loss.replace(0, float('nan'))
            _rsi = 100 - (100 / (1 + _rs))

            if len(_rsi) >= 11 and not pd.isna(_rsi.iloc[-2]) and not pd.isna(_rsi.iloc[-6]):
                price_higher = float(df["high"].iloc[-2]) > float(df["high"].iloc[-6])
                rsi_lower = float(_rsi.iloc[-2]) < float(_rsi.iloc[-6])

                if price_higher and rsi_lower and float(_rsi.iloc[-2]) > 65:
                    return self._neutral(
                        f"베어리시 다이버전스 (가격 신고가 but RSI 하락 {_rsi.iloc[-2]:.0f})"
                    )

        # 4) 목표가 돌파 확인 (현재가 또는 당일 장중 고가)
        # 쿨다운이 중복 진입을 방지하므로 intraday_high 사용 가능
        check_price = max(ctx.current_price, ctx.intraday_high) if ctx.intraday_high > 0 else ctx.current_price
        if check_price >= target_price:
            # 거래량 확인: 전일 거래량이 5일 평균의 50% 이상이어야 함
            if "volume" in df.columns and len(df) >= 6:
                df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
                prev_vol = int(yesterday.get("volume", 0))
                avg_vol = float(df["volume"].tail(6).head(5).mean())
                if avg_vol > 0 and prev_vol < avg_vol * 0.5:
                    return self._neutral(f"거래량 부족 (전일 {prev_vol:,} < 평균 {avg_vol:,.0f}의 50%)")

            # 5) 체결강도 필터: 매수세 > 매도세 확인
            # exec_strength: 100 = 매수/매도 동일, 100+ = 매수 우세, 100- = 매도 우세
            if ctx.exec_strength > 0:  # 0이면 데이터 없음 → 스킵
                if ctx.exec_strength < 100:
                    return self._neutral(
                        f"체결강도 부족 ({ctx.exec_strength:.0f}% < 100%)"
                    )

            # 6) 호가창 불균형: 매수 잔량 > 매도 잔량 확인
            if ctx.orderbook:
                try:
                    bids = ctx.orderbook.get("bid", [])
                    asks = ctx.orderbook.get("ask", [])
                    if bids and asks:
                        buy_qty = sum(b.get("qty", 0) for b in bids[:5])
                        sell_qty = sum(a.get("qty", 0) for a in asks[:5])
                        if sell_qty > 0:
                            imbalance = buy_qty / sell_qty
                            if imbalance < 0.7:  # 매도 잔량이 매수의 1.4배 이상
                                return self._neutral(
                                    f"호가 불균형 (매수/매도 비율 {imbalance:.1f}, 매도 우세)"
                                )
                except (ValueError, TypeError, KeyError):
                    pass  # 호가 데이터 불완전 시 필터 스킵

            # 7) 시간대별 신호 품질 필터
            # 09:10~09:30 초반 변동성 구간: 거짓 돌파 위험 → MEDIUM으로 강도 하향
            # 11:30~13:00 저유동성 구간: 거짓 돌파 위험 → MEDIUM으로 강도 하향
            # 나머지: STRONG (정상)
            from datetime import datetime as _dt
            _now = _dt.now()
            _hour, _min = _now.hour, _now.minute
            _time_strength = SignalStrength.STRONG
            _time_note = ""
            if _hour == 9 and _min < 30:
                _time_strength = SignalStrength.MEDIUM
                _time_note = " (09:10~09:30 초반변동성 주의)"
            elif 11 <= _hour <= 12 and (_hour == 11 and _min >= 30 or _hour == 12):
                _time_strength = SignalStrength.MEDIUM
                _time_note = " (점심시간 저유동성 주의)"

            return SignalResult(
                signal_type=SignalType.BUY,
                strength=_time_strength,
                score=10.0,
                reasons=[
                    f"돌파! 현재가 {ctx.current_price:,} >= 목표가 {target_price:,}{_time_note}",
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
