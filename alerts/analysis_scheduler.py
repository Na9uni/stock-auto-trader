"""주식 분석 스케줄러 — 자동매매 + 알림 + 포지션 관리

모듈 구조:
  - alerts/file_io.py          : 경로 상수, JSON I/O, 캔들/지표 유틸
  - alerts/market_guard.py     : 손실 관리, 급락 감지, 장 시간, 쿨다운
  - alerts/trade_executor.py   : 자동매매 실행 (매수/매도)
  - alerts/position_manager.py : 포지션 손절/트레일링/과매수 감시
  - alerts/crisis_manager.py   : 위기장 평균회귀 (RSI2)
  - alerts/notifications.py    : 텔레그램 알림, 신호 헤더, 사용자 관리
  - alerts/analysis_scheduler.py (이 파일): 설정 로드, 전략 빌드, 스케줄러 메인
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import schedule

from alerts.signal_detector import SignalType, SignalStrength, SignalResult
from alerts.telegram_notifier import TelegramNotifier
from config.trading_config import TradingConfig
from config.whitelist import (
    AUTO_TRADE_WHITELIST,
    LARGECAP_DAILY_THRESHOLD,
    LARGECAP_DAILY_MIN_SCORE,
    is_whitelisted,
)
from strategies.vb_strategy import VBStrategy
from strategies.score_strategy import ScoreStrategy
from strategies.combo_strategy import ComboStrategy
from strategies.base import MarketContext
from strategies.base import SignalType as StratSignalType
from strategies.base import SignalStrength as StratSignalStrength

# ---------------------------------------------------------------------------
# 서브모듈 임포트 (re-export 포함)
# ---------------------------------------------------------------------------

from alerts.file_io import (
    ROOT,
    KIWOOM_DATA_PATH,
    AUTO_POSITIONS_PATH,
    ORDER_QUEUE_PATH,
    MONTHLY_LOSS_PATH,
    TRADE_FILTER_PATH,
    logger,
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

from alerts.market_guard import (
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

from alerts.trade_executor import (
    _buy_in_progress,
    _calc_trade_amount,
    _auto_trade,
)

from alerts.position_manager import (
    _sell_fail_count,
    _cleanup_failed_positions,
    check_auto_positions,
)

from alerts.crisis_manager import (
    _CRISIS_MR_TARGETS,
    _crisis_mr_position,
    _rsi2_daily,
    _restore_crisis_mr_position,
    _check_crisis_meanrev,
)

from alerts.notifications import (
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
# 통합 설정 (TradingConfig에서 로드)
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

from alerts import position_manager as _pm
_pm._configure(STOPLOSS_PCT, TRAILING_ACTIVATE_PCT, TRAILING_STOP_PCT)

from alerts import notifications as _nf
_nf._configure(OPERATION_MODE, AUTO_TRADE_ENABLED, MAX_SLOTS)

# ---------------------------------------------------------------------------
# 전략 객체 (전략 선택은 .env STRATEGY 설정에 따름)
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
# 인메모리 상태 변수
# ---------------------------------------------------------------------------

_notified_orders: set[str] = set()
_order_init_done: bool = False

# ---------------------------------------------------------------------------
# 신호 감지 래퍼
# ---------------------------------------------------------------------------

def _run_signal_for_stock(ticker: str, name: str, stock: dict,
                          notifier, ai) -> None:
    """단일 종목 5분봉 신호 감지 → AI 분석 → 판단 기반 매매."""
    from alerts.signal_detector import detect

    df = candles_to_df(stock.get("candles_1m", []))
    df_ind = calc_indicators(df)
    if df_ind is None:
        return

    exec_strength = stock.get("exec_strength", 0.0)
    try:
        change_rate = float(stock.get("change_rate") or 0.0)
    except (ValueError, TypeError):
        change_rate = 0.0
    orderbook = stock.get("orderbook")
    signal = detect(df_ind, exec_strength=exec_strength, change_rate=change_rate, orderbook=orderbook)

    if signal.signal_type == SignalType.NEUTRAL or signal.strength != SignalStrength.STRONG:
        logger.debug("%s: 신호 강도 부족 (strength=%s, score=%d)", name, signal.strength.name, signal.score)
        return

    # 일봉 추세 게이트: MA20 상승 + RSI 35~65
    if signal.signal_type == SignalType.BUY:
        daily_candles = stock.get("candles_1d", [])
        if len(daily_candles) >= 25:
            df_daily = candles_to_df(daily_candles)
            df_daily_ind = calc_indicators(df_daily)
            if df_daily_ind is not None:
                last_daily = df_daily_ind.iloc[-1]
                daily_ma20 = last_daily.get("ma20", 0)
                prev_daily_ma20 = df_daily_ind.iloc[-5].get("ma20", 0) if len(df_daily_ind) >= 5 else 0
                daily_rsi = last_daily.get("rsi", 50)
                # MA20 하락 중이면 매수 차단
                if daily_ma20 > 0 and prev_daily_ma20 > 0 and daily_ma20 < prev_daily_ma20:
                    logger.debug("[일봉 필터] %s MA20 하락 중 → 매수 보류", name)
                    return
                # RSI 극단값이면 차단
                if daily_rsi > 75 or daily_rsi < 25:
                    logger.debug("[일봉 필터] %s 일봉 RSI %.0f 극단값 → 매수 보류", name, daily_rsi)
                    return

    # 매도 신호는 보유 종목에만
    if signal.signal_type == SignalType.SELL:
        positions = load_auto_positions()
        if ticker not in positions:
            return

    if not cooldown_ok(ticker, signal.signal_type, signal.strength):
        return

    # AI 분석 (매수 신호만)
    ai_decision = ""
    ai_text = ""
    if signal.signal_type == SignalType.BUY:
        warn_text = signal.warnings if hasattr(signal, "warnings") else []
        ai_result = ai.quick_signal_alert(
            ticker=ticker, name=name,
            price=stock.get("current_price", 0),
            change_rate=stock.get("change_rate", 0),
            signal_reasons=signal.reasons,
            rsi=signal.rsi, macd_cross=signal.macd_cross,
            vol_ratio=signal.vol_ratio,
            recent_candles=stock.get("candles_1m", [])[:5],
            orderbook=stock.get("orderbook"),
            exec_strength=exec_strength, warnings=warn_text,
        )
        ai_decision = ai_result.get("decision", "")
        ai_text = ai_result.get("text", "")

    header = build_signal_header(ticker, name, signal, stock)
    ai_block = f"\n[Sonnet AI 분석]\n{ai_text}\n" if ai_text else ""
    full_msg = header + ai_block + CMD_FOOTER

    target_ids = get_users_for_ticker(ticker)
    ok = notifier.send_to_users(target_ids, full_msg)
    if ok:
        # 매수 시간 밖이면 쿨다운 기록 안 함 (다음 유효 시간에 재평가)
        now_check = datetime.now()
        in_buy_window = not (now_check.hour == 9 and now_check.minute < _TRADING_CONFIG.buy_start_minute)
        if in_buy_window or signal.signal_type != SignalType.BUY:
            update_cooldown(ticker, signal.signal_type)
        save_last_signal(ticker, name)
        logger.info(
            "[%s] %s %s 알림 전송 (%d명, score=%d, AI판단=%s)",
            signal.strength.name, name, signal.signal_type.value,
            len(target_ids), signal.score, ai_decision or "실패",
        )

    _auto_trade(ticker, name, signal, stock, notifier, ai_decision)


# ---------------------------------------------------------------------------
# 신호 감지 메인
# ---------------------------------------------------------------------------

def check_signals() -> None:
    """1분마다: 화이트리스트 종목 신호 감지.

    매크로 레짐에 따라 전략 분기:
    - CRISIS → 위기장 평균회귀 (RSI2 급락 매수)
    - CAUTION/NORMAL → 변동성 돌파 + 합산 거부권
    """
    if not is_market_hours():
        return
    data = load_kiwoom_data()
    if not data:
        return

    # ── 32bit 수집기 생존 확인 ──
    # kiwoom_data가 5분 이상 갱신 안 되면 신규 매수 차단 (청산만 허용)
    _collector_alive = True
    updated_at = data.get("updated_at", "")
    if updated_at:
        try:
            from datetime import datetime as _dt
            _age = (_dt.now() - _dt.strptime(updated_at, "%Y-%m-%dT%H:%M:%S")).total_seconds()
            if _age > 300:  # 5분
                logger.warning("[수집기 사망 의심] kiwoom_data %.0f초 전 갱신 — 신규 매수 차단", _age)
                _collector_alive = False
        except (ValueError, TypeError):
            pass

    # ── 매크로 레짐 체크 ──
    from strategies.macro_regime import assess_current, MacroRegime
    macro = assess_current()

    # 위기MR 포지션 복구 (재시작 시, 레짐 무관)
    _restore_crisis_mr_position()

    # 위기MR 포지션이 열려 있으면 레짐과 무관하게 청산 로직 실행
    from alerts import crisis_manager as _cm
    had_position = _cm._crisis_mr_position is not None
    if had_position:
        _check_crisis_meanrev(data)

    if macro.regime in (MacroRegime.CRISIS, MacroRegime.CAUTION):
        if macro.regime == MacroRegime.CAUTION:
            logger.debug("[매크로] CAUTION — VB/combo 차단, 위기MR만 허용")
        if not had_position and _cm._crisis_mr_position is None and _collector_alive:
            _check_crisis_meanrev(data)
        # score 모드에서 기존 포지션의 매도 신호는 허용
        if _STRATEGY.name == "score_veto" and _collector_alive:
            from ai.ai_analyzer import AIAnalyzer
            _n = TelegramNotifier()
            _a = AIAnalyzer()
            now = datetime.now()
            if now.minute % 5 == 0:
                for ticker, info in data.get("stocks", {}).items():
                    try:
                        positions = load_auto_positions()
                        if ticker not in positions:
                            continue
                        if info.get("current_price", 0) == 0:
                            continue
                        name = info.get("name", ticker)
                        _run_signal_for_stock(ticker, name, info, _n, _a)
                    except Exception:
                        pass
        return

    from ai.ai_analyzer import AIAnalyzer
    notifier = TelegramNotifier()
    ai = AIAnalyzer()

    now = datetime.now()

    # ── 수집기 사망 시 신규 매수 전면 차단 (청산만 허용) ──
    if not _collector_alive:
        logger.warning("[수집기 사망] 신규 매수 전면 차단")
        return

    # ── 변동성 돌파 / 콤보 전략: 매분 체크 ──
    if _STRATEGY.name in ("volatility_breakout", "combo"):
      for ticker, info in data.get("stocks", {}).items():
        try:
            current_price = int(info.get("current_price", 0))
            if current_price == 0:
                continue
            if not is_whitelisted(ticker):
                continue
            name = info.get("name", ticker)

            # 변동성 돌파 신호 감지
            candles_1d = info.get("candles_1d", [])
            if len(candles_1d) < 12:
                continue

            # 5분봉 DataFrame (거부권 체크용)
            # 주의: kiwoom_data.json의 "candles_1m" 키는 실제로 5분봉 데이터
            # (키 이름이 1m이지만 opt10080 = 5분봉 조회 결과)
            df_5m = candles_to_df(info.get("candles_1m", []))
            df_5m_ind = calc_indicators(df_5m)

            # combo: 거부권용 5분봉 (20봉 이상 필요)
            # VB 전용 모드: 거부권 불필요 → 빈 DataFrame으로 통과
            if _STRATEGY.name == "combo":
                if df_5m_ind is not None and len(df_5m_ind) > 20:
                    candles_5m_for_veto = df_5m_ind.iloc[:-1].copy()
                else:
                    continue  # combo에서 거부권 데이터 부족 → 차단
            else:
                candles_5m_for_veto = pd.DataFrame()  # VB 전용: 거부권 안 씀

            # 장중 고가: 당일 세션 5분봉 high 최대값만 사용
            intraday_high = 0
            if df_5m is not None and not df_5m.empty and "high" in df_5m.columns:
                today_str = now.strftime("%Y%m%d")
                if "date" in df_5m.columns:
                    # candles_to_df가 date를 datetime으로 변환하므로 양쪽 형식 모두 대응
                    date_col = df_5m["date"].astype(str)
                    # "2026-04-03 09:05:00" → "20260403" 또는 "20260403" 그대로
                    today_bars = df_5m[date_col.str.replace("-", "").str.replace(" ", "").str[:8] == today_str]
                    if not today_bars.empty:
                        intraday_high = int(today_bars["high"].max())
                # 날짜 필터 실패 시 intraday_high=0 유지 (이전 세션 고가로 false breakout 방지)

            ctx = MarketContext(
                ticker=ticker,
                name=name,
                current_price=current_price,
                change_rate=float(info.get("change_rate") or 0.0),
                candles_5m=candles_5m_for_veto,
                candles_1d=pd.DataFrame(),
                exec_strength=float(info.get("exec_strength", 0.0)),
                orderbook=info.get("orderbook"),
                intraday_high=intraday_high,
                candles_1d_raw=candles_1d,
            )

            vb_signal = _STRATEGY.evaluate(ctx)

            if (vb_signal.signal_type == StratSignalType.BUY
                    and vb_signal.strength == StratSignalStrength.STRONG):
                # VB 전용 시간 제한
                if now.hour >= _TRADING_CONFIG.buy_end_hour:
                    continue
                # 쿨다운 체크
                ck_vb = f"vb_{ticker}"
                if not cooldown_ok(ck_vb, SignalType.BUY, SignalStrength.STRONG):
                    continue

                # AI 판단
                ai_decision = ""
                ai_text = ""
                # 5분봉 지표에서 RSI/MACD/거래량 추출 (VB 신호에는 없으므로)
                _vb_rsi = float("nan")
                _vb_macd = None
                _vb_vol = float("nan")
                if df_5m_ind is not None and len(df_5m_ind) > 0:
                    _last = df_5m_ind.iloc[-1]
                    _vb_rsi = float(_last.get("rsi", float("nan")))
                    _h = _last.get("macd_hist", None)
                    _ph = df_5m_ind.iloc[-2].get("macd_hist", None) if len(df_5m_ind) > 1 else None
                    if _h is not None and _ph is not None:
                        _vb_macd = "golden" if _ph < 0 and _h >= 0 else ("dead" if _ph > 0 and _h <= 0 else None)
                ai_result = ai.quick_signal_alert(
                    ticker=ticker, name=name,
                    price=current_price,
                    change_rate=info.get("change_rate", 0),
                    signal_reasons=vb_signal.reasons,
                    rsi=_vb_rsi, macd_cross=_vb_macd,
                    vol_ratio=_vb_vol,
                )
                ai_decision = ai_result.get("decision", "")
                ai_text = ai_result.get("text", "")

                # 알림
                target_str = f"{vb_signal.target_price:,}" if vb_signal.target_price else "N/A"
                header = (
                    f"🚀 [변동성 돌파] {name} ({ticker})\n"
                    f"💰 현재가: {current_price:,}원 / 목표가: {target_str}원\n"
                    f"📈 {', '.join(vb_signal.reasons[:3])}"
                )
                ai_block = f"\n[AI 분석]\n{ai_text}\n" if ai_text else ""
                full_msg = header + ai_block + CMD_FOOTER
                target_ids = get_users_for_ticker(ticker)
                ok = notifier.send_to_users(target_ids, full_msg)
                if ok:
                    # 매수 시간 전(09:00~09:09)이면 쿨다운 안 걸어서 09:10에 재시도
                    if not (now.hour == 9 and now.minute < _TRADING_CONFIG.buy_start_minute):
                        update_cooldown(ck_vb, SignalType.BUY)
                    save_last_signal(ticker, name)

                # 자동매매 (SignalResult 변환)
                compat_signal = SignalResult(
                    signal_type=SignalType.BUY,
                    strength=SignalStrength.STRONG,
                    score=int(vb_signal.score),
                    reasons=vb_signal.reasons,
                    rsi=vb_signal.rsi,
                    macd_cross=vb_signal.macd_cross,
                    vol_ratio=vb_signal.vol_ratio,
                )
                _auto_trade(ticker, name, compat_signal, info, notifier, ai_decision)

        except Exception as e:
            logger.error("[변동성돌파] %s 에러: %s", ticker, e)

    # ── 합산 전략: strategy=score일 때만 + 5분 봉 경계에서만 ──
    if _STRATEGY.name == "score_veto":
        if now.minute % 5 != 0:
            return

        for ticker, info in data.get("stocks", {}).items():
            try:
                if info.get("current_price", 0) == 0:
                    continue
                if not is_whitelisted(ticker):
                    continue
                name = info.get("name", ticker)
                _run_signal_for_stock(ticker, name, info, notifier, ai)
            except Exception as e:
                logger.error("[신호감지] %s 에러: %s", ticker, e)


# ---------------------------------------------------------------------------
# 일봉 스윙 신호
# ---------------------------------------------------------------------------

def check_daily_signals() -> None:
    """30분마다: 일봉 스윙 신호 (대형주 임계값 완화)."""
    if not is_market_hours():
        return
    # CRISIS/CAUTION 모드에서는 일봉 신규 매수 차단 (기존 포지션 매도는 허용)
    from strategies.macro_regime import assess_current, MacroRegime
    _macro = assess_current()
    _block_buy = _macro.regime in (MacroRegime.CRISIS, MacroRegime.CAUTION)
    data = load_kiwoom_data()
    if not data:
        return

    from alerts.signal_detector import detect_daily
    from ai.ai_analyzer import AIAnalyzer
    notifier = TelegramNotifier()
    ai = AIAnalyzer()

    for ticker, info in data.get("stocks", {}).items():
        try:
            if info.get("current_price", 0) == 0:
                continue
            if not is_whitelisted(ticker):
                continue

            candles_1d = info.get("candles_1d", [])
            if len(candles_1d) < 65:
                continue

            name = info.get("name", ticker)
            df = candles_to_df(candles_1d)
            df_ind = calc_indicators(df)
            if df_ind is None:
                continue

            try:
                change_rate = float(info.get("change_rate") or 0.0)
            except (ValueError, TypeError):
                change_rate = 0.0

            signal = detect_daily(df_ind, change_rate=change_rate)

            # 스윙 모드: 일봉 score >= 4이면 전체 종목 STRONG 승격
            is_largecap = ticker in LARGECAP_DAILY_THRESHOLD
            if signal.signal_type == SignalType.NEUTRAL:
                continue
            if signal.strength != SignalStrength.STRONG:
                if abs(signal.score) >= 4:
                    logger.info(
                        "[스윙 승격] %s 일봉 score=%d → STRONG",
                        name, signal.score,
                    )
                    signal = SignalResult(
                        signal_type=signal.signal_type,
                        strength=SignalStrength.STRONG,
                        score=signal.score,
                        reasons=signal.reasons,
                        warnings=signal.warnings,
                        rsi=signal.rsi,
                        macd_cross=signal.macd_cross,
                        vol_ratio=signal.vol_ratio,
                    )
                elif is_largecap and abs(signal.score) >= LARGECAP_DAILY_MIN_SCORE:
                    logger.info(
                        "[대형주 완화] %s 일봉 score=%d → STRONG 승격",
                        name, signal.score,
                    )
                    signal = SignalResult(
                        signal_type=signal.signal_type,
                        strength=SignalStrength.STRONG,
                        score=signal.score,
                        reasons=signal.reasons,
                        warnings=signal.warnings,
                        rsi=signal.rsi,
                        macd_cross=signal.macd_cross,
                        vol_ratio=signal.vol_ratio,
                    )
                else:
                    continue

            # 매도 신호: 보유 종목만
            if signal.signal_type == SignalType.SELL:
                positions = load_auto_positions()
                if ticker not in positions:
                    continue

            ck = f"{ticker}_daily_{signal.signal_type.value}"
            if not cooldown_ok(ck, signal.signal_type, signal.strength):
                continue

            # AI + 알림 + 자동매매
            ai_decision = ""
            ai_text = ""
            if signal.signal_type == SignalType.BUY:
                ai_result = ai.quick_signal_alert(
                    ticker=ticker, name=name,
                    price=info.get("current_price", 0),
                    change_rate=change_rate,
                    signal_reasons=signal.reasons,
                    rsi=signal.rsi, macd_cross=signal.macd_cross,
                    vol_ratio=signal.vol_ratio,
                )
                ai_decision = ai_result.get("decision", "")
                ai_text = ai_result.get("text", "")

            header = f"📊 [일봉 스윙] {build_signal_header(ticker, name, signal, info)}"
            ai_block = f"\n[AI 분석]\n{ai_text}\n" if ai_text else ""
            notifier.send_to_users(get_users_for_ticker(ticker), header + ai_block + CMD_FOOTER)
            update_cooldown(ck, signal.signal_type)
            save_last_signal(ticker, name)

            # CRISIS/CAUTION 모드에서는 매수 차단, 매도만 허용
            if _block_buy and signal.signal_type == SignalType.BUY:
                continue
            _auto_trade(ticker, name, signal, info, notifier, ai_decision)

        except Exception as e:
            logger.error("[일봉 신호] %s 에러: %s", ticker, e)


# ---------------------------------------------------------------------------
# 급등락 알림
# ---------------------------------------------------------------------------

def check_interest_spikes() -> None:
    """1분마다: 화이트리스트 종목 급등락 감지."""
    if not is_market_hours():
        return
    data = load_kiwoom_data()
    if not data:
        return

    notifier = TelegramNotifier()
    for ticker, info in data.get("stocks", {}).items():
        price = int(info.get("current_price", 0))
        change_rate = float(info.get("change_rate") or 0)
        name = info.get("name", ticker)

        if price == 0:
            continue
        if not is_whitelisted(ticker):
            continue
        if abs(change_rate) < INTEREST_SPIKE_THRESHOLD:
            continue

        ck = f"spike_{ticker}"
        if not cooldown_ok(ck, SignalType.BUY, SignalStrength.MEDIUM):
            continue

        emoji = "🚀" if change_rate > 0 else "📉"
        msg = (
            f"{emoji} [{name}] 급{'등' if change_rate > 0 else '락'} 감지\n"
            f"현재가: {price:,}원 ({change_rate:+.1f}%)"
            + CMD_FOOTER
        )
        notifier.send_to_users(get_users_for_ticker(ticker), msg)
        update_cooldown(ck, SignalType.BUY)


# ---------------------------------------------------------------------------
# 목표가/손절가 감시
# ---------------------------------------------------------------------------

def check_targets() -> None:
    """1분마다: targets.json 기반 알림."""
    if not is_market_hours():
        return
    data = load_kiwoom_data()
    if not data:
        return

    from trading.targets import check_targets as _check
    alerts = _check(data)
    if not alerts:
        return

    notifier = TelegramNotifier()
    for a in alerts:
        ck = f"target_{a['ticker']}_{a['type']}"
        if not cooldown_ok(ck, SignalType.BUY, SignalStrength.STRONG):
            continue
        emoji = "🎯" if a["type"] == "target" else "🛑"
        label = "목표가" if a["type"] == "target" else "손절가"
        msg = (
            f"{emoji} [{a['name']}] {label} 도달!\n"
            f"설정: {a['price']:,}원 → 현재: {a['current_price']:,}원"
            + CMD_FOOTER
        )
        notifier.send_to_users([get_admin_id()], msg)
        update_cooldown(ck, SignalType.BUY)


# ---------------------------------------------------------------------------
# 주문 상태 체크
# ---------------------------------------------------------------------------

def check_order_status() -> None:
    """1분마다: order_queue.json 체결/실패 감지 → 알림."""
    global _order_init_done

    try:
        with open(ORDER_QUEUE_PATH, "r", encoding="utf-8") as f:
            queue = json.load(f)
    except Exception:
        return

    orders = queue.get("orders", [])
    if not orders:
        return

    # 첫 실행: 시딩 (재시작 중복 알림 방지)
    if not _order_init_done:
        for order in orders:
            if order.get("status") in ("executed", "failed"):
                _notified_orders.add(order.get("id", ""))
        _order_init_done = True

        # 재시작 시 zombie selling + 분할익절 포지션 정리
        positions = load_auto_positions()
        if positions:
            pos_changed = False
            # 분할익절 체결/실패 정리
            for order in orders:
                if order.get("side") != "sell":
                    continue
                if order.get("rule_name") != "자동청산_분할익절":
                    continue
                ticker = order.get("ticker", "")
                if ticker not in positions:
                    continue
                p_sell_oid = positions[ticker].get("partial_sell_order_id", "")
                if p_sell_oid and p_sell_oid == order.get("id", ""):
                    if order.get("status") == "executed":
                        partial_qty = positions[ticker].pop("partial_sell_qty", 0)
                        positions[ticker].pop("partial_sell_order_id", None)
                        if partial_qty > 0:
                            positions[ticker]["qty"] = max(1, int(positions[ticker].get("qty", 0)) - partial_qty)
                        pos_changed = True
                        logger.info("[재시작 정리] %s 분할익절 체결 → qty 조정", ticker)
                    elif order.get("status") == "failed":
                        positions[ticker].pop("partial_sell_qty", None)
                        positions[ticker].pop("partial_sell_order_id", None)
                        pos_changed = True

            # 매도 체결 buy_price 백필 (일일 손실 집계용) + 디스크 저장
            _backfilled = False
            for order in orders:
                if order.get("status") != "executed" or order.get("side") != "sell":
                    continue
                if "buy_price" not in order:
                    ticker = order.get("ticker", "")
                    if ticker in positions:
                        order["buy_price"] = positions[ticker].get("buy_price", 0)
                        _backfilled = True
            if _backfilled:
                try:
                    _oq_tmp = Path(ORDER_QUEUE_PATH).with_suffix(".tmp")
                    with open(_oq_tmp, "w", encoding="utf-8") as f:
                        json.dump(queue, f, ensure_ascii=False, indent=2)
                    _oq_tmp.replace(ORDER_QUEUE_PATH)
                except Exception as e:
                    logger.error("[재시작] buy_price 백필 저장 실패: %s", e)

            # zombie selling 포지션 정리
            for order in orders:
                status = order.get("status", "")
                side = order.get("side", "")
                ticker = order.get("ticker", "")
                rule_name = order.get("rule_name", "")
                if side != "sell" or ticker not in positions:
                    continue
                if not rule_name.startswith(("자동매매_", "자동청산_", "스윙_")):
                    continue
                if not positions[ticker].get("selling"):
                    continue
                # sell_order_id 매칭
                pos_sell_oid = positions[ticker].get("sell_order_id", "")
                order_oid = order.get("id", "")
                if pos_sell_oid and order_oid and pos_sell_oid != order_oid:
                    continue
                if status == "executed":
                    _bp = positions[ticker].get("buy_price", 0)
                    _qty = int(positions[ticker].get("qty", 0))
                    try:
                        _ep = int(order.get("exec_price") or 0)
                    except (ValueError, TypeError):
                        _ep = 0
                    if _bp > 0 and _ep > 0 and _qty > 0:
                        _pnl = (_ep - _bp) * _qty
                        if _pnl < 0:
                            record_loss_and_stoploss(abs(_pnl))
                        else:
                            reset_consec_stoploss()
                    del positions[ticker]
                    _sell_fail_count.pop(ticker, None)
                    pos_changed = True
                    logger.info("[재시작 정리] %s 매도 체결 → 포지션 삭제", ticker)
                elif status == "failed":
                    positions[ticker].pop("selling", None)
                    positions[ticker].pop("sell_order_id", None)
                    pos_changed = True
                    logger.info("[재시작 정리] %s 매도 실패 → selling 해제", ticker)
            if pos_changed:
                save_auto_positions(positions)
        return

    notifier = TelegramNotifier()

    # submitted 3분 타임아웃
    _timeout_ids = []
    for order in orders:
        if order.get("status") == "submitted":
            submitted_at = order.get("submitted_at", "")
            if submitted_at:
                try:
                    elapsed = (datetime.now() - datetime.strptime(submitted_at, "%Y-%m-%dT%H:%M:%S")).total_seconds()
                    if elapsed > 180:
                        _timeout_ids.append(order.get("id"))
                        order["status"] = "failed"
                        order["fail_reason"] = "submitted 3분 타임아웃"
                        logger.warning("[타임아웃] %s 3분 미체결 → failed", order.get("ticker"))
                except Exception:
                    pass

    if _timeout_ids:
        try:
            with open(ORDER_QUEUE_PATH, "r", encoding="utf-8") as f:
                fresh_queue = json.load(f)
            _changed = False
            for fo in fresh_queue.get("orders", []):
                if fo.get("id") in _timeout_ids and fo.get("status") == "submitted":
                    fo["status"] = "failed"
                    fo["fail_reason"] = "submitted 3분 타임아웃"
                    _changed = True
            if _changed:
                _qtmp = Path(ORDER_QUEUE_PATH).with_suffix(".tmp")
                with open(_qtmp, "w", encoding="utf-8") as f:
                    json.dump(fresh_queue, f, ensure_ascii=False, indent=2)
                _qtmp.replace(ORDER_QUEUE_PATH)
                orders = fresh_queue.get("orders", [])
        except Exception as e:
            logger.error("[타임아웃] 저장 실패: %s", e)

    # 체결/실패 알림
    for order in orders:
        order_id = order.get("id", "")
        status = order.get("status", "")
        if not order_id or status not in ("executed", "failed"):
            continue
        if order_id in _notified_orders:
            continue

        side = order.get("side", "")
        ticker = order.get("ticker", "")
        quantity = int(order.get("quantity", 0))
        price = int(order.get("price", 0))
        rule_name = order.get("rule_name", "")
        mock_mode = order.get("mock_mode", False)

        # 종목명 조회
        try:
            from data.users_manager import get_all_interests
            name = get_all_interests().get(ticker, ticker)
        except Exception:
            name = ticker

        side_kor = "매수" if side == "buy" else "매도"
        emoji = "✅" if status == "executed" else "❌"
        label = "체결 완료" if status == "executed" else "실패"

        lines = [f"{emoji} [주문 {label}] {name} ({ticker})", "━" * 23]
        if mock_mode:
            lines.append("  [테스트 모드]")
        lines += [f"구분:   {side_kor}", f"수량:   {quantity:,}주"]
        if price > 0:
            lines.append(f"가격:   {price:,}원")
        if rule_name:
            lines.append(f"규칙:   {rule_name}")

        msg = "\n".join(lines) + CMD_FOOTER
        try:
            ok = notifier.send_to_users([get_admin_id()], msg)
            if ok:
                _notified_orders.add(order_id)
                logger.info("[주문 %s] %s %s %d주 (ID=%s)", label, name, side_kor, quantity, order_id[:8])
        except Exception as e:
            logger.error("[주문 알림 실패] %s: %s", order_id, e)

        # 분할 익절 체결/실패 → qty 조정 (포지션 삭제 안 함)
        if side == "sell" and rule_name == "자동청산_분할익절":
            positions = load_auto_positions()
            if ticker in positions:
                p_sell_oid = positions[ticker].get("partial_sell_order_id", "")
                if p_sell_oid and p_sell_oid == order_id:
                    if status == "executed":
                        partial_qty = positions[ticker].pop("partial_sell_qty", 0)
                        positions[ticker].pop("partial_sell_order_id", None)
                        if partial_qty > 0:
                            positions[ticker]["qty"] = max(1, int(positions[ticker].get("qty", 0)) - partial_qty)
                        save_auto_positions(positions)
                        logger.info("[분할 익절 체결] %s %d주 확정", name, partial_qty)
                    elif status == "failed":
                        positions[ticker].pop("partial_sell_qty", None)
                        positions[ticker].pop("partial_sell_order_id", None)
                        save_auto_positions(positions)
                        logger.warning("[분할 익절 실패] %s qty 원복 (변경 없음)", name)
            continue  # 분할 익절은 아래 포지션 삭제 로직으로 가면 안 됨

        # 매도 체결 → 포지션 삭제 + 손익 기록
        if status == "executed" and side == "sell" and rule_name.startswith(("자동매매_", "자동청산_", "스윙_")):
            positions = load_auto_positions()
            if ticker in positions:
                pos_data = positions[ticker]
                saved_sell_oid = pos_data.get("sell_order_id", "")
                if not pos_data.get("selling"):
                    logger.warning("[포지션 보호] %s selling 미설정 → 삭제 안 함", name)
                elif saved_sell_oid and saved_sell_oid != order_id:
                    logger.warning("[포지션 보호] %s sell_order_id 불일치 → 삭제 안 함", name)
                else:
                    _bp = pos_data.get("buy_price", 0)
                    try:
                        _ep = int(order.get("exec_price") or 0)
                    except (ValueError, TypeError):
                        _ep = 0
                    if _bp > 0 and _ep > 0:
                        _pnl = (_ep - _bp) * quantity
                        if _pnl < 0:
                            record_loss_and_stoploss(abs(_pnl))
                        else:
                            reset_consec_stoploss()
                    # order에 buy_price 기록 (일일 손실 집계용)
                    order["buy_price"] = _bp
                    try:
                        with open(ORDER_QUEUE_PATH, "r", encoding="utf-8") as f:
                            _oq = json.load(f)
                        for _o in _oq.get("orders", []):
                            if _o.get("id") == order_id:
                                _o["buy_price"] = _bp
                                break
                        _oq_tmp = Path(ORDER_QUEUE_PATH).with_suffix(".tmp")
                        with open(_oq_tmp, "w", encoding="utf-8") as f:
                            json.dump(_oq, f, ensure_ascii=False, indent=2)
                        _oq_tmp.replace(ORDER_QUEUE_PATH)
                    except Exception as e:
                        logger.error("[buy_price 기록] order_queue 저장 실패: %s", e)
                    del positions[ticker]
                    _sell_fail_count.pop(ticker, None)
                    save_auto_positions(positions)
                    logger.info("[포지션 삭제] %s 매도 체결 확인", name)

        # 매수 체결 → 포지션 추가
        if status == "executed" and side == "buy" and rule_name.startswith(("자동매매_", "스윙_")):
            _buy_in_progress.discard(ticker)
            positions = load_auto_positions()
            if ticker not in positions:
                try:
                    _ep = int(order.get("exec_price") or price)
                except (ValueError, TypeError):
                    _ep = price
                positions[ticker] = {
                    "name": name,
                    "qty": quantity,
                    "buy_price": _ep,
                    "bought_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "order_id": order_id,
                    "high_price": _ep,
                    "rule_name": rule_name,
                }
                save_auto_positions(positions)
                logger.info("[포지션 추가] %s %d주 @%s", name, quantity, f"{_ep:,}")

        # 매수 실패 → _buy_in_progress 정리
        if status == "failed" and side == "buy":
            _buy_in_progress.discard(ticker)


# ---------------------------------------------------------------------------
# 주문 큐 정리
# ---------------------------------------------------------------------------

def _prune_order_queue() -> None:
    """06:00: 7일 초과 완료 주문 정리."""
    try:
        with open(ORDER_QUEUE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    cutoff = datetime.now() - timedelta(days=7)
    original = len(data.get("orders", []))
    data["orders"] = [
        o for o in data.get("orders", [])
        if o.get("status") in ("pending", "submitted")
        or _parse_dt(o.get("created_at", "")) > cutoff
    ]
    pruned = original - len(data["orders"])
    if pruned > 0:
        tmp = Path(ORDER_QUEUE_PATH).with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(ORDER_QUEUE_PATH)
        logger.info("[주문 정리] %d건 삭제 (7일 초과)", pruned)


def _parse_dt(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return datetime.min


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
    schedule.every(30).minutes.do(check_daily_signals)
    schedule.every(1).minutes.do(check_targets)
    schedule.every(1).minutes.do(check_order_status)
    schedule.every(1).minutes.do(check_interest_spikes)
    schedule.every(1).minutes.do(check_auto_positions)
    schedule.every().day.at("08:30").do(send_premarket_news)
    schedule.every().day.at("09:00").do(send_market_open)
    schedule.every().day.at("15:40").do(send_market_close)
    schedule.every().hour.at(":00").do(send_news_update)
    schedule.every().day.at("16:00").do(send_daily_report)
    schedule.every().day.at("06:00").do(_prune_order_queue)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    from alerts.telegram_commander import start_telegram_commander
    start_telegram_commander()
    run_scheduler()
