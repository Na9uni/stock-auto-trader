"""레짐 엔진 테스트 — 4-모드 전환 로직 검증."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from strategies.regime_engine import RegimeEngine, RegimeState, REGIME_PARAMS
from strategies.macro_regime import MacroStatus, MacroRegime
from config.trading_config import TradingConfig


class TestRegimeEngine:
    """RegimeEngine 핵심 로직 테스트."""

    def _make_engine(self) -> RegimeEngine:
        """매 테스트마다 새 엔진 생성. 상태 파일 로드를 우회한다."""
        config = TradingConfig.from_env()
        with patch.object(RegimeEngine, "_load_state"):
            engine = RegimeEngine(config)
        # _load_state가 스킵되었으므로 초기값 수동 설정
        engine._current_state = RegimeState.NORMAL
        engine._prev_state = None
        engine._state_entered_at = datetime.now()
        engine._defense_index_price = None
        engine._cooldown_until = None
        engine._transition_reason = ""
        engine._recent_changes = []
        return engine

    def _macro_ok(self) -> MacroStatus:
        """정상 매크로 상태."""
        return MacroStatus(
            regime=MacroRegime.NORMAL,
            reasons=[],
            equity_ratio=1.0,
            allowed_strategies=[],
        )

    # ── 초기 상태 ──

    def test_initial_state_is_normal(self) -> None:
        e = self._make_engine()
        assert e.state == RegimeState.NORMAL

    # ── DEFENSE 전환 ──

    def test_kospi_minus_2_triggers_defense(self) -> None:
        e = self._make_engine()
        r = e.detect(
            {"KOSPI": {"price": 2500, "change_pct": -2.5},
             "KOSDAQ": {"price": 800, "change_pct": -1.0}},
            self._macro_ok(),
        )
        assert r == RegimeState.DEFENSE

    def test_defense_index_price_recorded(self) -> None:
        e = self._make_engine()
        e.detect(
            {"KOSPI": {"price": 2500, "change_pct": -2.5},
             "KOSDAQ": {"price": 800, "change_pct": -1.0}},
            self._macro_ok(),
        )
        assert e._defense_index_price == 2500

    def test_kosdaq_only_crash_triggers_defense(self) -> None:
        """KOSDAQ만 급락해도 DEFENSE 진입해야 한다."""
        e = self._make_engine()
        r = e.detect(
            {"KOSPI": {"price": 2600, "change_pct": 0.5},
             "KOSDAQ": {"price": 750, "change_pct": -3.0}},
            self._macro_ok(),
        )
        assert r == RegimeState.DEFENSE

    # ── DEFENSE -> CASH 에스컬레이션 ──

    def test_defense_to_cash_on_further_drop(self) -> None:
        e = self._make_engine()
        # 1단계: DEFENSE 진입
        e.detect(
            {"KOSPI": {"price": 2500, "change_pct": -2.5},
             "KOSDAQ": {"price": 800, "change_pct": -1.0}},
            self._macro_ok(),
        )
        assert e.state == RegimeState.DEFENSE
        # 2단계: KOSPI가 DEFENSE 진입 가격(2500) 대비 -3% 이상 추가 하락
        # cash_trigger_pct 기본값 = -3.0
        # drop_from_defense = (2400 - 2500) / 2500 * 100 = -4.0% <= -3.0
        r = e.detect(
            {"KOSPI": {"price": 2400, "change_pct": -0.5},
             "KOSDAQ": {"price": 780, "change_pct": -0.3}},
            self._macro_ok(),
        )
        assert r == RegimeState.CASH

    # ── 매크로 CRISIS ──

    def test_macro_crisis_triggers_cash(self) -> None:
        e = self._make_engine()
        crisis = MacroStatus(
            regime=MacroRegime.CRISIS,
            reasons=["war"],
            equity_ratio=0.5,
            allowed_strategies=[],
        )
        r = e.detect(
            {"KOSPI": {"price": 2600, "change_pct": 0.5},
             "KOSDAQ": {"price": 800, "change_pct": 0.3}},
            crisis,
        )
        assert r == RegimeState.CASH

    # ── 빈 데이터 / NaN 안전 처리 ──

    def test_empty_index_data_keeps_current_state(self) -> None:
        e = self._make_engine()
        r = e.detect({}, self._macro_ok())
        assert r == RegimeState.NORMAL

    def test_nan_change_pct_handled(self) -> None:
        """NaN change_pct가 들어와도 크래시하지 않아야 한다."""
        e = self._make_engine()
        r = e.detect(
            {"KOSPI": {"price": 2600, "change_pct": float("nan")},
             "KOSDAQ": {"price": 800, "change_pct": 0.3}},
            self._macro_ok(),
        )
        assert r == RegimeState.NORMAL

    # ── SWING 전환 ──

    def test_swing_threshold(self) -> None:
        """지수 -1.5% 이하면 SWING으로 전환."""
        e = self._make_engine()
        r = e.detect(
            {"KOSPI": {"price": 2580, "change_pct": -1.6},
             "KOSDAQ": {"price": 790, "change_pct": -1.2}},
            self._macro_ok(),
        )
        assert r == RegimeState.SWING

    def test_us_overnight_crash_triggers_swing(self) -> None:
        """US 야간 급락 (-3% 이상) 시 SWING 전환."""
        e = self._make_engine()
        r = e.detect(
            {"KOSPI": {"price": 2600, "change_pct": 0.5},
             "KOSDAQ": {"price": 800, "change_pct": 0.3},
             "S&P500": {"price": 4800, "change_pct": -4.0},
             "NASDAQ": {"price": 15000, "change_pct": -3.5}},
            self._macro_ok(),
        )
        # worst_us_change = min(-4.0, -3.5) = -4.0, -4.0 <= -3.0 -> SWING
        assert r == RegimeState.SWING

    # ── 디에스컬레이션 쿨다운 ──

    def test_deescalation_cooldown(self) -> None:
        """디에스컬레이션 후 쿨다운이 설정되어 연속 디에스컬레이션이 차단된다.

        _transition()은 디에스컬레이션 *완료 후* 쿨다운을 설정한다.
        따라서 첫 번째 디에스컬레이션은 허용되지만, 연속 디에스컬레이션은 차단.
        예: CASH -> DEFENSE (쿨다운 설정) -> 즉시 NORMAL 시도 -> 쿨다운으로 차단
        """
        e = self._make_engine()
        # 1. CASH 상태로 강제 설정
        e._current_state = RegimeState.CASH
        # 2. 첫 디에스컬레이션: CASH -> DEFENSE (성공, 쿨다운 설정됨)
        e.detect(
            {"KOSPI": {"price": 2500, "change_pct": -2.5},
             "KOSDAQ": {"price": 800, "change_pct": -1.0}},
            self._macro_ok(),
        )
        assert e.state == RegimeState.DEFENSE
        # 쿨다운이 설정되었는지 확인
        assert e._cooldown_until is not None
        # 3. 즉시 NORMAL 시도 -> 쿨다운으로 차단되어 DEFENSE 유지
        r = e.detect(
            {"KOSPI": {"price": 2600, "change_pct": 0.5},
             "KOSDAQ": {"price": 800, "change_pct": 0.3}},
            self._macro_ok(),
        )
        assert r == RegimeState.DEFENSE

    def test_deescalation_after_cooldown_expires(self) -> None:
        """쿨다운 만료 후에는 디에스컬레이션이 가능해야 한다."""
        e = self._make_engine()
        # DEFENSE 진입
        e.detect(
            {"KOSPI": {"price": 2500, "change_pct": -2.5},
             "KOSDAQ": {"price": 800, "change_pct": -1.0}},
            self._macro_ok(),
        )
        assert e.state == RegimeState.DEFENSE
        # 쿨다운 강제 만료
        e._cooldown_until = datetime.now() - timedelta(minutes=1)
        r = e.detect(
            {"KOSPI": {"price": 2600, "change_pct": 0.5},
             "KOSDAQ": {"price": 800, "change_pct": 0.3}},
            self._macro_ok(),
        )
        assert r == RegimeState.NORMAL

    # ── REGIME_PARAMS 일관성 ──

    def test_regime_params_consistency(self) -> None:
        """모든 RegimeState에 대해 REGIME_PARAMS가 정의되어 있어야 한다."""
        for state in RegimeState:
            assert state in REGIME_PARAMS, f"{state} has no REGIME_PARAMS entry"
            p = REGIME_PARAMS[state]
            assert 0.0 <= p.position_size_pct <= 1.0
            assert p.max_slots >= 0

    def test_cash_regime_blocks_buy(self) -> None:
        """CASH 레짐은 buy_allowed=False, max_slots=0이어야 한다."""
        p = REGIME_PARAMS[RegimeState.CASH]
        assert p.buy_allowed is False
        assert p.max_slots == 0
        assert p.force_liquidate_pct == 1.0

    def test_defense_regime_blocks_buy(self) -> None:
        """DEFENSE 레짐은 buy_allowed=False여야 한다."""
        p = REGIME_PARAMS[RegimeState.DEFENSE]
        assert p.buy_allowed is False

    def test_normal_regime_allows_buy(self) -> None:
        """NORMAL 레짐은 buy_allowed=True여야 한다."""
        p = REGIME_PARAMS[RegimeState.NORMAL]
        assert p.buy_allowed is True

    # ── 에스컬레이션은 쿨다운 없이 즉시 ──

    def test_escalation_ignores_cooldown(self) -> None:
        """SWING -> DEFENSE 에스컬레이션은 쿨다운 무시하고 즉시 전환."""
        e = self._make_engine()
        # SWING 진입
        e.detect(
            {"KOSPI": {"price": 2580, "change_pct": -1.6},
             "KOSDAQ": {"price": 790, "change_pct": -1.2}},
            self._macro_ok(),
        )
        assert e.state == RegimeState.SWING
        # 즉시 DEFENSE 진입 (쿨다운 상관없이)
        r = e.detect(
            {"KOSPI": {"price": 2500, "change_pct": -2.5},
             "KOSDAQ": {"price": 800, "change_pct": -1.0}},
            self._macro_ok(),
        )
        assert r == RegimeState.DEFENSE

    # ── US 야간 급락 -5% 이상은 DEFENSE ──

    def test_us_severe_crash_triggers_defense(self) -> None:
        """US 야간 -5% 이상 급락 시 DEFENSE 전환."""
        e = self._make_engine()
        r = e.detect(
            {"KOSPI": {"price": 2600, "change_pct": 0.5},
             "KOSDAQ": {"price": 800, "change_pct": 0.3},
             "S&P500": {"price": 4500, "change_pct": -5.5},
             "NASDAQ": {"price": 14000, "change_pct": -6.0}},
            self._macro_ok(),
        )
        assert r == RegimeState.DEFENSE
