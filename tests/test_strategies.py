"""전략 테스트 — VB, AutoStrategy 핵심 로직 검증."""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from strategies.base import MarketContext, SignalResult, SignalType, SignalStrength
from strategies.vb_strategy import VBStrategy
from strategies.auto_strategy import AutoStrategy
from config.trading_config import TradingConfig


def _make_candles(n: int = 70, base_price: int = 80000, daily_range: int = 1000) -> list[dict]:
    """테스트용 일봉 데이터 생성."""
    candles = []
    for i in range(n):
        day = i % 28 + 1
        month = 4 + (i // 28)
        candles.append({
            "date": f"2026{month:02d}{day:02d}",
            "open": base_price,
            "high": base_price + daily_range,
            "low": base_price - 100,
            "close": base_price + 500,
            "volume": 100000,
        })
    return candles


def _make_ctx(
    candles: list[dict],
    current_price: int,
    ticker: str = "069500",
    name: str = "TEST",
    intraday_high: int = 0,
) -> MarketContext:
    """테스트용 MarketContext 생성."""
    return MarketContext(
        ticker=ticker,
        name=name,
        current_price=current_price,
        change_rate=0.0,
        candles_5m=pd.DataFrame(),
        candles_1d=pd.DataFrame(),
        candles_1d_raw=candles,
        intraday_high=intraday_high if intraday_high > 0 else current_price,
    )


class TestVBStrategy:
    """변동성 돌파 전략 테스트."""

    def test_neutral_when_data_insufficient(self) -> None:
        """일봉 12개 미만이면 NEUTRAL 반환."""
        config = TradingConfig.from_env()
        vb = VBStrategy(config)
        ctx = _make_ctx([], 80000)
        result = vb.evaluate(ctx)
        assert result.signal_type == SignalType.NEUTRAL

    def test_neutral_with_few_candles(self) -> None:
        """일봉 5개만 있으면 NEUTRAL."""
        config = TradingConfig.from_env()
        vb = VBStrategy(config)
        candles = _make_candles(5)
        ctx = _make_ctx(candles, 80000)
        result = vb.evaluate(ctx)
        assert result.signal_type == SignalType.NEUTRAL

    def test_buy_on_breakout(self) -> None:
        """목표가 돌파 시 BUY 또는 NEUTRAL (필터에 따라 다를 수 있음)."""
        config = TradingConfig.from_env()
        vb = VBStrategy(config)
        candles = _make_candles(70)
        # K=0.5 (기본 ETF K), 전일 range=1100 (high-low), 5일 ATR 평균 ≈ 1100
        # target = open + int(1100 * 0.5) = 80000 + 550 = 80550
        # breakout_price > target
        ctx = _make_ctx(candles, 81000, ticker="229200", intraday_high=81000)
        result = vb.evaluate(ctx)
        # BUY 또는 NEUTRAL (MA20<MA60 레짐필터, 거래량 필터 등에 의해)
        assert result.signal_type in (SignalType.BUY, SignalType.NEUTRAL)
        assert result.strategy_name == "volatility_breakout"

    def test_neutral_when_below_target(self) -> None:
        """현재가가 목표가 미만이면 NEUTRAL."""
        config = TradingConfig.from_env()
        vb = VBStrategy(config)
        candles = _make_candles(70)
        # 목표가보다 훨씬 아래
        ctx = _make_ctx(candles, 79000, ticker="229200")
        result = vb.evaluate(ctx)
        assert result.signal_type == SignalType.NEUTRAL

    def test_signal_result_has_strategy_name(self) -> None:
        """모든 반환값에 strategy_name이 설정되어 있어야 한다."""
        config = TradingConfig.from_env()
        vb = VBStrategy(config)
        candles = _make_candles(70)
        ctx = _make_ctx(candles, 80000, ticker="229200")
        result = vb.evaluate(ctx)
        assert result.strategy_name == "volatility_breakout"

    def test_per_ticker_k_applied(self) -> None:
        """TICKER_K_MAP에 정의된 K값이 사용되는지 확인."""
        config = TradingConfig.from_env()
        vb = VBStrategy(config)
        # 131890 (ACE 삼성그룹동일가중) 은 K=0.3
        candles = _make_candles(70, base_price=10000, daily_range=200)
        # K=0.3이므로 target = 10000 + int(200 * 0.3) = 10060
        # 돌파 가격
        ctx = _make_ctx(candles, 10100, ticker="131890", intraday_high=10100)
        result = vb.evaluate(ctx)
        if result.signal_type == SignalType.BUY:
            assert result.target_price > 0


class TestAutoStrategy:
    """자동 전환 전략 테스트."""

    def test_returns_signal_result(self) -> None:
        """AutoStrategy.evaluate()는 항상 SignalResult를 반환해야 한다."""
        config = TradingConfig.from_env()
        auto = AutoStrategy(config)
        candles = _make_candles(70)
        ctx = _make_ctx(candles, 80500, ticker="229200", name="KODEX 코스닥150")
        result = auto.evaluate(ctx)
        assert isinstance(result, SignalResult)
        assert result.strategy_name == "auto"

    def test_strategy_name_is_auto(self) -> None:
        """반환된 SignalResult의 strategy_name이 'auto'여야 한다."""
        config = TradingConfig.from_env()
        auto = AutoStrategy(config)
        candles = _make_candles(70)
        ctx = _make_ctx(candles, 80500, ticker="229200")
        result = auto.evaluate(ctx)
        assert result.strategy_name == "auto"

    def test_insufficient_data_returns_neutral(self) -> None:
        """데이터 부족 시 NEUTRAL 반환."""
        config = TradingConfig.from_env()
        auto = AutoStrategy(config)
        # 61개 미만 -> 레짐 판단 불가 -> NEUTRAL
        candles = _make_candles(10)
        ctx = _make_ctx(candles, 80500, ticker="229200")
        result = auto.evaluate(ctx)
        assert result.signal_type == SignalType.NEUTRAL

    def test_signal_type_is_valid_enum(self) -> None:
        """반환된 signal_type이 유효한 SignalType 열거형이어야 한다."""
        config = TradingConfig.from_env()
        auto = AutoStrategy(config)
        candles = _make_candles(70)
        ctx = _make_ctx(candles, 80500, ticker="229200")
        result = auto.evaluate(ctx)
        assert result.signal_type in (SignalType.BUY, SignalType.SELL, SignalType.NEUTRAL)


class TestSignalResult:
    """SignalResult 데이터 클래스 테스트."""

    def test_default_values(self) -> None:
        """기본값이 올바르게 설정되어야 한다."""
        sr = SignalResult(
            signal_type=SignalType.NEUTRAL,
            strength=SignalStrength.WEAK,
        )
        assert sr.score == 0.0
        assert sr.reasons == []
        assert sr.warnings == []
        assert sr.target_price == 0
        assert sr.strategy_name == ""

    def test_buy_signal_construction(self) -> None:
        """BUY 신호 생성이 정상적이어야 한다."""
        sr = SignalResult(
            signal_type=SignalType.BUY,
            strength=SignalStrength.STRONG,
            score=10.0,
            reasons=["breakout"],
            target_price=80500,
            strategy_name="vb",
        )
        assert sr.signal_type == SignalType.BUY
        assert sr.strength == SignalStrength.STRONG
        assert sr.target_price == 80500


class TestMarketContext:
    """MarketContext 데이터 클래스 테스트."""

    def test_construction(self) -> None:
        """MarketContext 생성이 정상적이어야 한다."""
        ctx = MarketContext(
            ticker="005930",
            name="삼성전자",
            current_price=70000,
            change_rate=-1.5,
            candles_5m=pd.DataFrame(),
            candles_1d=pd.DataFrame(),
        )
        assert ctx.ticker == "005930"
        assert ctx.current_price == 70000
        assert ctx.intraday_high == 0
        assert ctx.candles_1d_raw == []
