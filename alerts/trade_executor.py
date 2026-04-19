"""자동매매 실행 — 매수/매도 로직."""

from __future__ import annotations

import copy
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from alerts.file_io import (
    KIWOOM_DATA_PATH,
    load_auto_positions,
    is_ticker_filtered,
)
from alerts.market_guard import (
    is_monthly_loss_exceeded,
    is_consec_stoploss_exceeded,
    is_daily_loss_exceeded,
    _is_market_crash,
)
from alerts.notifications import get_admin_id, CMD_FOOTER
from strategies.base import SignalType, SignalStrength, SignalResult
from config.whitelist import is_whitelisted

logger = logging.getLogger("stock_analysis")

# ---------------------------------------------------------------------------
# 인메모리 상태
# ---------------------------------------------------------------------------

_buy_in_progress: set[str] = set()

# ---------------------------------------------------------------------------
# 손실 한도 알림 (텔레그램, 하루 1회 쿨다운, 파일 영속화)
# ---------------------------------------------------------------------------

_LOSS_LIMIT_ALERT_PATH = Path(__file__).parent.parent / "data" / "loss_limit_alert.json"
# 한도 사이클 기반 중복 방지 — 쿨다운 아닌 "LIVE 다운 1회 = 스냅샷 1회" 원칙
# - monthly: 월 바뀔 때까지 1회
# - daily:   자정 지날 때까지 1회
# - consec:  카운터 0 리셋될 때까지 1회 (익절 1회 발생 시)


def _compute_cycle_id(kind: str) -> str:
    """한도 사이클 식별자 (monthly/daily 순수 함수).

    monthly/daily: 월/일 문자열 기반, 날짜 바뀌면 자동으로 새 사이클.
    consec: **이 함수 아닌 `_notify_loss_limit` 내부에서 카운터 기반으로 생성**.
            고정 문자열 쓰면 리셋→재발→리셋→재발 반복 시 같은 ID로 스냅샷 누락.
    """
    now = datetime.now()
    if kind == "monthly":
        return f"m-{now.strftime('%Y-%m')}"
    if kind == "daily":
        return f"d-{now.strftime('%Y-%m-%d')}"
    # consec은 caller(_notify_loss_limit)가 카운터 기반으로 처리
    return f"{kind}-unknown"


def _read_loss_limit_alerts() -> dict:
    """loss_limit_alert.json 읽기. 운영모드 전환 감지 시 자동 리셋.

    MOCK→LIVE 전환 시 이전 MOCK 사이클 ID가 남아있으면 LIVE 첫 한도
    히트 시 알림 침묵 발생. 이를 방지하기 위해 mode 필드로 감지 후
    전체 리셋.
    """
    try:
        if not _LOSS_LIMIT_ALERT_PATH.exists():
            return {"_mode": OPERATION_MODE}
        with open(_LOSS_LIMIT_ALERT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"_mode": OPERATION_MODE}

    stored_mode = data.get("_mode")
    if stored_mode and stored_mode != OPERATION_MODE:
        logger.warning(
            "[loss_limit] 운영모드 전환 감지 %s → %s. 이전 사이클 ID 전체 리셋.",
            stored_mode, OPERATION_MODE,
        )
        return {"_mode": OPERATION_MODE}
    if not stored_mode:
        data["_mode"] = OPERATION_MODE
    return data


def _write_loss_limit_alerts(data: dict) -> None:
    try:
        _LOSS_LIMIT_ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _LOSS_LIMIT_ALERT_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(_LOSS_LIMIT_ALERT_PATH)
    except Exception as e:
        logger.warning("[loss_limit] 알림 파일 저장 실패: %s", e)


def _notify_loss_limit(
    kind: str,
    message: str,
    *,
    snapshot_info: dict | None = None,
) -> bool:
    """손실 한도 초과 텔레그램 알림 + MOCK 스냅샷. 한도 사이클당 1회.

    kind: "monthly" | "daily" | "consec"
    snapshot_info: MOCK 모드일 때 성과 스냅샷에 저장할 정보
                   {current_loss, limit_value, positions} 형태
                   None이면 스냅샷 생략 (LIVE 또는 기존 호출 호환)

    **LIVE 관점에서 "다운=끝"이므로 MOCK에서도 같은 다운 이벤트엔 1회만 기록.**
    한도 사이클(월/일/연속카운터)이 리셋되면 다시 트리거 가능.

    Returns:
        True = 알림 발송됨 (새 사이클), False = 같은 사이클 내 스킵
    """
    now = datetime.now()
    alerts = _read_loss_limit_alerts()

    # consec 리셋 감지: 카운터가 한도 밑으로 내려갔으면 사이클 ID clear
    # 가드: _MCS=0이면 market_guard._configure() 미호출 상태 → 리셋 판정 보류
    if alerts.get("consec_cycle_id"):
        try:
            from alerts.file_io import load_monthly_loss as _lml
            from alerts.market_guard import MAX_CONSEC_STOPLOSS as _MCS
            if _MCS > 0 and _lml().get("consec_stoploss", 0) < _MCS:
                del alerts["consec_cycle_id"]
        except Exception:
            pass

    # 현재 사이클 ID 계산
    if kind == "consec":
        # consec: 기존 활성 사이클 있으면 재사용 (스킵됨), 없으면 카운터 증가하여 신규 ID
        # 이유: "c-triggered" 고정 문자열로 하면 리셋→재발 반복 시 같은 ID라서 2회차부터 누락됨.
        existing = alerts.get("consec_cycle_id")
        if existing:
            current_cycle = existing
        else:
            next_count = int(alerts.get("consec_trigger_count", 0)) + 1
            current_cycle = f"c-{next_count}"
            alerts["consec_trigger_count"] = next_count
    else:
        current_cycle = _compute_cycle_id(kind)

    last_cycle = alerts.get(f"{kind}_cycle_id")
    if last_cycle == current_cycle:
        return False  # 같은 다운 이벤트 — 스냅샷/알림 스킵

    alerts[f"{kind}_cycle_id"] = current_cycle
    alerts[kind] = now.isoformat()
    _write_loss_limit_alerts(alerts)
    try:
        from alerts.telegram_notifier import TelegramNotifier
        TelegramNotifier().send_to_users(
            [get_admin_id()],
            message + CMD_FOOTER,
        )
    except Exception as e:
        logger.warning("[loss_limit] 텔레그램 알림 실패: %s", e)

    # MOCK 모드에서만 성과 스냅샷 저장 (실측 비교 실험용)
    if snapshot_info and OPERATION_MODE == "MOCK":
        try:
            from alerts.performance_snapshot import save_loss_limit_snapshot
            save_loss_limit_snapshot(
                kind=kind,
                current_loss=snapshot_info.get("current_loss", 0),
                limit_value=snapshot_info.get("limit_value", 0),
                positions=snapshot_info.get("positions"),
            )
        except Exception as e:
            logger.warning("[loss_limit] 스냅샷 저장 실패: %s", e)
    return True


# ---------------------------------------------------------------------------
# 필터 차단 통계 (옵션 B — 병목 계측)
# ---------------------------------------------------------------------------
# 목적: MOCK 운영 중 "어느 필터가 몇 번 매수를 막았나" 일별 집계.
# 활용: 2주+ 데이터 축적 후 하드 필터 → 가중치 전환 검토 근거.
# 계측 대상 (BUY 신호 차단 지점만):
#   whitelist, time_filter, market_crash, ai_sell,
#   monthly_loss, daily_loss, consec_stoploss,
#   daily_roundtrips, pullback_pct, slot_full

_FILTER_STATS_PATH = Path(__file__).parent.parent / "data" / "filter_block_stats.json"


def _read_filter_stats() -> dict:
    try:
        if not _FILTER_STATS_PATH.exists():
            return {}
        with open(_FILTER_STATS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_filter_stats(data: dict) -> None:
    try:
        _FILTER_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FILTER_STATS_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(_FILTER_STATS_PATH)
    except Exception as e:
        logger.warning("[filter_stats] 저장 실패: %s", e)


def _record_filter_block(filter_name: str) -> None:
    """필터가 매수를 1회 차단했음을 기록. 날짜 바뀌면 자동 리셋."""
    today = datetime.now().strftime("%Y-%m-%d")
    stats = _read_filter_stats()
    if stats.get("date") != today:
        stats = {"date": today}
    stats[filter_name] = int(stats.get(filter_name, 0)) + 1
    _write_filter_stats(stats)


def cleanup_stale_buy_in_progress(max_age_sec: int = 300) -> None:
    """크래시 복구 + stale 주문 정리.

    - order_queue의 pending 매수 주문 중 _buy_in_progress에 없는 것 → 복구
    - _buy_in_progress에 있으나 order_queue에 pending 아님 또는 max_age_sec 초과 → 제거

    시스템 시작 시 1회 + 주기적 호출 권장 (analysis_scheduler에서).
    """
    from alerts.file_io import ORDER_QUEUE_PATH
    try:
        if not ORDER_QUEUE_PATH.exists():
            return
        with open(ORDER_QUEUE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        orders = data.get("orders", [])
        now = datetime.now()

        active_pending = set()
        stale_tickers = set()
        all_buy_tickers = set()
        for o in orders:
            if not isinstance(o, dict) or o.get("side") != "buy":
                continue
            ticker = o.get("ticker", "")
            if not ticker:
                continue
            all_buy_tickers.add(ticker)
            if o.get("status") != "pending":
                continue

            # 3차 감사 지적: submitted_at이 None/"" 면 **방금 접수된 주문**일 가능성 ↑.
            # 이전 로직은 이를 stale로 오판 → 방금 접수 주문 즉시 _buy_in_progress에서 제거 → 중복 주문 위험.
            # fallback 우선순위: submitted_at → created_at → 현재 시각 (= 방금 접수로 간주)
            submitted = o.get("submitted_at") or o.get("created_at") or ""
            if not submitted:
                # 타임스탬프 완전 부재 → 안전하게 "방금 접수됨"으로 간주 (active_pending 유지)
                active_pending.add(ticker)
                continue
            try:
                submit_dt = datetime.fromisoformat(submitted)
                age_sec = (now - submit_dt).total_seconds()
                if age_sec > max_age_sec:
                    stale_tickers.add(ticker)
                else:
                    active_pending.add(ticker)
            except (ValueError, TypeError):
                # 파싱 실패: 타임스탬프 포맷 이상 → 안전하게 active로 처리 (false positive stale 방지)
                # 정말 오래된 주문이라면 다음 cycle에서 정상 파싱되거나 status가 바뀔 것.
                logger.warning("[buy_in_progress] %s submitted_at 파싱 실패 (%r) — active로 유지",
                               ticker, submitted)
                active_pending.add(ticker)

        # 크래시 복구: pending인데 _buy_in_progress 없음 → 복구
        for ticker in active_pending - _buy_in_progress:
            _buy_in_progress.add(ticker)
            logger.info("[buy_in_progress] %s 재시작 복구 (pending 주문 존재)", ticker)

        # 정리 대상: stale + orphan (order_queue에 아예 매수 이력 없음)
        orphan = _buy_in_progress - all_buy_tickers
        for ticker in orphan | stale_tickers:
            if ticker in _buy_in_progress:
                _buy_in_progress.discard(ticker)
                reason = "stale >5min" if ticker in stale_tickers else "orphan (no queue entry)"
                logger.warning("[buy_in_progress] %s 정리: %s", ticker, reason)
    except Exception as e:
        logger.warning("[buy_in_progress] cleanup 실패: %s", e)


def _get_today_daily_loss() -> int:
    """order_queue.json에서 오늘 체결된 매도 주문 손실 합계.

    market_guard.is_daily_loss_exceeded() 내부 로직과 동일하나,
    실제 손실 금액을 반환 (한도 초과 여부가 아닌). 스냅샷용.
    """
    from alerts.file_io import ORDER_QUEUE_PATH
    from datetime import date
    if not ORDER_QUEUE_PATH.exists():
        return 0
    try:
        with open(ORDER_QUEUE_PATH, "r", encoding="utf-8") as f:
            queue_data = json.load(f)
        orders = queue_data.get("orders", [])
    except Exception:
        return 0
    today_str = date.today().isoformat()
    total = 0
    for o in orders:
        if not isinstance(o, dict):
            continue
        if o.get("side") != "sell" or o.get("status") != "executed":
            continue
        exec_at = o.get("executed_at", "")
        if not exec_at.startswith(today_str):
            continue
        sell = int(o.get("exec_price") or 0)
        buy = int(o.get("buy_price", 0))
        qty = int(o.get("quantity", 0))
        if sell > 0 and buy > 0 and qty > 0:
            pnl = (sell - buy) * qty
            if pnl < 0:
                total += abs(pnl)
    return total


def get_filter_block_stats_today() -> dict:
    """오늘자 필터 차단 통계 조회. daily_report/heartbeat에서 사용."""
    today = datetime.now().strftime("%Y-%m-%d")
    stats = _read_filter_stats()
    if stats.get("date") != today:
        return {"date": today}
    return stats


# ---------------------------------------------------------------------------
# 설정 (analysis_scheduler에서 주입)
# ---------------------------------------------------------------------------

AUTO_TRADE_ENABLED: bool = False
OPERATION_MODE: str = "PAPER"
MOCK_MODE: bool = False
AUTO_TRADE_AMOUNT: int = 0
MAX_ORDER_AMOUNT: int = 0
MAX_SLOTS: int = 0
_buy_start_minute: int = 10
_buy_end_hour: int = 15


def _configure(
    auto_trade_enabled: bool,
    operation_mode: str,
    mock_mode: bool,
    auto_trade_amount: int,
    max_order_amount: int,
    max_slots: int,
    buy_start_minute: int,
    buy_end_hour: int,
) -> None:
    """analysis_scheduler에서 호출하여 설정값 주입."""
    global AUTO_TRADE_ENABLED, OPERATION_MODE, MOCK_MODE
    global AUTO_TRADE_AMOUNT, MAX_ORDER_AMOUNT, MAX_SLOTS
    global _buy_start_minute, _buy_end_hour
    AUTO_TRADE_ENABLED = auto_trade_enabled
    OPERATION_MODE = operation_mode
    MOCK_MODE = mock_mode
    AUTO_TRADE_AMOUNT = auto_trade_amount
    MAX_ORDER_AMOUNT = max_order_amount
    MAX_SLOTS = max_slots
    _buy_start_minute = buy_start_minute
    _buy_end_hour = buy_end_hour


# ---------------------------------------------------------------------------
# 매수 금액 계산
# ---------------------------------------------------------------------------

def _calc_trade_amount(price: int = 0) -> int:
    """슬롯별 매수 금액 계산 (master 구조 + son-dev price 파라미터 병합).

    - MOCK 모드: 수동 포지션과 완전 분리. AUTO_TRADE_AMOUNT 슬롯당 고정 예산.
      (사용자 요청: "수동은 없는 거라고 보고, 자동만 시드머니 N만원")
      → AUTO_TRADE_AMOUNT × MAX_SLOTS = 자동매매 전용 시드
    - LIVE 모드: 실제 예수금 기반 계산 (과매수 방지).

    Args:
        price: 대상 종목 현재가. LIVE에서 슬롯 예산 < 1주 가격일 때 1주 금액까지 증액
               (비싼 종목 매수 허용, MAX_ORDER_AMOUNT 범위 내, 예수금 내에서만).

    VETO 방어선 유지 (리스크 매니저 복구):
    - 레짐별 max_slots 오버라이드 (MOCK/LIVE 공통)
    - manual 포지션 슬롯 제외
    - MAX_ORDER_AMOUNT 상한 (단일 종목 집중 방지)
    """
    try:
        # 레짐별 max_slots 오버라이드 (공통 — MOCK도 레짐 제약 준수)
        effective_max_slots = MAX_SLOTS
        try:
            from strategies.regime_engine import get_regime_engine
            _rp = get_regime_engine().params
            effective_max_slots = min(MAX_SLOTS, _rp.max_slots)
        except Exception:
            pass

        # manual 포지션은 슬롯에서 완전히 제외 (공통)
        all_pos = load_auto_positions()
        auto_count = sum(1 for p in all_pos.values() if not p.get("manual", False))
        holding_count = auto_count + len(_buy_in_progress)
        free_slots = effective_max_slots - holding_count
        if free_slots <= 0:
            logger.info("[시드머니] 슬롯 꽉참 (%d/%d)", holding_count, effective_max_slots)
            return 0

        if MOCK_MODE:
            # MOCK: 자동매매 전용 시드 = AUTO_TRADE_AMOUNT × MAX_SLOTS
            # 수동 포지션/실제 예수금 영향 없이 항상 AUTO_TRADE_AMOUNT 고정 반환
            amount = min(AUTO_TRADE_AMOUNT, MAX_ORDER_AMOUNT)
            if amount < 10000:
                logger.info("[시드머니] 슬롯 예산 %s원 < 최소 1만원 → 매수 불가", f"{amount:,}")
                return 0
            return amount

        # LIVE: 실제 예수금 기반
        with open(KIWOOM_DATA_PATH, "r", encoding="utf-8") as f:
            kd = json.load(f)
        balance = int(kd.get("account", {}).get("balance", 0))
        if balance <= 0:
            logger.info("[시드머니] 예수금 0 → 매수 불가")
            return 0
        amount = balance // free_slots
        # MAX_ORDER_AMOUNT 상한 복구 (리스크 매니저 VETO 반영)
        # 단일 종목 집중 방지 — 아무리 비싼 종목이라도 MAX_ORDER_AMOUNT 초과 매수 금지
        if amount > MAX_ORDER_AMOUNT:
            amount = MAX_ORDER_AMOUNT
        # 비싼 종목 1주 매수 허용 (단, MAX_ORDER_AMOUNT 범위 내에서만)
        if price > 0 and amount < price:
            if price <= MAX_ORDER_AMOUNT and price <= balance:
                logger.info("[시드머니] 슬롯예산 %s원 < 현재가 %s원 → 1주 금액으로 증액 (상한 %s원 내)",
                            f"{amount:,}", f"{price:,}", f"{MAX_ORDER_AMOUNT:,}")
                amount = price
            else:
                logger.info("[시드머니] 현재가 %s원 > 주문 상한 %s원 → 매수 불가",
                            f"{price:,}", f"{MAX_ORDER_AMOUNT:,}")
                return 0
        if amount < 10000:
            logger.info("[시드머니] 슬롯 예산 %s원 < 최소 1만원 → 매수 불가", f"{amount:,}")
            return 0
        return amount
    except Exception as e:
        logger.error("[시드머니] 예수금 계산 실패: %s", e)
        if not MOCK_MODE:
            return 0
        return AUTO_TRADE_AMOUNT


# ---------------------------------------------------------------------------
# 자동매매 실행
# ---------------------------------------------------------------------------

def _auto_trade(ticker: str, name: str, signal: SignalResult,
                stock: dict, notifier, ai_decision: str) -> None:
    """신호 기반 자동매매 실행."""
    from trading.auto_trader import execute_buy, execute_sell
    from alerts.file_io import save_auto_positions

    if not AUTO_TRADE_ENABLED:
        logger.debug("[자동매매] %s 차단: 자동매매 비활성화", name)
        return
    if OPERATION_MODE not in ("LIVE", "MOCK"):
        logger.debug("[자동매매] %s 차단: 운영모드 %s (LIVE/MOCK 아님)", name, OPERATION_MODE)
        return
    if not is_whitelisted(ticker):
        logger.debug("[자동매매] %s 차단: 화이트리스트 미포함", name)
        return

    # ── 시간 제한 (TradingConfig 기반) ──
    now = datetime.now()
    buy_start = _buy_start_minute
    buy_end = _buy_end_hour
    # 장 시작 N분 이내 매수 차단 (매도는 허용)
    if now.hour == 9 and now.minute < buy_start and signal.signal_type == SignalType.BUY:
        logger.debug("[시간 제한] 장 시작 %d분 이내 — 매수 보류", buy_start)
        _record_filter_block("time_filter")
        return
    # buy_end_hour 이후 신규 매수 차단 (매도는 허용)
    if now.hour >= buy_end and signal.signal_type == SignalType.BUY:
        logger.debug("[시간 제한] %d시 이후 — 신규 매수 차단", buy_end)
        _record_filter_block("time_filter")
        return

    # ── 서킷브레이커/급락 대응 ──
    if signal.signal_type == SignalType.BUY and _is_market_crash():
        logger.debug("[자동매매] %s 차단: 시장 급락 감지", name)
        _record_filter_block("market_crash")
        return

    # ── 매수 ──
    if signal.signal_type == SignalType.BUY and signal.strength == SignalStrength.STRONG:
        # AI 판단: "매도"이면 차단, 실패/빈값은 경고 후 진행
        if ai_decision == "매도":
            logger.info("[자동매매] %s AI 판단=매도 → 매수 차단", name)
            _record_filter_block("ai_sell")
            return
        if ai_decision and ai_decision != "매수":
            logger.info("[자동매매] %s AI 판단=%s → 경고 후 진행", name, ai_decision)
        if not ai_decision:
            logger.debug("[자동매매] %s AI 미응답 → AI 없이 진행", name)

        # 방어 체크 — HEAD(dad-dev) 안전장치 + master(son-dev) mock 파라미터 결합
        # MOCK: 한도 초과 시에도 매수 계속 (스냅샷만 저장, 실측 비교용)
        # LIVE: 즉시 차단 (노후 자금 보호)
        # MOCK 손실은 monthly_loss_mock.json 별도 파일 (mock=MOCK_MODE 파라미터)
        if is_monthly_loss_exceeded(mock=MOCK_MODE):
            logger.warning("[자동매매] 월간 손실 한도 초과 → 매수 차단%s", " [MOCK]" if MOCK_MODE else "")
            _record_filter_block("monthly_loss")
            from alerts.file_io import load_monthly_loss as _load_ml
            from alerts.market_guard import MAX_MONTHLY_LOSS as _MAX_ML
            _ml_state = _load_ml(mock=MOCK_MODE)
            _notify_loss_limit(
                "monthly",
                "🛑 [월 손실 한도 초과]\n"
                "신규 매수 중단\n"
                "→ 다음 달 1일 자동 재개\n"
                "※ 기존 포지션 관리(손절/트레일링)는 계속 작동",
                snapshot_info={
                    "current_loss": int(_ml_state.get("loss", 0)),
                    "limit_value": int(_MAX_ML),
                    "positions": copy.deepcopy(load_auto_positions()),
                },
            )
            if OPERATION_MODE != "MOCK":
                return
            # MOCK: 거래 계속 진행 (데이터 수집용)
        if is_consec_stoploss_exceeded(mock=MOCK_MODE):
            logger.warning("[자동매매] 연속 손절 한도 초과 → 매수 차단%s", " [MOCK]" if MOCK_MODE else "")
            _record_filter_block("consec_stoploss")
            from alerts.file_io import load_monthly_loss as _load_ml2
            from alerts.market_guard import MAX_CONSEC_STOPLOSS as _MAX_CS
            _ml_state2 = _load_ml2(mock=MOCK_MODE)
            _notify_loss_limit(
                "consec",
                "⚠️ [연속 손절 한도 초과]\n"
                "신규 매수 일시 중단\n"
                "→ 익절 1회 발생 시 자동 재개",
                snapshot_info={
                    "current_loss": int(_ml_state2.get("consec_stoploss", 0)),
                    "limit_value": int(_MAX_CS),
                    "positions": copy.deepcopy(load_auto_positions()),
                },
            )
            if OPERATION_MODE != "MOCK":
                return
            # MOCK: 거래 계속 진행 (데이터 수집용)
        # 일일 손실은 order_queue.json 1회 읽기로 판정 + 스냅샷 값 동시 확보 (race 방지)
        # _MAX_DL<=0일 때 안전 차단 + 비정상값 가드
        from alerts.market_guard import MAX_DAILY_LOSS as _MAX_DL
        _daily_loss_now = _get_today_daily_loss()
        _daily_exceeded = False
        if _MAX_DL <= 0:
            logger.error(
                "[안전] MAX_DAILY_LOSS=%d — 비정상 값 (market_guard._configure 미호출 또는 .env 실수?). "
                "안전을 위해 매수 차단.", _MAX_DL,
            )
            _daily_exceeded = True
        elif _daily_loss_now >= _MAX_DL:
            _daily_exceeded = True
        if _daily_exceeded:
            logger.warning("[자동매매] 당일 손실 한도 초과: %d / %d → 매수 차단",
                           _daily_loss_now, _MAX_DL)
            _record_filter_block("daily_loss")
            if _MAX_DL > 0:
                _notify_loss_limit(
                    "daily",
                    "⏸️ [당일 손실 한도 초과]\n"
                    "오늘만 신규 매수 중단\n"
                    "→ 내일 09:00 자동 재개",
                    snapshot_info={
                        "current_loss": _daily_loss_now,
                        "limit_value": int(_MAX_DL),
                        "positions": copy.deepcopy(load_auto_positions()),
                    },
                )
            if OPERATION_MODE != "MOCK":
                return
            # MOCK: 거래 계속 진행 (데이터 수집용)

        # 중복 체크 (manual 포지션은 자동매매 대상 아님)
        positions = load_auto_positions()
        auto_positions = {k: v for k, v in positions.items() if not v.get("manual", False)}
        if ticker in auto_positions or ticker in _buy_in_progress:
            if ticker in auto_positions and auto_positions[ticker].get("selling"):
                logger.debug("[자동매매] %s 차단: 매도 진행 중", name)
                _record_filter_block("already_holding")
                return
            if ticker in auto_positions:
                logger.debug("[자동매매] %s 차단: 이미 보유 중", name)
                _record_filter_block("already_holding")
                return
            if ticker in _buy_in_progress:
                logger.debug("[자동매매] %s 차단: 매수 접수 진행 중", name)
                _record_filter_block("already_holding")
                return

        # 필터 체크
        filtered, reason = is_ticker_filtered(ticker)
        if filtered:
            logger.info("[자동매매] %s 필터 제외: %s", name, reason)
            _record_filter_block("whitelist")
            return

        # 하루 동일 종목 매매 횟수 제한 (master 추가 — 데이트레이딩 재진입 무한루프 방지)
        from alerts.market_guard import daily_buy_count_ok
        if not daily_buy_count_ok(ticker):
            _record_filter_block("daily_roundtrips")
            return

        # 저점 매수 필터 (master 추가) — 당일 고가 대비 N% 이상 눌렸을 때만 매수
        # 사용자 요청: "매수신호가 오면 최대한 저점에서 매수, 보유 인터벌 확보"
        # ⚠️ VB(변동성 돌파)는 "돌파 순간 즉시 매수"가 철학이라 필터 제외.
        # → trend_following / crisis_meanrev 등 추세/평균회귀 전략에만 적용.
        # 기본 0.3%, .env의 PULLBACK_PCT로 조정 가능.
        current_price_now = int(stock.get("current_price", 0))
        day_high = int(stock.get("high", 0) or 0)
        import os as _os_pb
        try:
            PULLBACK_PCT = float(_os_pb.getenv("PULLBACK_PCT", "0.3"))
        except ValueError:
            PULLBACK_PCT = 0.3
        underlying = (getattr(signal, "underlying_strategy", "") or "").lower()
        strategy_name = (getattr(signal, "strategy_name", "") or "").lower()
        is_breakout_strategy = (
            underlying in ("vb", "volatility_breakout")
            or strategy_name in ("vb", "volatility_breakout")
        )
        apply_pullback = not is_breakout_strategy
        if apply_pullback and day_high > 0 and current_price_now > 0:
            pullback = (day_high - current_price_now) / day_high * 100
            if pullback < PULLBACK_PCT:
                logger.info(
                    "[자동매매] %s 저점대기: 현재가 %s / 당일고가 %s (눌림 %.2f%% < %.1f%%) [strategy=%s/underlying=%s]",
                    name, f"{current_price_now:,}", f"{day_high:,}", pullback, PULLBACK_PCT,
                    strategy_name or "?", underlying or "?",
                )
                _record_filter_block("pullback_pct")
                return

        # 금액 계산 (master price 파라미터 + dad-dev no_budget 필터 카운트)
        price = current_price_now
        if price <= 0:
            return
        amount = _calc_trade_amount(price=price)
        if amount <= 0:
            _record_filter_block("no_budget")
            return
        quantity = amount // price
        if quantity <= 0:
            return

        _buy_in_progress.add(ticker)
        logger.debug("[자동매매] %s 매수 진행: qty=%d, price=%s, mode=%s", name, quantity, f"{price:,}", OPERATION_MODE)

        # _buy_in_progress 누수 방지용 플래그.
        # MOCK 성공 또는 LIVE pending이면 _keep_in_progress=True → finally에서 discard 안 함.
        # (LIVE pending은 체결 콜백이 정리해야 하므로 유지)
        _keep_in_progress = False
        try:
            if OPERATION_MODE == "MOCK":
                # 100% 즉시 매수 (분할 매수 비활성 — 자본 효율 극대화)
                logger.info(
                    "[가상매매] %s 매수 체결 %d주 @%s (금액 %s)",
                    name, quantity, f"{price:,}", f"{amount:,}",
                )
                from trading.trade_journal import record_trade
                record_trade(
                    ticker=ticker, name=name, side="buy",
                    quantity=quantity, price=price,
                    reason=", ".join(signal.reasons[:2]),
                    strategy=signal.strategy_name if hasattr(signal, "strategy_name") else "",
                    mock=True,
                    exec_strength=float(stock.get("exec_strength", 0)),
                    ai_decision=ai_decision,
                )
                notifier.send_to_users(
                    [get_admin_id()],
                    f"🛒 [가상 매수 체결] {name} ({ticker})\n"
                    f"💰 수량: {quantity}주 / 가격: {price:,}원\n"
                    f"💵 투자금액: {amount:,}원\n"
                    f"📊 사유: {', '.join(signal.reasons[:3])}\n"
                    f"⚠️ 모의투자"
                    + CMD_FOOTER,
                )
                # Determine strategy mode from regime (EOD liquidation용 — legacy)
                try:
                    from strategies.regime_engine import get_regime_engine
                    _regime = get_regime_engine().state.value
                    _strategy_tag = "trend_following" if _regime in ("swing", "defense", "cash") else "vb"
                except Exception:
                    _strategy_tag = ""

                # EOD 청산 판정은 TRADING_STYLE 기반 intent로 결정 (레짐 전환에 불변).
                # intent="daytrading" → EOD 청산, intent="swing" → 보유 지속.
                # 매수 시점의 TRADING_STYLE을 고정 기록.
                from alerts._state import get_trading_intent as _get_intent
                _intent = _get_intent()

                # rule_name: 위기MR 평균회귀 전략일 땐 "위기MR" 태그를 포함시켜
                # EOD 청산 / 전용 청산 로직이 식별할 수 있게 함.
                _underlying = (getattr(signal, "underlying_strategy", "") or "").lower()
                _strength_name = signal.strength.name if hasattr(signal, "strength") else ""
                if "crisis" in _underlying or "meanrev" in _underlying:
                    _rule_name = f"자동매매_위기MR_{_strength_name}" if _strength_name else "자동매매_위기MR"
                else:
                    _rule_name = f"자동매매_{_strength_name}" if _strength_name else "자동매매"

                positions = load_auto_positions()
                positions[ticker] = {
                    "name": name,
                    "qty": quantity,
                    "buy_price": price,
                    "buy_amount": amount,
                    "buy_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "bought_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "high_price": price,
                    "trailing_activated": False,
                    "rule_name": _rule_name,
                    "mock": True,
                    "strategy": _strategy_tag,  # legacy 태그 (호환용)
                    "intent": _intent,          # 새 기준 — EOD 청산 판정용
                }
                save_auto_positions(positions)
                # MOCK은 즉시 체결 처리 완료 → discard (finally에서 처리)
            else:
                # LIVE rule_name: MOCK과 동일한 규칙 — 위기MR이면 전용 태깅
                _underlying_live = (getattr(signal, "underlying_strategy", "") or "").lower()
                _strength_live = signal.strength.name if hasattr(signal, "strength") else ""
                if "crisis" in _underlying_live or "meanrev" in _underlying_live:
                    _rn_live = f"자동매매_위기MR_{_strength_live}" if _strength_live else "자동매매_위기MR"
                else:
                    _rn_live = f"자동매매_{_strength_live}" if _strength_live else "자동매매"
                buy_result = execute_buy(ticker, name, quantity, price,
                                         rule_name=_rn_live)
                if buy_result.get("status") == "pending":
                    logger.info(
                        "[자동매매] %s 매수 접수 %d주 @%s (금액 %s)",
                        name, quantity, f"{price:,}", f"{amount:,}",
                    )
                    notifier.send_to_users(
                        [get_admin_id()],
                        f"🛒 [자동매매] {name} 매수 접수\n"
                        f"수량: {quantity}주 / 가격: {price:,}원\n"
                        f"사유: {', '.join(signal.reasons[:3])}"
                        + CMD_FOOTER,
                    )
                    # LIVE pending: 체결 콜백이 _buy_in_progress 정리해야 함
                    _keep_in_progress = True
                # pending 아니면(실패/거부) finally에서 discard
        finally:
            if not _keep_in_progress:
                _buy_in_progress.discard(ticker)

    # ── 매도 ──
    elif signal.signal_type == SignalType.SELL:
        positions = load_auto_positions()
        if ticker not in positions:
            return
        pos = positions[ticker]
        if pos.get("manual", False):
            return
        if pos.get("selling"):
            return

        qty = int(pos.get("qty", 0))
        if qty <= 0:
            return

        current_price = int(stock.get("current_price", 0))

        if OPERATION_MODE == "MOCK":
            # MOCK 모드: 가상 매도 체결 알림
            buy_price = int(pos.get("buy_price", 0))
            pnl = (current_price - buy_price) * qty if buy_price > 0 else 0
            pnl_pct = ((current_price - buy_price) / buy_price * 100) if buy_price > 0 else 0
            emoji = "📈" if pnl >= 0 else "📉"

            logger.info(
                "[가상매매] %s 매도 체결 %d주 @%s (손익 %s원)",
                name, qty, f"{current_price:,}", f"{pnl:+,}",
            )
            from trading.trade_journal import record_trade
            record_trade(
                ticker=ticker, name=name, side="sell",
                quantity=qty, price=current_price,
                reason=", ".join(signal.reasons[:2]),
                strategy=signal.strategy_name if hasattr(signal, "strategy_name") else "",
                mock=True,
                buy_price=buy_price,
                buy_time=pos.get("buy_time", ""),
                pnl=pnl,
            )
            notifier.send_to_users(
                [get_admin_id()],
                f"💸 [가상 매도 체결] {name} ({ticker})\n"
                f"💰 수량: {qty}주 / 매도가: {current_price:,}원\n"
                f"📊 매수가: {buy_price:,}원\n"
                f"{emoji} 손익: {pnl:+,}원 ({pnl_pct:+.1f}%)\n"
                f"📊 사유: {', '.join(signal.reasons[:3])}\n"
                f"⚠️ 모의투자 — 실제 돈은 사용되지 않았습니다"
                + CMD_FOOTER,
            )
            # 가상 포지션 제거
            positions = load_auto_positions()
            if ticker in positions:
                del positions[ticker]
                save_auto_positions(positions)
        else:
            sell_result = execute_sell(ticker, name, qty, current_price,
                                       rule_name=f"자동매매_{signal.strength.name}")
            if sell_result.get("status") == "pending":
                fresh = load_auto_positions()
                if ticker in fresh:
                    fresh[ticker]["selling"] = True
                    fresh[ticker]["sell_order_id"] = sell_result.get("order_id", "")
                    save_auto_positions(fresh)
                logger.info("[자동매매] %s 매도 접수 %d주", name, qty)
