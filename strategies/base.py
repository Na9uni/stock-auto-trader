"""전략 프로토콜 — 모든 전략이 구현해야 하는 인터페이스."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

import pandas as pd


class SignalType(Enum):
    BUY = "buy"
    SELL = "sell"
    NEUTRAL = "neutral"


class SignalStrength(Enum):
    STRONG = "strong"
    MEDIUM = "medium"
    WEAK = "weak"


@dataclass
class SignalResult:
    """통합 신호 결과."""
    signal_type: SignalType
    strength: SignalStrength
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 선택 필드 (기존 호환)
    rsi: float = float("nan")
    macd_cross: str | None = None
    vol_ratio: float = float("nan")
    # 변동성 돌파 전용
    target_price: int = 0
    strategy_name: str = ""
    # 실제 신호를 생성한 기반 전략 이름.
    # AutoStrategy 같은 dispatcher가 strategy_name을 자기 이름("auto")으로 덮어써도
    # 원본 서브전략(vb/trend/crisis_mr)은 이 필드로 보존되어 하위 로직(저점 필터 등)이
    # 기반 전략 특성에 맞게 분기할 수 있다.
    underlying_strategy: str = ""


@dataclass
class MarketContext:
    """전략에 전달되는 시장 데이터 컨텍스트 (불변 취급)."""
    ticker: str
    name: str
    current_price: int
    change_rate: float
    candles_5m: pd.DataFrame       # 5분봉 (지표 포함)
    candles_1d: pd.DataFrame       # 일봉 (지표 포함)
    exec_strength: float = 0.0
    orderbook: dict | None = None
    intraday_high: int = 0             # 당일 장중 고가 (VB 돌파 판단용)
    # 캔들 원본 (list[dict]) — 변동성 돌파용
    candles_1d_raw: list[dict] = field(default_factory=list)


class Strategy(Protocol):
    """전략 프로토콜. 모든 전략은 이 인터페이스를 구현한다."""

    name: str

    def evaluate(self, ctx: MarketContext) -> SignalResult:
        """시장 데이터를 평가하여 매매 신호를 반환한다."""
        ...
