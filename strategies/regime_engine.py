"""4-모드 레짐 엔진 — 시장 상태에 따라 매매 파라미터를 자동 조정.

RegimeState: NORMAL / SWING / DEFENSE / CASH
각 레짐마다 포지션 크기, 손절%, 슬롯 수 등을 사전 정의.
매크로 레짐(macro_regime)과 지수 등락(index_data)을 조합하여 판정.

Anti-oscillation: 상위 레짐(위험↑)은 즉시 전환, 하위(위험↓)는 쿨다운 필요.
"""

from __future__ import annotations

import json
import logging
import math
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
    ),
    RegimeState.SWING: RegimeParams(
        position_size_pct=0.5,
        max_slots=1,
        stoploss_pct=1.5,
        trailing_activate_pct=2.0,
        trailing_stop_pct=0.8,
        buy_allowed=True,
        force_liquidate_pct=0.0,
    ),
    RegimeState.DEFENSE: RegimeParams(
        position_size_pct=0.3,
        max_slots=1,
        stoploss_pct=1.0,
        trailing_activate_pct=1.5,
        trailing_stop_pct=0.5,
        buy_allowed=False,
        force_liquidate_pct=0.5,
    ),
    RegimeState.CASH: RegimeParams(
        position_size_pct=0.0,
        max_slots=0,
        stoploss_pct=0.5,
        trailing_activate_pct=1.0,
        trailing_stop_pct=0.3,
        buy_allowed=False,
        force_liquidate_pct=1.0,
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

    def __init__(self, config: object, silent: bool = False) -> None:
        """초기화.

        Args:
            config: TradingConfig 인스턴스
            silent: True이면 텔레그램 알림/상태 저장 비활성화 (테스트용)
        """
        self._config = config
        self._silent = silent
        self._current_state = RegimeState.NORMAL
        self._prev_state: RegimeState | None = None
        self._state_entered_at = datetime.now()
        self._defense_index_price: float | None = None
        self._cooldown_until: datetime | None = None
        self._transition_reason: str = ""
        self._recent_changes: list[tuple[str, float]] = []
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

    def detect(
        self,
        index_data: dict,
        macro_status: object,
        kospi_candles: list[dict] | None = None,
    ) -> RegimeState:
        """메인 레짐 판별. check_signals()에서 호출.

        Args:
            index_data: {"KOSPI": {"price": float, "change_pct": float}, ...}
            macro_status: MacroStatus from assess_current()
            kospi_candles: KOSPI ETF(069500) 일봉 리스트 (ATR/전일고저 조기 감지용)

        Returns:
            현재 레짐 상태
        """
        # 지수 데이터 추출 (KOSPI + KOSDAQ 모두 확인)
        kospi = index_data.get("KOSPI", {})
        kosdaq = index_data.get("KOSDAQ", {})
        kospi_change = kospi.get("change_pct", 0.0)
        kospi_price = kospi.get("price", 0.0)
        kosdaq_change = kosdaq.get("change_pct", 0.0)

        # M6: NaN 처리
        if math.isnan(kospi_change): kospi_change = 0.0
        if math.isnan(kosdaq_change): kosdaq_change = 0.0

        # 두 지수 중 더 큰 하락폭 사용
        worst_index_change = min(kospi_change, kosdaq_change)

        # index_data가 비었으면 fail-safe: 현재 상태 유지 (fail-closed)
        # 빈 데이터로 _recent_changes를 오염시키지 않기 위해 여기서 먼저 체크
        if not index_data or "KOSPI" not in index_data:
            logger.warning("[레짐] 지수 데이터 없음 — 현재 상태(%s) 유지", self._current_state.value)
            return self._current_state

        # C1: 누적 하락 추적 (날짜별 1회만 기록 — 매분 호출돼도 당일 최악값만 갱신)
        today_str = date.today().isoformat()
        if self._recent_changes and self._recent_changes[-1][0] == today_str:
            # 당일 이미 기록됨 → 더 나쁜 값으로 갱신
            prev_val = self._recent_changes[-1][1]
            self._recent_changes[-1] = (today_str, min(prev_val, worst_index_change))
        else:
            # 새 날짜 → 추가
            self._recent_changes.append((today_str, worst_index_change))
            self._recent_changes = self._recent_changes[-5:]  # 최근 5일만 유지

        # config에서 임계값 가져오기
        defense_trigger = getattr(self._config, "regime_defense_trigger_pct", -2.0)
        cash_trigger = getattr(self._config, "regime_cash_trigger_pct", -3.0)

        # macro_status에서 regime과 crisis_score 추출
        macro_regime = getattr(macro_status, "regime", None)
        macro_regime_value = getattr(macro_regime, "value", "") if macro_regime else ""

        # H4: US 야간 지수 데이터 추출
        sp500 = index_data.get("S&P500", {})
        nasdaq = index_data.get("NASDAQ", {})
        sp500_change = sp500.get("change_pct", 0.0)
        nasdaq_change = nasdaq.get("change_pct", 0.0)
        if math.isnan(sp500_change): sp500_change = 0.0
        if math.isnan(nasdaq_change): nasdaq_change = 0.0
        worst_us_change = min(sp500_change, nasdaq_change)

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

        # C1: 누적 하락 감지 (느린 하락장 방어) — 날짜별 최악값의 합산
        cumulative_5d = sum(v for _, v in self._recent_changes[-5:]) if len(self._recent_changes) >= 3 else 0
        if cumulative_5d <= -8.0 and new_state == RegimeState.NORMAL:
            new_state = RegimeState.DEFENSE
            reason = f"5일 누적 하락 {cumulative_5d:.1f}% (기준: -8%)"
        elif cumulative_5d <= -5.0 and new_state == RegimeState.NORMAL:
            new_state = RegimeState.SWING
            reason = f"5일 누적 하락 {cumulative_5d:.1f}% (기준: -5%)"

        # DEFENSE 조건 (CASH가 아닐 때만) — KOSPI/KOSDAQ 중 더 큰 하락 사용
        if new_state == RegimeState.NORMAL:
            if worst_index_change <= defense_trigger:
                idx_name = "KOSDAQ" if kosdaq_change < kospi_change else "KOSPI"
                new_state = RegimeState.DEFENSE
                reason = f"{idx_name} {worst_index_change:+.1f}% (기준: {defense_trigger}%)"
            elif macro_regime_value == "caution" and worst_index_change <= -1.5:
                new_state = RegimeState.DEFENSE
                reason = f"매크로 CAUTION + 지수 {worst_index_change:+.1f}%"

        # H4: US 야간 급락 반영
        if worst_us_change <= -5.0 and new_state in (RegimeState.NORMAL, RegimeState.SWING):
            new_state = RegimeState.DEFENSE
            reason = f"US 야간 급등락 (S&P/NASDAQ {worst_us_change:+.1f}%)"
        elif worst_us_change <= -3.0 and new_state == RegimeState.NORMAL:
            new_state = RegimeState.SWING
            reason = f"US 야간 급락 (S&P/NASDAQ {worst_us_change:+.1f}%)"

        # ── 조기 감지: ATR 급팽창 + 전일 저점 이탈 ──
        if kospi_candles and len(kospi_candles) >= 10 and new_state == RegimeState.NORMAL:
            try:
                import pandas as pd
                _cdf = pd.DataFrame(kospi_candles)
                for _col in ("open", "high", "low", "close"):
                    _cdf[_col] = pd.to_numeric(_cdf[_col], errors="coerce")
                _cdf = _cdf.dropna(subset=["high", "low", "close"])
                if len(_cdf) >= 10:
                    # ATR 급팽창: 당일 ATR vs 10일 평균 ATR
                    _ranges = _cdf["high"] - _cdf["low"]
                    _atr_10 = float(_ranges.iloc[-11:-1].mean())  # 전일까지 10일 평균
                    _atr_today = float(_ranges.iloc[-1])           # 당일 range
                    _atr_ratio = _atr_today / _atr_10 if _atr_10 > 0 else 1.0

                    # ATR 2배 이상 팽창 → SWING (변동성 급증 = 레짐 변화 전조)
                    if _atr_ratio >= 2.5 and new_state == RegimeState.NORMAL:
                        new_state = RegimeState.DEFENSE
                        reason = f"ATR 급팽창 {_atr_ratio:.1f}배 (기준: 2.5배)"
                    elif _atr_ratio >= 1.8 and new_state == RegimeState.NORMAL:
                        new_state = RegimeState.SWING
                        reason = f"ATR 팽창 {_atr_ratio:.1f}배 (기준: 1.8배)"

                    # 전일 저점 이탈: 당일 종가가 전일 저점 아래 → SWING
                    if len(_cdf) >= 2 and new_state == RegimeState.NORMAL:
                        _prev_low = float(_cdf["low"].iloc[-2])
                        _today_close = float(_cdf["close"].iloc[-1])
                        if _prev_low > 0 and _today_close < _prev_low:
                            _break_pct = (_today_close - _prev_low) / _prev_low * 100
                            new_state = RegimeState.SWING
                            reason = f"전일 저점 이탈 ({_today_close:,.0f} < {_prev_low:,.0f}, {_break_pct:+.1f}%)"
            except Exception as e:
                logger.debug("[레짐] ATR/전일고저 계산 실패 (무시): %s", e)

        # SWING 조건 (DEFENSE/CASH가 아닐 때만)
        if new_state == RegimeState.NORMAL:
            # 히스테리시스: SWING 진입은 -1.5%, 복귀는 -0.5% 이상 회복
            swing_entry_threshold = -1.5
            if worst_index_change <= swing_entry_threshold:
                new_state = RegimeState.SWING
                reason = f"지수 {worst_index_change:+.1f}% (SWING 기준: {swing_entry_threshold}%)"
            elif macro_regime_value == "caution":
                new_state = RegimeState.SWING
                reason = f"매크로 CAUTION (위험 감지)"

        # NORMAL (default) - reason은 빈 문자열 유지

        if new_state == RegimeState.NORMAL and not reason:
            reason = "시장 정상"

        # ── Anti-oscillation: 에스컬레이션은 즉시, 디에스컬레이션은 쿨다운 ──
        new_severity = _SEVERITY[new_state]
        cur_severity = _SEVERITY[self._current_state]

        if new_severity > cur_severity:
            # 위험 상승 -> 즉시 전환
            # DEFENSE 진입 시 현재 KOSPI 가격 기록 (CASH 에스컬레이션용)
            if new_state == RegimeState.DEFENSE and kospi_price > 0:
                self._defense_index_price = kospi_price
            self._transition(new_state, reason)
        elif new_severity < cur_severity:
            # 위험 하강 -> 쿨다운 체크
            # M8: SWING → NORMAL 히스테리시스 (-0.5% 이상 회복 필요)
            if (
                self._current_state == RegimeState.SWING
                and new_state == RegimeState.NORMAL
                and worst_index_change < -0.5
            ):
                logger.debug(
                    "[레짐] SWING→NORMAL 히스테리시스: 지수 %+.1f%% < -0.5%%, 유지",
                    worst_index_change,
                )
            elif self._can_deescalate():
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
        # detect()에서 전달받은 kospi_price를 _transition 호출 전에 설정해야 하므로
        # detect()에서 직접 설정하도록 함
        if new_state != RegimeState.DEFENSE:
            self._defense_index_price = None

        # 쿨다운 설정 (디에스컬레이션에만 적용, 에스컬레이션은 쿨다운 안 걸음)
        new_sev = _SEVERITY[new_state]
        old_sev = _SEVERITY[old_state]
        if new_sev < old_sev:
            # 디에스컬레이션 → 쿨다운 설정
            cooldown_min = getattr(
                self._config, "regime_deescalation_cooldown_min", 30
            )
            # CASH 탈출은 더 긴 쿨다운 (60분)
            if old_state == RegimeState.CASH:
                cooldown_min = max(cooldown_min, 60)
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
        if self._silent:
            return
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
        if self._silent:
            return
        data = {
            "state": self._current_state.value,
            "prev_state": self._prev_state.value if self._prev_state else None,
            "entered_at": self._state_entered_at.isoformat(),
            "defense_index_price": self._defense_index_price,
            "cooldown_until": (
                self._cooldown_until.isoformat() if self._cooldown_until else None
            ),
            "reason": self._transition_reason,
            "recent_changes": self._recent_changes,
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
        if self._silent:
            return
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
        raw_changes = data.get("recent_changes", [])
        self._recent_changes = [tuple(x) if isinstance(x, list) else x for x in raw_changes]

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
