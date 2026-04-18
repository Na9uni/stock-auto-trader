"""주식 분석 스케줄러 — 자동매매 + 알림 + 포지션 관리

모듈 구조:
  - alerts/_state.py            : 공유 설정, 전략, 인메모리 상태
  - alerts/signal_runner.py     : 신호 감지 (VB/combo/trend/score)
  - alerts/order_manager.py     : 주문 상태 체크, 큐 정리
  - alerts/file_io.py           : 경로 상수, JSON I/O, 캔들/지표 유틸
  - alerts/market_guard.py      : 손실 관리, 급락 감지, 장 시간, 쿨다운
  - alerts/trade_executor.py    : 자동매매 실행 (매수/매도)
  - alerts/position_manager.py  : 포지션 손절/트레일링/과매수 감시
  - alerts/crisis_manager.py    : 위기장 평균회귀 (RSI2)
  - alerts/notifications.py     : 텔레그램 알림, 신호 헤더, 사용자 관리
  - alerts/analysis_scheduler.py (이 파일): 설정 주입, 스케줄러 메인
"""

from __future__ import annotations

import time

import schedule

from strategies.base import SignalStrength
from alerts.telegram_notifier import TelegramNotifier

# ---------------------------------------------------------------------------
# 공유 상태 & 설정 (re-export)
# ---------------------------------------------------------------------------

from alerts._state import (                          # noqa: F401
    _TRADING_CONFIG,
    _STRATEGY,
    OPERATION_MODE,
    MOCK_MODE,
    AUTO_TRADE_ENABLED,
    AUTO_TRADE_AMOUNT,
    MAX_ORDER_AMOUNT,
    MAX_SLOTS,
    STOPLOSS_PCT,
    TRAILING_ACTIVATE_PCT,
    TRAILING_STOP_PCT,
    MAX_MONTHLY_LOSS,
    MAX_CONSEC_STOPLOSS,
    MAX_DAILY_LOSS,
    INTEREST_SPIKE_THRESHOLD,
    MAX_SELL_FAIL_BEFORE_REMOVE,
    _notified_orders,
    _order_init_done,
    logger,
)

# ---------------------------------------------------------------------------
# 서브모듈 임포트 (re-export — 기존 코드 호환)
# ---------------------------------------------------------------------------

from alerts.file_io import (                         # noqa: F401
    ROOT,
    KIWOOM_DATA_PATH,
    AUTO_POSITIONS_PATH,
    ORDER_QUEUE_PATH,
    MONTHLY_LOSS_PATH,
    TRADE_FILTER_PATH,
    load_kiwoom_data,
    load_auto_positions,
    save_auto_positions,
    load_trade_filter,
    is_ticker_filtered,
    load_monthly_loss,
    save_monthly_loss,
    candles_to_df,
    calc_indicators,
)

from alerts.market_guard import (                    # noqa: F401
    COOLDOWN,
    record_loss_and_stoploss,
    reset_consec_stoploss,
    is_monthly_loss_exceeded,
    is_consec_stoploss_exceeded,
    is_daily_loss_exceeded,
    _is_market_crash,
    fetch_index_prices,
    is_market_holiday,
    is_market_hours,
    cooldown_ok,
    update_cooldown,
)

from alerts.trade_executor import (                  # noqa: F401
    _buy_in_progress,
    _calc_trade_amount,
    _auto_trade,
)

from alerts.position_manager import (                # noqa: F401
    _sell_fail_count,
    _cleanup_failed_positions,
    check_auto_positions,
)

from alerts.crisis_manager import (                  # noqa: F401
    _CRISIS_MR_TARGETS,
    _crisis_mr_position,
    _rsi2_daily,
    _restore_crisis_mr_position,
    _check_crisis_meanrev,
)

from alerts.notifications import (                   # noqa: F401
    CMD_FOOTER,
    get_admin_id,
    get_users_for_ticker,
    save_last_signal,
    build_signal_header,
    send_premarket_news,
    send_news_update,
    send_market_open,
    send_market_close,
    send_daily_report,
)

# ---------------------------------------------------------------------------
# 분리된 모듈에서 함수 re-export
# ---------------------------------------------------------------------------

from alerts.signal_runner import (                   # noqa: F401
    check_signals,
    check_interest_spikes,
    check_targets,
    check_eod_liquidation,
    check_premarket_us,
    check_heartbeat,
)

from alerts.order_manager import (                   # noqa: F401
    check_order_status,
    _prune_order_queue,
    _parse_dt,
)

from config.theme_detector import (                  # noqa: F401
    check_theme_leaders,
    detect_theme_leaders,
    detect_volume_surge,
    detect_institutional_flow,
    detect_52week_high,
    detect_relative_strength,
)

from analysis.improvement_cycle import (             # noqa: F401
    run_improvement_cycle,
)

# ---------------------------------------------------------------------------
# 서브모듈 설정 주입
# ---------------------------------------------------------------------------

from alerts import market_guard as _mg
_mg._configure(MAX_MONTHLY_LOSS, MAX_CONSEC_STOPLOSS, MAX_DAILY_LOSS)

from alerts import trade_executor as _te
_te._configure(
    auto_trade_enabled=AUTO_TRADE_ENABLED,
    operation_mode=OPERATION_MODE,
    mock_mode=MOCK_MODE,
    auto_trade_amount=AUTO_TRADE_AMOUNT,
    max_order_amount=MAX_ORDER_AMOUNT,
    max_slots=MAX_SLOTS,
    buy_start_minute=_TRADING_CONFIG.buy_start_minute,
    buy_end_hour=_TRADING_CONFIG.buy_end_hour,
)
# 크래시 후 재시작 시 _buy_in_progress 복구 + stale 주문 정리
_te.cleanup_stale_buy_in_progress()

from alerts import position_manager as _pm
_pm._configure(STOPLOSS_PCT, TRAILING_ACTIVATE_PCT, TRAILING_STOP_PCT)

from alerts import notifications as _nf
_nf._configure(OPERATION_MODE, AUTO_TRADE_ENABLED, MAX_SLOTS)


# ---------------------------------------------------------------------------
# 스케줄러 메인
# ---------------------------------------------------------------------------

def run_scheduler() -> None:
    """스케줄러 메인: schedule 라이브러리로 주기적 실행."""
    logger.info("=" * 50)
    logger.info("주식 분석 스케줄러 시작")
    logger.info("운영 모드: %s | 자동매매: %s", OPERATION_MODE, "ON" if AUTO_TRADE_ENABLED else "OFF")
    logger.info("방어: 월간손실한도 %s원 / 연속손절한도 %d회", f"{MAX_MONTHLY_LOSS:,}", MAX_CONSEC_STOPLOSS)
    logger.info(
        "신호 쿨다운: STRONG=%d분 / MEDIUM=%d분 / WEAK=무시",
        COOLDOWN[SignalStrength.STRONG], COOLDOWN[SignalStrength.MEDIUM],
    )
    logger.info("=" * 50)

    # 시작 자가 진단
    try:
        from alerts.self_diagnostic import run_and_report
        run_and_report()
    except Exception as e:
        logger.error("[자가진단] 실행 실패: %s", e)

    # 시작 알림 (텔레그램)
    try:
        positions = load_auto_positions()
        holding = len(positions)
        # 매크로 레짐 판별
        from strategies.macro_regime import assess_current
        macro = assess_current()
        notifier = TelegramNotifier()
        notifier.send_to_users(
            [get_admin_id()],
            f"✅ 시스템 시작 완료 (v2)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  매크로: {macro.regime.value.upper()} (주식비중 {macro.equity_ratio*100:.0f}%)\n"
            f"  전략: {_STRATEGY.name}\n"
            f"  운영 모드: {OPERATION_MODE}\n"
            f"  자동매매: {'ON' if AUTO_TRADE_ENABLED else 'OFF'}\n"
            f"  보유 종목: {holding}개 / {MAX_SLOTS}슬롯\n"
            f"  손절: {STOPLOSS_PCT}% | 트레일링: {TRAILING_ACTIVATE_PCT}%→{TRAILING_STOP_PCT}%\n"
            f"  일일한도: {MAX_DAILY_LOSS:,}원 | 월간한도: {MAX_MONTHLY_LOSS:,}원\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
            + CMD_FOOTER,
        )
        logger.info("시작 알림 전송 완료")
    except Exception as e:
        logger.error("시작 알림 실패: %s", e)

    schedule.every(1).minutes.do(check_signals)
    schedule.every(1).minutes.do(check_targets)
    schedule.every(1).minutes.do(check_order_status)
    schedule.every(1).minutes.do(check_interest_spikes)
    schedule.every(1).minutes.do(check_auto_positions)
    # _buy_in_progress stale 정리 (크래시 방어 + 중복 주문 방지)
    schedule.every(5).minutes.do(_te.cleanup_stale_buy_in_progress)
    schedule.every().day.at("15:20").do(check_eod_liquidation)
    schedule.every().day.at("15:25").do(check_eod_liquidation)  # 재시도
    schedule.every().day.at("08:30").do(send_premarket_news)
    schedule.every().day.at("08:40").do(check_premarket_us)
    schedule.every().day.at("09:00").do(send_market_open)
    schedule.every().day.at("15:40").do(send_market_close)
    schedule.every().hour.at(":00").do(send_news_update)
    schedule.every(30).minutes.do(check_heartbeat)
    schedule.every().hour.at(":30").do(check_theme_leaders)
    schedule.every().day.at("16:00").do(send_daily_report)
    schedule.every().friday.at("16:00").do(run_improvement_cycle)
    schedule.every().day.at("06:00").do(_prune_order_queue)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    from alerts.telegram_commander import start_telegram_commander
    start_telegram_commander()
    run_scheduler()
