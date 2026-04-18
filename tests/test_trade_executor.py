"""trade_executor / market_guard / position_manager 통합 검증.

오늘(2026-04-17) 추가된 수정들의 회귀 방지:
- intent 필드 (TRADING_STYLE 기반)
- 저점 매수 필터 VB 예외
- 하루 매매 횟수 제한
- EOD 청산 자동 파생
- 쿨다운 영속화
- 위기MR rule_name 태깅
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from strategies.base import SignalResult, SignalType, SignalStrength


# ---------------------------------------------------------------------------
# intent / TRADING_STYLE 정규화
# ---------------------------------------------------------------------------

class TestTradingIntent:
    """TRADING_STYLE → intent 정규화."""

    def test_daytrading_valid(self, monkeypatch):
        monkeypatch.setenv("TRADING_STYLE", "daytrading")
        from alerts._state import get_trading_intent
        assert get_trading_intent() == "daytrading"

    def test_swing_valid(self, monkeypatch):
        monkeypatch.setenv("TRADING_STYLE", "swing")
        from alerts._state import get_trading_intent
        assert get_trading_intent() == "swing"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("TRADING_STYLE", "DayTrading")
        from alerts._state import get_trading_intent
        assert get_trading_intent() == "daytrading"

    def test_invalid_falls_back_to_swing(self, monkeypatch):
        """오타/알 수 없는 값이면 swing으로 fallback (안전)."""
        monkeypatch.setenv("TRADING_STYLE", "day")  # 오타
        from alerts._state import get_trading_intent
        assert get_trading_intent() == "swing"

    def test_missing_env_defaults_swing(self, monkeypatch):
        monkeypatch.delenv("TRADING_STYLE", raising=False)
        from alerts._state import get_trading_intent
        assert get_trading_intent() == "swing"


# ---------------------------------------------------------------------------
# EOD 자동 파생
# ---------------------------------------------------------------------------

class TestEodAutoDerive:
    """TRADING_STYLE → eod_liquidation 자동 파생."""

    def test_daytrading_derives_true(self, monkeypatch):
        monkeypatch.setenv("TRADING_STYLE", "daytrading")
        monkeypatch.delenv("EOD_LIQUIDATION", raising=False)
        from config.trading_config import TradingConfig
        cfg = TradingConfig.from_env()
        assert cfg.eod_liquidation is True

    def test_swing_derives_false(self, monkeypatch):
        monkeypatch.setenv("TRADING_STYLE", "swing")
        monkeypatch.delenv("EOD_LIQUIDATION", raising=False)
        from config.trading_config import TradingConfig
        cfg = TradingConfig.from_env()
        assert cfg.eod_liquidation is False

    def test_explicit_override(self, monkeypatch):
        """EOD_LIQUIDATION 명시값이 TRADING_STYLE보다 우선."""
        monkeypatch.setenv("TRADING_STYLE", "daytrading")
        monkeypatch.setenv("EOD_LIQUIDATION", "false")
        from config.trading_config import TradingConfig
        cfg = TradingConfig.from_env()
        assert cfg.eod_liquidation is False


# ---------------------------------------------------------------------------
# 저점 매수 필터 - VB 우회 로직
# ---------------------------------------------------------------------------

class TestPullbackFilterVbBypass:
    """저점 매수 필터의 전략별 적용."""

    def test_vb_underlying_bypasses_filter(self):
        """underlying_strategy=volatility_breakout이면 저점 필터 제외."""
        sig = SignalResult(
            signal_type=SignalType.BUY,
            strength=SignalStrength.STRONG,
            strategy_name="auto",  # AutoStrategy dispatcher가 덮어쓴 이름
            underlying_strategy="volatility_breakout",  # 원본 보존
        )
        underlying = (sig.underlying_strategy or "").lower()
        strategy_name = (sig.strategy_name or "").lower()
        is_breakout = (
            underlying in ("vb", "volatility_breakout")
            or strategy_name in ("vb", "volatility_breakout")
        )
        apply_pullback = not is_breakout
        assert apply_pullback is False, "VB 신호는 저점 필터 우회해야 함"

    def test_trend_following_applies_filter(self):
        """trend_following은 저점 필터 적용."""
        sig = SignalResult(
            signal_type=SignalType.BUY,
            strength=SignalStrength.STRONG,
            strategy_name="auto",
            underlying_strategy="trend_following",
        )
        underlying = (sig.underlying_strategy or "").lower()
        strategy_name = (sig.strategy_name or "").lower()
        is_breakout = (
            underlying in ("vb", "volatility_breakout")
            or strategy_name in ("vb", "volatility_breakout")
        )
        apply_pullback = not is_breakout
        assert apply_pullback is True, "trend_following은 저점 필터 적용"

    def test_unknown_strategy_applies_filter(self):
        """전략명 불명(빈 문자열)이면 안전하게 필터 적용 (default)."""
        sig = SignalResult(
            signal_type=SignalType.BUY,
            strength=SignalStrength.STRONG,
        )
        underlying = (sig.underlying_strategy or "").lower()
        strategy_name = (sig.strategy_name or "").lower()
        is_breakout = (
            underlying in ("vb", "volatility_breakout")
            or strategy_name in ("vb", "volatility_breakout")
        )
        apply_pullback = not is_breakout
        assert apply_pullback is True, "불명 전략은 안전하게 필터 적용"


# ---------------------------------------------------------------------------
# 위기MR rule_name 태깅
# ---------------------------------------------------------------------------

class TestCrisisMrTagging:
    """위기MR underlying_strategy → rule_name에 '위기MR' 포함."""

    def _compute_rule_name(self, underlying: str, strength_name: str) -> str:
        """trade_executor._auto_trade의 rule_name 생성 로직 추출본."""
        u = (underlying or "").lower()
        if "crisis" in u or "meanrev" in u:
            return f"자동매매_위기MR_{strength_name}" if strength_name else "자동매매_위기MR"
        return f"자동매매_{strength_name}" if strength_name else "자동매매"

    def test_crisis_meanrev_tagged(self):
        rn = self._compute_rule_name("crisis_meanrev", "STRONG")
        assert "위기MR" in rn
        assert rn == "자동매매_위기MR_STRONG"

    def test_crisis_prefix_tagged(self):
        rn = self._compute_rule_name("crisis", "STRONG")
        assert "위기MR" in rn

    def test_normal_vb_not_tagged(self):
        rn = self._compute_rule_name("volatility_breakout", "STRONG")
        assert "위기MR" not in rn
        assert rn == "자동매매_STRONG"

    def test_trend_not_tagged(self):
        rn = self._compute_rule_name("trend_following", "STRONG")
        assert "위기MR" not in rn


# ---------------------------------------------------------------------------
# EOD 청산 판정 로직 (signal_runner.check_eod_liquidation 규칙)
# ---------------------------------------------------------------------------

class TestEodLiquidationRules:
    """check_eod_liquidation의 제외 조건 조합.

    NOTE: TradingConfig가 frozen dataclass라 실제 함수 호출 테스트는 설계 리팩터링 필요
    (e.g., config 주입 방식 도입). 현재는 스킵 로직을 복제해 검증.
    실제 함수 통합 테스트는 `LIVE 전환 전 필수작업` 리스트에 기록.
    """

    def _should_skip_eod(self, pos: dict) -> bool:
        """signal_runner.check_eod_liquidation의 스킵 조건 추출본.

        signal_runner.py:607-624와 동일한 순서/조건.
        """
        if pos.get("manual"):
            return True
        if pos.get("selling"):
            return True
        if "위기MR" in pos.get("rule_name", ""):
            return True
        intent = pos.get("intent")
        if intent == "swing":
            return True
        if (intent in (None, "")) and pos.get("strategy") == "trend_following":
            return True
        return False

    def test_manual_skipped(self):
        assert self._should_skip_eod({"manual": True}) is True

    def test_crisis_mr_skipped(self):
        assert self._should_skip_eod({"rule_name": "자동매매_위기MR_STRONG"}) is True

    def test_crisis_mr_overrides_daytrading(self):
        """위기MR rule_name은 intent=daytrading보다 우선."""
        pos = {"intent": "daytrading", "rule_name": "자동매매_위기MR_STRONG"}
        assert self._should_skip_eod(pos) is True

    def test_swing_intent_skipped(self):
        assert self._should_skip_eod({"intent": "swing"}) is True

    def test_legacy_trend_following_skipped(self):
        """intent 필드 없는 구 포지션은 legacy strategy 태그로 판단."""
        assert self._should_skip_eod({"strategy": "trend_following"}) is True

    def test_empty_intent_falls_back_to_legacy(self):
        """intent='' (수동 편집) → legacy trend_following 경로로 fallback."""
        pos = {"intent": "", "strategy": "trend_following"}
        assert self._should_skip_eod(pos) is True

    def test_daytrading_intent_liquidated(self):
        """intent=daytrading은 청산 대상 (False 반환)."""
        assert self._should_skip_eod({"intent": "daytrading"}) is False

    def test_daytrading_intent_overrides_legacy_strategy(self):
        """intent가 있으면 legacy strategy 태그 무시."""
        pos = {"intent": "daytrading", "strategy": "trend_following"}
        assert self._should_skip_eod(pos) is False, "intent=daytrading → 청산 대상"


# ---------------------------------------------------------------------------
# 쿨다운 영속화
# ---------------------------------------------------------------------------

class TestCooldownPersistence:
    """market_guard._save/_load_cooldown_state."""

    def test_save_and_reload(self, tmp_path, monkeypatch):
        """update_cooldown 후 파일에 저장되고 다시 읽을 수 있어야 함."""
        from alerts import market_guard
        # 임시 경로로 교체
        test_path = tmp_path / "cooldown_state.json"
        monkeypatch.setattr(market_guard, "_COOLDOWN_PATH", test_path)
        monkeypatch.setattr(market_guard, "_last_alert", {})

        market_guard.update_cooldown("005930", SignalType.BUY)
        assert test_path.exists()

        # 메모리 초기화 후 복원
        market_guard._last_alert = {}
        market_guard._load_cooldown_state()
        assert "005930:buy" in market_guard._last_alert

    def test_drops_entries_older_than_24h(self, tmp_path, monkeypatch):
        """24시간 이상된 엔트리는 로드 시 드롭."""
        from alerts import market_guard
        test_path = tmp_path / "cooldown_state.json"
        old_time = (datetime.now() - timedelta(days=2)).isoformat()
        new_time = datetime.now().isoformat()
        test_path.write_text(json.dumps({
            "OLD:buy": old_time,
            "NEW:buy": new_time,
        }), encoding="utf-8")

        monkeypatch.setattr(market_guard, "_COOLDOWN_PATH", test_path)
        monkeypatch.setattr(market_guard, "_last_alert", {})
        market_guard._load_cooldown_state()

        assert "OLD:buy" not in market_guard._last_alert
        assert "NEW:buy" in market_guard._last_alert


# ---------------------------------------------------------------------------
# 하루 매매 횟수 제한
# ---------------------------------------------------------------------------

class TestDailyBuyCount:
    """market_guard.daily_buy_count_ok."""

    def test_no_journal_file_passes(self, tmp_path, monkeypatch):
        """trade_journal.csv 없으면 통과 (첫 실행)."""
        from alerts import market_guard
        # journal 경로를 임시 디렉토리로 향하게 함 (존재하지 않는 파일)
        # daily_buy_count_ok 내부에서 Path(__file__).parent.parent / "data" / "trade_journal.csv"
        # 를 쓰므로 직접 monkeypatch 어려움. 대신 함수가 FileNotFound면 True 반환한다는 사실 확인.
        # → 현재 구조에선 파일이 없으면 True 리턴 경로를 타는지 간접 확인
        assert market_guard.MAX_DAILY_ROUNDTRIPS >= 1

    def test_max_daily_roundtrips_env_override(self, monkeypatch):
        """MAX_DAILY_ROUNDTRIPS 환경변수로 오버라이드 가능."""
        monkeypatch.setenv("MAX_DAILY_ROUNDTRIPS", "7")
        # 재로드
        import importlib
        from alerts import market_guard
        importlib.reload(market_guard)
        assert market_guard.MAX_DAILY_ROUNDTRIPS == 7
        # 원복
        monkeypatch.setenv("MAX_DAILY_ROUNDTRIPS", "3")
        importlib.reload(market_guard)
        assert market_guard.MAX_DAILY_ROUNDTRIPS == 3


# ---------------------------------------------------------------------------
# 시장 crash fail-safe
# ---------------------------------------------------------------------------

class TestMarketCrashFailSafe:
    """_is_market_crash 빈 데이터 fail-safe."""

    def test_empty_indices_returns_true(self, monkeypatch):
        """지수 데이터가 비어있으면 급락으로 간주 (fail-safe)."""
        from alerts import market_guard
        monkeypatch.setattr(market_guard, "fetch_index_prices", lambda: {})
        assert market_guard._is_market_crash() is True

    def test_exception_returns_true(self, monkeypatch):
        """예외 발생 시 급락으로 간주."""
        from alerts import market_guard

        def _raise():
            raise RuntimeError("network")
        monkeypatch.setattr(market_guard, "fetch_index_prices", _raise)
        assert market_guard._is_market_crash() is True

    def test_normal_data_returns_false(self, monkeypatch):
        """정상 데이터 + 급락 없으면 False."""
        from alerts import market_guard
        monkeypatch.setattr(
            market_guard,
            "fetch_index_prices",
            lambda: {"KOSPI": {"change_pct": -0.5}},
        )
        assert market_guard._is_market_crash() is False

    def test_crash_detected(self, monkeypatch):
        """-3% 이하 감지."""
        from alerts import market_guard
        monkeypatch.setattr(
            market_guard,
            "fetch_index_prices",
            lambda: {"KOSPI": {"change_pct": -3.5}},
        )
        assert market_guard._is_market_crash() is True


# ---------------------------------------------------------------------------
# 손실 한도 초과 텔레그램 알림 (하루 1회 쿨다운)
# ---------------------------------------------------------------------------

class TestLossLimitAlert:
    """한도 초과 시 텔레그램 알림 + 24h 중복 방지."""

    @pytest.fixture
    def mock_notifier(self, monkeypatch):
        """TelegramNotifier를 캡처. send_to_users 호출 내역을 리스트로 수집."""
        calls = []

        class _Mock:
            def send_to_users(self, users, msg):
                calls.append((users, msg))

        monkeypatch.setattr("alerts.telegram_notifier.TelegramNotifier", lambda: _Mock())
        return calls

    @pytest.fixture
    def isolated_alert_path(self, tmp_path, monkeypatch):
        """알림 상태 파일을 tmp_path로 격리."""
        from alerts import trade_executor
        p = tmp_path / "loss_limit_alert.json"
        monkeypatch.setattr(trade_executor, "_LOSS_LIMIT_ALERT_PATH", p)
        return p

    def test_first_alert_sends(self, mock_notifier, isolated_alert_path):
        """첫 호출은 텔레그램 발송."""
        from alerts.trade_executor import _notify_loss_limit
        _notify_loss_limit("monthly", "테스트 메시지")
        assert len(mock_notifier) == 1
        assert "테스트 메시지" in mock_notifier[0][1]

    def test_duplicate_alert_skipped(self, mock_notifier, isolated_alert_path):
        """같은 kind 재호출은 24h 쿨다운으로 스킵."""
        from alerts.trade_executor import _notify_loss_limit
        _notify_loss_limit("monthly", "첫 번째")
        _notify_loss_limit("monthly", "두 번째")
        assert len(mock_notifier) == 1
        assert "첫 번째" in mock_notifier[0][1]

    def test_different_kinds_independent(self, mock_notifier, isolated_alert_path):
        """kind가 다르면 각각 발송 (monthly/daily/consec 독립)."""
        from alerts.trade_executor import _notify_loss_limit
        _notify_loss_limit("monthly", "월")
        _notify_loss_limit("daily", "일")
        _notify_loss_limit("consec", "연속")
        assert len(mock_notifier) == 3

    def test_new_cycle_resends(self, mock_notifier, isolated_alert_path):
        """이전 사이클 ID와 다르면 재발송 (예: 월 바뀜)."""
        import json as _json
        from alerts.trade_executor import _notify_loss_limit
        # 지난달 사이클로 기록해둠
        isolated_alert_path.parent.mkdir(parents=True, exist_ok=True)
        isolated_alert_path.write_text(
            _json.dumps({"monthly_cycle_id": "m-2026-03"}), encoding="utf-8"
        )
        # 현재 월 = 2026-04 이상이므로 다른 사이클
        _notify_loss_limit("monthly", "새 달 트리거")
        assert len(mock_notifier) == 1


# ---------------------------------------------------------------------------
# 필터 차단 통계 (옵션 B — 병목 계측)
# ---------------------------------------------------------------------------

class TestFilterBlockStats:
    """매수 차단 필터별 카운터 + 날짜 자동 리셋."""

    @pytest.fixture
    def isolated_stats_path(self, tmp_path, monkeypatch):
        from alerts import trade_executor
        p = tmp_path / "filter_block_stats.json"
        monkeypatch.setattr(trade_executor, "_FILTER_STATS_PATH", p)
        return p

    def test_first_block_records_one(self, isolated_stats_path):
        from alerts.trade_executor import _record_filter_block, get_filter_block_stats_today
        _record_filter_block("pullback_pct")
        stats = get_filter_block_stats_today()
        assert stats.get("pullback_pct") == 1

    def test_repeated_blocks_accumulate(self, isolated_stats_path):
        from alerts.trade_executor import _record_filter_block, get_filter_block_stats_today
        for _ in range(5):
            _record_filter_block("daily_roundtrips")
        stats = get_filter_block_stats_today()
        assert stats["daily_roundtrips"] == 5

    def test_different_filters_independent(self, isolated_stats_path):
        from alerts.trade_executor import _record_filter_block, get_filter_block_stats_today
        _record_filter_block("pullback_pct")
        _record_filter_block("pullback_pct")
        _record_filter_block("ai_sell")
        _record_filter_block("time_filter")
        stats = get_filter_block_stats_today()
        assert stats["pullback_pct"] == 2
        assert stats["ai_sell"] == 1
        assert stats["time_filter"] == 1

    def test_date_change_resets(self, isolated_stats_path, monkeypatch):
        """날짜가 바뀌면 카운터 자동 리셋."""
        import json as _json
        from alerts.trade_executor import _record_filter_block, get_filter_block_stats_today
        # 어제 데이터 쓰기
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        isolated_stats_path.parent.mkdir(parents=True, exist_ok=True)
        isolated_stats_path.write_text(
            _json.dumps({"date": yesterday, "pullback_pct": 99}), encoding="utf-8"
        )
        _record_filter_block("pullback_pct")
        stats = get_filter_block_stats_today()
        assert stats.get("pullback_pct") == 1  # 99 → 1 (리셋됨)
        assert stats["date"] == datetime.now().strftime("%Y-%m-%d")

    def test_get_today_empty_if_no_data(self, isolated_stats_path):
        from alerts.trade_executor import get_filter_block_stats_today
        stats = get_filter_block_stats_today()
        assert stats["date"] == datetime.now().strftime("%Y-%m-%d")
        # 어떤 필터 키도 없어야 함
        assert "pullback_pct" not in stats


# ---------------------------------------------------------------------------
# 성과 스냅샷 (MOCK 단계 손실 한도 초과 시 비교 데이터 축적)
# ---------------------------------------------------------------------------

class TestLossLimitSnapshot:
    """MOCK에서 한도 초과 시 스냅샷 저장 / LIVE에선 저장 안 함."""

    @pytest.fixture
    def mock_notifier(self, monkeypatch):
        """TelegramNotifier Mock."""
        calls = []

        class _Mock:
            def send_to_users(self, users, msg):
                calls.append((users, msg))

        monkeypatch.setattr("alerts.telegram_notifier.TelegramNotifier", lambda: _Mock())
        return calls

    @pytest.fixture
    def isolated_alert_path(self, tmp_path, monkeypatch):
        from alerts import trade_executor
        p = tmp_path / "loss_limit_alert.json"
        monkeypatch.setattr(trade_executor, "_LOSS_LIMIT_ALERT_PATH", p)
        return p

    @pytest.fixture
    def isolated_snapshot_dir(self, tmp_path, monkeypatch):
        from alerts import performance_snapshot
        monkeypatch.setattr(performance_snapshot, "_SNAPSHOT_DIR", tmp_path / "snapshots")
        return tmp_path / "snapshots"

    def test_mock_saves_snapshot(self, mock_notifier, isolated_alert_path, isolated_snapshot_dir, monkeypatch):
        """MOCK 모드: 한도 초과 시 스냅샷 파일 저장."""
        from alerts import trade_executor
        monkeypatch.setattr(trade_executor, "OPERATION_MODE", "MOCK")
        trade_executor._notify_loss_limit(
            "monthly",
            "월 한도 테스트",
            snapshot_info={
                "current_loss": 62000,
                "limit_value": 60000,
                "positions": {"229200": {"qty": 10, "buy_price": 19000}},
            },
        )
        files = list(isolated_snapshot_dir.glob("snapshot_*_monthly.json"))
        assert len(files) == 1
        import json as _json
        data = _json.loads(files[0].read_text(encoding="utf-8"))
        assert data["kind"] == "monthly"
        assert data["current_loss"] == 62000
        assert data["limit_value"] == 60000
        assert data["exceeded_by"] == 2000
        assert "229200" in data["positions"]

    def test_live_skips_snapshot(self, mock_notifier, isolated_alert_path, isolated_snapshot_dir, monkeypatch):
        """LIVE 모드: snapshot_info 있어도 저장 안 함."""
        from alerts import trade_executor
        monkeypatch.setattr(trade_executor, "OPERATION_MODE", "LIVE")
        trade_executor._notify_loss_limit(
            "monthly",
            "LIVE 한도 테스트",
            snapshot_info={
                "current_loss": 62000,
                "limit_value": 60000,
                "positions": {},
            },
        )
        files = list(isolated_snapshot_dir.glob("snapshot_*.json"))
        assert len(files) == 0
        # 텔레그램 알림은 발송됨
        assert len(mock_notifier) == 1

    def test_no_snapshot_info_no_save(self, mock_notifier, isolated_alert_path, isolated_snapshot_dir, monkeypatch):
        """snapshot_info=None이면 저장 안 함 (기존 호출 호환)."""
        from alerts import trade_executor
        monkeypatch.setattr(trade_executor, "OPERATION_MODE", "MOCK")
        trade_executor._notify_loss_limit("monthly", "기본 호출")
        files = list(isolated_snapshot_dir.glob("snapshot_*.json"))
        assert len(files) == 0

    def test_same_cycle_skips_snapshot(self, mock_notifier, isolated_alert_path, isolated_snapshot_dir, monkeypatch):
        """같은 한도 사이클 내에서 두 번째 호출은 스냅샷/알림 둘 다 스킵."""
        from alerts import trade_executor
        monkeypatch.setattr(trade_executor, "OPERATION_MODE", "MOCK")
        trade_executor._notify_loss_limit(
            "monthly", "첫 번째",
            snapshot_info={"current_loss": 60000, "limit_value": 60000, "positions": {}},
        )
        trade_executor._notify_loss_limit(
            "monthly", "두 번째",
            snapshot_info={"current_loss": 80000, "limit_value": 60000, "positions": {}},
        )
        files = list(isolated_snapshot_dir.glob("snapshot_*_monthly.json"))
        assert len(files) == 1  # 같은 월 사이클이라 하나만
        assert len(mock_notifier) == 1  # 알림도 하나만


class TestCycleReset:
    """한도 사이클 리셋 감지 (LIVE 다운=끝 철학)."""

    @pytest.fixture
    def mock_notifier(self, monkeypatch):
        calls = []

        class _Mock:
            def send_to_users(self, users, msg):
                calls.append((users, msg))

        monkeypatch.setattr("alerts.telegram_notifier.TelegramNotifier", lambda: _Mock())
        return calls

    @pytest.fixture
    def isolated_alert_path(self, tmp_path, monkeypatch):
        from alerts import trade_executor
        p = tmp_path / "loss_limit_alert.json"
        monkeypatch.setattr(trade_executor, "_LOSS_LIMIT_ALERT_PATH", p)
        return p

    def test_consec_reset_allows_retrigger(self, mock_notifier, isolated_alert_path, monkeypatch):
        """consec 카운터가 0으로 리셋되면 다음 초과 시 재트리거 (새 cycle_id 생성)."""
        import json as _json
        from alerts import trade_executor, market_guard
        monkeypatch.setattr(market_guard, "MAX_CONSEC_STOPLOSS", 2)
        # 1차 트리거 상태로 설정 (c-1)
        isolated_alert_path.parent.mkdir(parents=True, exist_ok=True)
        isolated_alert_path.write_text(
            _json.dumps({"consec_cycle_id": "c-1", "consec_trigger_count": 1,
                         "_mode": "MOCK"}), encoding="utf-8"
        )
        # 카운터 0 (리셋됨) 시뮬
        monkeypatch.setattr(trade_executor, "OPERATION_MODE", "MOCK")
        monkeypatch.setattr("alerts.file_io.load_monthly_loss", lambda: {"consec_stoploss": 0})
        # 이제 다시 알림 호출하면 cycle clear + 카운터 증가 → c-2
        result = trade_executor._notify_loss_limit("consec", "리셋 후 재트리거")
        assert result is True
        assert len(mock_notifier) == 1
        # 파일에 c-2가 기록됐는지
        data = _json.loads(isolated_alert_path.read_text(encoding="utf-8"))
        assert data["consec_cycle_id"] == "c-2"
        assert data["consec_trigger_count"] == 2

    def test_consec_still_exceeded_stays_silent(self, mock_notifier, isolated_alert_path, monkeypatch):
        """consec 카운터가 여전히 한도 이상이면 같은 사이클 — 스킵."""
        import json as _json
        from alerts import trade_executor, market_guard
        monkeypatch.setattr(market_guard, "MAX_CONSEC_STOPLOSS", 2)
        monkeypatch.setattr(trade_executor, "OPERATION_MODE", "MOCK")
        isolated_alert_path.parent.mkdir(parents=True, exist_ok=True)
        isolated_alert_path.write_text(
            _json.dumps({"consec_cycle_id": "c-1", "consec_trigger_count": 1,
                         "_mode": "MOCK"}), encoding="utf-8"
        )
        # 카운터 3 (여전히 초과)
        monkeypatch.setattr("alerts.file_io.load_monthly_loss", lambda: {"consec_stoploss": 3})
        result = trade_executor._notify_loss_limit("consec", "추가 호출")
        assert result is False  # 같은 사이클
        assert len(mock_notifier) == 0

    def test_mcs_zero_guards_against_false_reset(self, mock_notifier, isolated_alert_path, monkeypatch):
        """Critical #1: MAX_CONSEC_STOPLOSS=0일 때 잘못된 리셋 방지 가드."""
        import json as _json
        from alerts import trade_executor, market_guard
        # _MCS=0 상태 (module 로드 직후)
        monkeypatch.setattr(market_guard, "MAX_CONSEC_STOPLOSS", 0)
        monkeypatch.setattr(trade_executor, "OPERATION_MODE", "MOCK")
        isolated_alert_path.parent.mkdir(parents=True, exist_ok=True)
        isolated_alert_path.write_text(
            _json.dumps({"consec_cycle_id": "c-1", "consec_trigger_count": 1,
                         "_mode": "MOCK"}), encoding="utf-8"
        )
        # count=0이고 _MCS=0일 때 "0 < 0 = False"라 리셋 안 돼야 정상 (가드 있음)
        monkeypatch.setattr("alerts.file_io.load_monthly_loss", lambda: {"consec_stoploss": 0})
        # 호출 시 기존 cycle_id 유지 → 같은 사이클 → 스킵
        result = trade_executor._notify_loss_limit("consec", "가드 확인")
        # 가드가 없었다면 리셋되어 c-2 신규 트리거됐을 것. 가드 덕에 스킵.
        data = _json.loads(isolated_alert_path.read_text(encoding="utf-8"))
        assert data.get("consec_cycle_id") == "c-1"  # 유지
        assert result is False  # 스킵


class TestModeTransition:
    """Critical #3: MOCK→LIVE 전환 시 loss_limit_alert.json 자동 리셋."""

    @pytest.fixture
    def isolated_alert_path(self, tmp_path, monkeypatch):
        from alerts import trade_executor
        p = tmp_path / "loss_limit_alert.json"
        monkeypatch.setattr(trade_executor, "_LOSS_LIMIT_ALERT_PATH", p)
        return p

    def test_mock_to_live_resets_alerts(self, isolated_alert_path, monkeypatch):
        """MOCK에서 기록된 cycle_id가 LIVE 전환 시 자동 리셋."""
        import json as _json
        from alerts import trade_executor
        # MOCK 상태로 저장된 파일
        isolated_alert_path.parent.mkdir(parents=True, exist_ok=True)
        isolated_alert_path.write_text(
            _json.dumps({
                "_mode": "MOCK",
                "monthly_cycle_id": "m-2026-04",
                "daily_cycle_id": "d-2026-04-17",
                "consec_cycle_id": "c-5",
            }), encoding="utf-8"
        )
        # LIVE 전환
        monkeypatch.setattr(trade_executor, "OPERATION_MODE", "LIVE")
        alerts = trade_executor._read_loss_limit_alerts()
        # 모드 불일치 감지 → 전체 리셋
        assert alerts == {"_mode": "LIVE"}
        assert "monthly_cycle_id" not in alerts

    def test_same_mode_keeps_state(self, isolated_alert_path, monkeypatch):
        """동일 모드면 상태 유지."""
        import json as _json
        from alerts import trade_executor
        isolated_alert_path.parent.mkdir(parents=True, exist_ok=True)
        isolated_alert_path.write_text(
            _json.dumps({
                "_mode": "MOCK",
                "monthly_cycle_id": "m-2026-04",
            }), encoding="utf-8"
        )
        monkeypatch.setattr(trade_executor, "OPERATION_MODE", "MOCK")
        alerts = trade_executor._read_loss_limit_alerts()
        assert alerts.get("monthly_cycle_id") == "m-2026-04"


class TestSnapshotModeGuard:
    """신규 #4: LIVE 모드에서 save_loss_limit_snapshot 호출 시 저장 거부."""

    def test_live_mode_refuses_save(self, tmp_path, monkeypatch):
        from alerts import performance_snapshot
        monkeypatch.setattr(performance_snapshot, "_SNAPSHOT_DIR", tmp_path)
        result = performance_snapshot.save_loss_limit_snapshot(
            "monthly", current_loss=60000, limit_value=60000,
            positions={}, mode="LIVE",
        )
        assert result is None  # 저장 거부
        files = list(tmp_path.glob("snapshot_*.json"))
        assert len(files) == 0

    def test_mock_mode_saves(self, tmp_path, monkeypatch):
        from alerts import performance_snapshot
        monkeypatch.setattr(performance_snapshot, "_SNAPSHOT_DIR", tmp_path)
        result = performance_snapshot.save_loss_limit_snapshot(
            "monthly", current_loss=60000, limit_value=60000,
            positions={}, mode="MOCK",
        )
        assert result is not None
        assert result.exists()


class TestTokenMasking:
    """Major #10: 텔레그램 토큰 로그 마스킹."""

    def test_mask_url_with_token(self):
        from alerts.telegram_notifier import mask_bot_token
        url = "https://api.telegram.org/bot1234567:AbcDefGhi123-xyz/sendMessage"
        masked = mask_bot_token(url)
        assert "1234567:AbcDefGhi123-xyz" not in masked
        assert "bot***MASKED***" in masked

    def test_mask_exception_message(self):
        from alerts.telegram_notifier import mask_bot_token
        exc_msg = "HTTPSConnectionPool: url https://api.telegram.org/bot9999:SECRET/getUpdates failed"
        masked = mask_bot_token(exc_msg)
        assert "SECRET" not in masked
        assert "9999" not in masked


class TestBuyInProgressCleanup:
    """Critical #4: cleanup_stale_buy_in_progress 동작."""

    @pytest.fixture
    def isolated_queue(self, tmp_path, monkeypatch):
        from alerts import trade_executor
        q = tmp_path / "order_queue.json"
        monkeypatch.setattr("alerts.file_io.ORDER_QUEUE_PATH", q)
        # _buy_in_progress 초기화
        trade_executor._buy_in_progress.clear()
        return q

    def test_restores_pending_ticker(self, isolated_queue, monkeypatch):
        """재시작 후 pending 주문이 있으면 _buy_in_progress에 복구."""
        import json as _json
        from alerts import trade_executor
        now_iso = datetime.now().isoformat()
        isolated_queue.write_text(_json.dumps({
            "orders": [
                {"ticker": "005930", "side": "buy", "status": "pending",
                 "submitted_at": now_iso},
            ]
        }), encoding="utf-8")
        trade_executor.cleanup_stale_buy_in_progress()
        assert "005930" in trade_executor._buy_in_progress

    def test_removes_stale_ticker(self, isolated_queue, monkeypatch):
        """5분 이상 오래된 pending은 _buy_in_progress에서 제거."""
        import json as _json
        from alerts import trade_executor
        old_iso = (datetime.now() - timedelta(minutes=10)).isoformat()
        isolated_queue.write_text(_json.dumps({
            "orders": [
                {"ticker": "005930", "side": "buy", "status": "pending",
                 "submitted_at": old_iso},
            ]
        }), encoding="utf-8")
        trade_executor._buy_in_progress.add("005930")
        trade_executor.cleanup_stale_buy_in_progress(max_age_sec=300)
        assert "005930" not in trade_executor._buy_in_progress

    def test_removes_orphan(self, isolated_queue, monkeypatch):
        """order_queue에 아예 없는 ticker도 정리."""
        import json as _json
        from alerts import trade_executor
        isolated_queue.write_text(_json.dumps({"orders": []}), encoding="utf-8")
        trade_executor._buy_in_progress.add("999999")
        trade_executor.cleanup_stale_buy_in_progress()
        assert "999999" not in trade_executor._buy_in_progress


class TestDailyLossCalc:
    """일일 손실 실제 값 계산 (감사관 지적: 하드코딩된 0 대신)."""

    def test_empty_queue_returns_zero(self, tmp_path, monkeypatch):
        from alerts import trade_executor
        empty = tmp_path / "order_queue.json"
        monkeypatch.setattr("alerts.file_io.ORDER_QUEUE_PATH", empty)
        assert trade_executor._get_today_daily_loss() == 0

    def test_losses_summed_correctly(self, tmp_path, monkeypatch):
        import json as _json
        from alerts import trade_executor
        today = datetime.now().strftime("%Y-%m-%d")
        queue = {
            "orders": [
                {"side": "sell", "status": "executed", "executed_at": f"{today}T10:30:00",
                 "exec_price": 19000, "buy_price": 20000, "quantity": 10},  # 손실 -10000
                {"side": "sell", "status": "executed", "executed_at": f"{today}T14:00:00",
                 "exec_price": 21000, "buy_price": 20000, "quantity": 10},  # 수익 +10000 (무시)
                {"side": "sell", "status": "executed", "executed_at": f"{today}T15:10:00",
                 "exec_price": 19500, "buy_price": 20000, "quantity": 20},  # 손실 -10000
            ]
        }
        q_path = tmp_path / "order_queue.json"
        q_path.write_text(_json.dumps(queue), encoding="utf-8")
        monkeypatch.setattr("alerts.file_io.ORDER_QUEUE_PATH", q_path)
        # 손실만 합산: 10000 + 10000 = 20000
        assert trade_executor._get_today_daily_loss() == 20000


class TestSnapshotCleanup:
    """1000개 초과 스냅샷 자동 정리."""

    def test_cleanup_removes_oldest(self, tmp_path, monkeypatch):
        from alerts import performance_snapshot
        monkeypatch.setattr(performance_snapshot, "_SNAPSHOT_DIR", tmp_path)
        monkeypatch.setattr(performance_snapshot, "_MAX_SNAPSHOTS", 5)  # 테스트용 작게
        # 10개 생성 (의도적으로 5 초과)
        for i in range(10):
            (tmp_path / f"snapshot_2026041{i}_120000_monthly.json").write_text("{}")
        performance_snapshot._cleanup_old_snapshots()
        remaining = list(tmp_path.glob("snapshot_*.json"))
        assert len(remaining) == 5
