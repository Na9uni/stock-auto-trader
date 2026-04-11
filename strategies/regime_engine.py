"""4-모드 레짐 엔진 — 시장 상태에 따라 매매 파라미터를 자동 조정.

RegimeState: NORMAL / SWING / DEFENSE / CASH
각 레짐마다 포지션 크기, 손절%, 슬롯 수 등을 사전 정의.
매크로 레짐(macro_regime)과 지수 등락(index_data)을 조합하여 판정.

Anti-oscillation: 상위 레짐(위험↑)은 즉시 전환, 하위(위험↓)는 쿨다운 필요.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from enum import Enum
from pathlib import Path

logger = logging.getLogger("stock_analysis")

ROOT = Path(__file__).parent.parent
_STATE_PATH = ROOT / "data" / "regime_state.json"

# ---------------------------------------------------------------------------
# 1. RegimeState enum
# ---------------------------------------------------------------------------


class RegimeState(Enum):
    """시장 레짐 상태."""
    NORMAL = "normal"       # 안정장 -> 추세추종
    SWING = "swing"         # 변동장 -> 스윙
    DEFENSE = "defense"     # 급락장 -> 방어
    CASH = "cash"           # 현금화


# 레짐 심각도 순서 (높을수록 위험)
_SEVERITY = {
    RegimeState.NORMAL: 0,
    RegimeState.SWING: 1,
    RegimeState.DEFENSE: 2,
    RegimeState.CASH: 3,
}

# ---------------------------------------------------------------------------
# 2. RegimeParams dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeParams:
    """레짐별 매매 파라미터."""

    position_size_pct: float        # 기본 포지션 비중 (1.0 = 100%)
    max_slots: int                  # 최대 동시 보유 종목 수
    stoploss_pct: float             # 손절 기준 (%)
    trailing_activate_pct: float    # 트레일링 스탑 활성화 (%)
    trailing_stop_pct: float        # 트레일링 스탑 폭 (%)
    buy_allowed: bool               # 신규 매수 허용 여부
    force_liquidate_pct: float      # 기존 포지션 강제 청산 비율 (0.0~1.0)
    eod_liquidate: bool             # 장 마감 전 전량 청산 여부


# ---------------------------------------------------------------------------
# 3. REGIME_PARAMS: RegimeState -> RegimeParams 매핑
# ---------------------------------------------------------------------------

REGIME_PARAMS: dict[RegimeState, RegimeParams] = {
    RegimeState.NORMAL: RegimeParams(
        position_size_pct=1.0,
        max_slots=2,
        stoploss_pct=2.0,
        trailing_activate_pct=2.5,
        trailing_stop_pct=1.0,
        buy_allowed=True,
        force_liquidate_pct=0.0,
        eod_liquidate=True,
    ),
    RegimeState.SWING: RegimeParams(
        position_size_pct=0.5,
        max_slots=1,
        stoploss_pct=1.5,
        trailing_activate_pct=2.0,
        trailing_stop_pct=0.8,
        buy_allowed=True,
        force_liquidate_pct=0.0,
        eod_liquidate=True,
    ),
    RegimeState.DEFENSE: RegimeParams(
        position_size_pct=0.3,
        max_slots=1,
        stoploss_pct=1.0,
        trailing_activate_pct=1.5,
        trailing_stop_pct=0.5,
        buy_allowed=False,
        force_liquidate_pct=0.5,
        eod_liquidate=True,
    ),
    RegimeState.CASH: RegimeParams(
        position_size_pct=0.0,
        max_slots=0,
        stoploss_pct=0.5,
        trailing_activate_pct=1.0,
        trailing_stop_pct=0.3,
        buy_allowed=False,
        force_liquidate_pct=1.0,
        eod_liquidate=True,
    ),
}


# ---------------------------------------------------------------------------
# 4. RegimeEngine 클래스
# ---------------------------------------------------------------------------


class RegimeEngine:
    """시장 레짐 판별 엔진.

    check_signals()에서 주기적으로 detect()를 호출하여 현재 레짐을 갱신.
    레짐 변경 시 텔레그램 알림 + JSON 상태 저장.
    """

    def __init__(self, config: object) -> None:
        """초기화.

        Args:
            config: TradingConfig 인스턴스
        """
        self._config = config
        self._current_state = RegimeState.NORMAL
        self._prev_state: RegimeState | None = None
        self._state_entered_at = datetime.now()
        self._defense_index_price: float | None = None
        self._cooldown_until: datetime | None = None
        self._transition_reason: str = ""
        self._load_state()

    # -- Properties --

    @property
    def state(self) -> RegimeState:
        """현재 레짐 상태."""
        return self._current_state

    @property
    def params(self) -> RegimeParams:
        """현재 레짐의 매매 파라미터."""
        return REGIME_PARAMS[self._current_state]

    @property
    def prev_state(self) -> RegimeState | None:
        """이전 레짐 상태."""
        return self._prev_state

    # -- Main Detection --

    def detect(self, index_data: dict, macro_status: object) -> RegimeState:
        """메인 레짐 판별. check_signals()에서 호출.

        Args:
            index_data: {"KOSPI": {"price": float, "change_pct": float}, ...}
            macro_status: MacroStatus from assess_current()

        Returns:
            현재 레짐 상태
        """
        kospi = index_data.get("KOSPI", {})
        kospi_change = kospi.get("change_pct", 0.0)
        kospi_price = kospi.get("price", 0.0)

        # config에서 임계값 가져오기
        defense_trigger = getattr(self._config, "regime_defense_trigger_pct", -2.0)
        cash_trigger = getattr(self._config, "regime_cash_trigger_pct", -3.0)

        # macro_status에서 regime과 crisis_score 추출
        macro_regime = getattr(macro_status, "regime", None)
        macro_regime_value = getattr(macro_regime, "value", "") if macro_regime else ""

        new_state = RegimeState.NORMAL
        reason = ""

        # ── Decision tree (top-down, first match wins) ──

        # CASH 조건
        if macro_regime_value == "crisis":
            new_state = RegimeState.CASH
            reason = "매크로 CRISIS 레짐 감지"
        elif (
            self._current_state == RegimeState.DEFENSE
            and self._defense_index_price is not None
            and kospi_price > 0
        ):
            drop_from_defense = (
                (kospi_price - self._defense_index_price) / self._defense_index_price * 100
            )
            if drop_from_defense <= cash_trigger:
                new_state = RegimeState.CASH
                reason = (
                    f"DEFENSE 진입 후 KOSPI 추가 {drop_from_defense:.1f}% 하락 "
                    f"(기준: {cash_trigger}%)"
                )

        # DEFENSE 조건 (CASH가 아닐 때만)
        if new_state == RegimeState.NORMAL:
            if kospi_change <= defense_trigger:
                new_state = RegimeState.DEFENSE
                reason = f"KOSPI {kospi_change:+.1f}% (기준: {defense_trigger}%)"
            elif macro_regime_value == "caution" and kospi_change <= -1.5:
                new_state = RegimeState.DEFENSE
                reason = f"매크로 CAUTION + KOSPI {kospi_change:+.1f}%"

        # SWING 조건 (DEFENSE/CASH가 아닐 때만)
        if new_state == RegimeState.NORMAL:
            # MA20 아래 근사치: 변동률이 음수이고 일정 수준 이하
            swing_volatility = getattr(
                self._config, "regime_swing_volatility_pct", 3.0
            )
            if kospi_change < -1.0:
                new_state = RegimeState.SWING
                reason = f"KOSPI {kospi_change:+.1f}% (하락 추세)"
            # crisis_score 기반
            elif hasattr(macro_status, "equity_ratio"):
                equity_ratio = getattr(macro_status, "equity_ratio", 1.0)
                # equity_ratio 0.7 이하 = crisis_score >= 1~2
                if equity_ratio <= 0.7:
                    new_state = RegimeState.SWING
                    reason = f"매크로 주식비중 {equity_ratio*100:.0f}% (위험 감지)"

        # NORMAL (default) - reason은 빈 문자열 유지

        if new_state == RegimeState.NORMAL and not reason:
            reason = "시장 정상"

        # ── Anti-oscillation: 에스컬레이션은 즉시, 디에스컬레이션은 쿨다운 ──
        new_severity = _SEVERITY[new_state]
        cur_severity = _SEVERITY[self._current_state]

        if new_severity > cur_severity:
            # 위험 상승 -> 즉시 전환
            self._transition(new_state, reason)
        elif new_severity < cur_severity:
            # 위험 하강 -> 쿨다운 체크
            if self._can_deescalate():
                self._transition(new_state, reason)
            else:
                logger.debug(
                    "[레짐] 디에스컬레이션 쿨다운 중: %s -> %s 대기",
                    self._current_state.value,
                    new_state.value,
                )
        # 동일 레짐이면 변경 없음

        return self._current_state

    # -- Internal Methods --

    def _transition(self, new_state: RegimeState, reason: str) -> None:
        """레짐 전환 + 로깅 + 텔레그램 알림 + 상태 저장."""
        if new_state == self._current_state:
            return

        old_state = self._current_state
        self._prev_state = old_state
        self._current_state = new_state
        self._state_entered_at = datetime.now()
        self._transition_reason = reason

        # DEFENSE 진입 시 지수 가격 기록 (CASH 전환 판단용)
        if new_state == RegimeState.DEFENSE:
            self._defense_index_price = None  # detect()에서 설정됨
        if new_state != RegimeState.DEFENSE:
            self._defense_index_price = None

        # 쿨다운 설정 (디에스컬레이션 방지)
        cooldown_min = getattr(
            self._config, "regime_deescalation_cooldown_min", 30
        )
        self._cooldown_until = datetime.now() + timedelta(minutes=cooldown_min)

        logger.warning(
            "[레짐 전환] %s -> %s (사유: %s)",
            old_state.value,
            new_state.value,
            reason,
        )

        # 텔레그램 알림 (실패해도 무시)
        self._send_telegram_alert(old_state, new_state, reason)

        # JSON 상태 저장
        self._save_state()

    def _can_deescalate(self) -> bool:
        """디에스컬레이션 쿨다운 확인."""
        if self._cooldown_until is None:
            return True
        return datetime.now() >= self._cooldown_until

    def _send_telegram_alert(
        self,
        old_state: RegimeState,
        new_state: RegimeState,
        reason: str,
    ) -> None:
        """레짐 전환 텔레그램 알림."""
        try:
            from alerts.telegram_notifier import TelegramNotifier

            severity_emoji = {
                RegimeState.NORMAL: "[안정]",
                RegimeState.SWING: "[주의]",
                RegimeState.DEFENSE: "[경고]",
                RegimeState.CASH: "[위험]",
            }
            msg = (
                f"{severity_emoji.get(new_state, '')} 레짐 전환\n"
                f"{old_state.value} -> {new_state.value}\n"
                f"사유: {reason}\n"
                f"매수 허용: {'O' if REGIME_PARAMS[new_state].buy_allowed else 'X'}\n"
                f"최대 슬롯: {REGIME_PARAMS[new_state].max_slots}\n"
                f"포지션 비중: {REGIME_PARAMS[new_state].position_size_pct*100:.0f}%"
            )
            notifier = TelegramNotifier()
            notifier.send_message(msg)
        except Exception as exc:
            logger.debug("[레짐] 텔레그램 알림 실패 (무시): %s", exc)

    def _save_state(self) -> None:
        """data/regime_state.json에 현재 상태 atomic write."""
        data = {
            "state": self._current_state.value,
            "prev_state": self._prev_state.value if self._prev_state else None,
            "entered_at": self._state_entered_at.isoformat(),
            "defense_index_price": self._defense_index_price,
            "cooldown_until": (
                self._cooldown_until.isoformat() if self._cooldown_until else None
            ),
            "reason": self._transition_reason,
            "date": date.today().isoformat(),
        }
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(_STATE_PATH.parent), suffix=".tmp", prefix=".regime_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(_STATE_PATH))
        except Exception:
            # 실패 시 임시 파일 정리
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _load_state(self) -> None:
        """data/regime_state.json에서 상태 복원.

        12시간 이상 경과했거나 다른 날짜면 NORMAL로 리셋.
        """
        if not _STATE_PATH.exists():
            logger.info("[레짐] 상태 파일 없음 -> NORMAL 시작")
            return

        try:
            with open(_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("[레짐] 상태 파일 로드 실패: %s -> NORMAL", exc)
            return

        # stale check: 날짜 다르거나 12시간 경과
        saved_date = data.get("date", "")
        if saved_date != date.today().isoformat():
            logger.info("[레짐] 날짜 변경 (%s) -> NORMAL 리셋", saved_date)
            return

        entered_str = data.get("entered_at", "")
        if entered_str:
            try:
                entered_at = datetime.fromisoformat(entered_str)
                if (datetime.now() - entered_at).total_seconds() > 12 * 3600:
                    logger.info("[레짐] 12시간 경과 -> NORMAL 리셋")
                    return
            except (ValueError, TypeError):
                pass

        # 복원
        state_str = data.get("state", "normal")
        try:
            self._current_state = RegimeState(state_str)
        except ValueError:
            self._current_state = RegimeState.NORMAL

        prev_str = data.get("prev_state")
        if prev_str:
            try:
                self._prev_state = RegimeState(prev_str)
            except ValueError:
                self._prev_state = None

        if entered_str:
            try:
                self._state_entered_at = datetime.fromisoformat(entered_str)
            except (ValueError, TypeError):
                self._state_entered_at = datetime.now()

        self._defense_index_price = data.get("defense_index_price")
        self._transition_reason = data.get("reason", "")

        cooldown_str = data.get("cooldown_until")
        if cooldown_str:
            try:
                self._cooldown_until = datetime.fromisoformat(cooldown_str)
            except (ValueError, TypeError):
                self._cooldown_until = None

        logger.info(
            "[레짐] 상태 복원: %s (사유: %s)",
            self._current_state.value,
            self._transition_reason,
        )


# ---------------------------------------------------------------------------
# 5. Singleton
# ---------------------------------------------------------------------------

_engine: RegimeEngine | None = None


def get_regime_engine() -> RegimeEngine:
    """싱글톤 RegimeEngine 인스턴스 반환."""
    global _engine
    if _engine is None:
        from config.trading_config import TradingConfig

        _engine = RegimeEngine(TradingConfig.from_env())
    return _engine
