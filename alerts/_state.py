"""공유 상태 모듈 — 설정 상수, 전략 객체, 인메모리 상태.

signal_runner / order_manager / analysis_scheduler 가 공통으로 참조하는
설정값과 상태를 한곳에 모아 순환 임포트를 방지한다.
이 모듈은 signal_runner, order_manager 를 임포트하지 않는다.
"""

from __future__ import annotations

import logging

from config.trading_config import TradingConfig
from strategies.vb_strategy import VBStrategy
from strategies.score_strategy import ScoreStrategy
from strategies.combo_strategy import ComboStrategy
from strategies.trend_strategy import TrendStrategy
from strategies.auto_strategy import AutoStrategy

# ---------------------------------------------------------------------------
# 로거
# ---------------------------------------------------------------------------

logger = logging.getLogger("stock_analysis")

# ---------------------------------------------------------------------------
# 통합 설정 (TradingConfig 에서 로드)
# ---------------------------------------------------------------------------

_TRADING_CONFIG = TradingConfig.from_env()

OPERATION_MODE = _TRADING_CONFIG.operation_mode
MOCK_MODE = _TRADING_CONFIG.mock_mode
AUTO_TRADE_ENABLED = _TRADING_CONFIG.auto_trade_enabled

AUTO_TRADE_AMOUNT = _TRADING_CONFIG.auto_trade_amount
MAX_ORDER_AMOUNT = _TRADING_CONFIG.max_order_amount
MAX_SLOTS = _TRADING_CONFIG.max_slots

STOPLOSS_PCT = _TRADING_CONFIG.stoploss_pct
TRAILING_ACTIVATE_PCT = _TRADING_CONFIG.trailing_activate_pct
TRAILING_STOP_PCT = _TRADING_CONFIG.trailing_stop_pct

MAX_MONTHLY_LOSS = _TRADING_CONFIG.max_monthly_loss
MAX_CONSEC_STOPLOSS = _TRADING_CONFIG.max_consec_stoploss
MAX_DAILY_LOSS = _TRADING_CONFIG.max_daily_loss

# ---------------------------------------------------------------------------
# 전략 객체
# ---------------------------------------------------------------------------

def _build_strategy():
    """설정에 따라 전략 객체를 생성."""
    strategy_name = _TRADING_CONFIG.strategy
    if strategy_name == "vb":
        return VBStrategy(_TRADING_CONFIG)
    elif strategy_name == "score":
        return ScoreStrategy(_TRADING_CONFIG)
    elif strategy_name == "combo":
        return ComboStrategy(_TRADING_CONFIG)
    elif strategy_name == "trend":
        return TrendStrategy(_TRADING_CONFIG)
    elif strategy_name == "auto":
        return AutoStrategy(_TRADING_CONFIG)
    else:
        logger.warning("알 수 없는 전략 '%s' → combo 사용", strategy_name)
        return ComboStrategy(_TRADING_CONFIG)


_STRATEGY = _build_strategy()
logger.info("전략: %s", _STRATEGY.name)

# ---------------------------------------------------------------------------
# 기타 상수
# ---------------------------------------------------------------------------

INTEREST_SPIKE_THRESHOLD = 3.0
MAX_SELL_FAIL_BEFORE_REMOVE = 5


# ---------------------------------------------------------------------------
# 공통 헬퍼: TRADING_STYLE 정규화 (intent 필드 값 = EOD 청산 판정 master key)
# ---------------------------------------------------------------------------

_VALID_TRADING_STYLES = ("daytrading", "swing")


def get_trading_intent() -> str:
    """환경변수 TRADING_STYLE을 정규화하여 포지션 intent 값으로 반환.

    - "daytrading" / "swing"만 유효. 그 외(오타 포함)면 warning 로그 + swing fallback.
    - 반환값은 포지션 dict의 "intent" 필드, EOD 청산 판정의 master key.
    """
    import os
    raw = (os.getenv("TRADING_STYLE", "swing") or "swing").strip().lower()
    if raw in _VALID_TRADING_STYLES:
        return raw
    logger.warning(
        "[설정] TRADING_STYLE 값 '%s'는 유효하지 않음 (허용: %s) → swing으로 fallback",
        raw, _VALID_TRADING_STYLES,
    )
    return "swing"


def derive_eod_liquidation_from_style() -> bool:
    """TRADING_STYLE → EOD 청산 여부 자동 파생.

    - daytrading → True (하루 마감 시 전부 청산)
    - swing → False (다음 날로 보유)
    - TradingConfig.eod_liquidation이 명시 설정되어 있으면 그 값이 우선함(별도 처리).
    """
    return get_trading_intent() == "daytrading"

# ---------------------------------------------------------------------------
# 인메모리 상태 변수
# ---------------------------------------------------------------------------

_notified_orders: set[str] = set()
_order_init_done: bool = False
