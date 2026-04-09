"""신호 감지 — 단일 종목 래퍼 + 전략별 메인 루프 + 급등락/목표가 감시.

Functions:
    _run_signal_for_stock  : 단일 종목 5분봉 신호 → AI → 매매
    check_signals          : 1분마다 화이트리스트 종목 신호 감지
    check_interest_spikes  : 1분마다 급등락 감지
    check_targets          : 1분마다 목표가/손절가 감시
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from alerts.signal_detector import SignalType, SignalStrength, SignalResult
from alerts.telegram_notifier import TelegramNotifier
from config.whitelist import is_whitelisted
from strategies.base import MarketContext
from strategies.base import SignalType as StratSignalType
from strategies.base import SignalStrength as StratSignalStrength

from alerts._state import (
    _TRADING_CONFIG,
    _STRATEGY,
    INTEREST_SPIKE_THRESHOLD,
    logger,
)

from alerts.file_io import (
    load_kiwoom_data,
    load_auto_positions,
    candles_to_df,
    calc_indicators,
)

from alerts.market_guard import (
    is_market_hours,
    cooldown_ok,
    update_cooldown,
    record_loss_and_stoploss,
    reset_consec_stoploss,
)

from alerts.trade_executor import (
    _auto_trade,
)

from alerts.notifications import (
    CMD_FOOTER,
    get_admin_id,
    get_users_for_ticker,
    save_last_signal,
    build_signal_header,
)

from alerts.crisis_manager import (
    _restore_crisis_mr_position,
    _check_crisis_meanrev,
)


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
                # P1-10: 텔레그램 긴급 알림 (쿨다운: 5분에 1번)
                _collector_death_ck = "collector_death"
                if cooldown_ok(_collector_death_ck, SignalType.BUY, SignalStrength.STRONG):
                    try:
                        TelegramNotifier().send_to_users(
                            [get_admin_id()],
                            f"🚨 [긴급] 키움 수집기 응답 없음!\n"
                            f"마지막 갱신: {_age:.0f}초 전\n"
                            f"신규 매수 차단 중. 확인 필요!"
                        )
                        update_cooldown(_collector_death_ck, SignalType.BUY)
                    except Exception:
                        pass
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
    if _STRATEGY.name in ("volatility_breakout", "combo", "auto"):
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
            if _STRATEGY.name in ("combo", "auto"):
                if df_5m_ind is not None and len(df_5m_ind) > 20:
                    candles_5m_for_veto = df_5m_ind.iloc[:-1].copy()
                else:
                    candles_5m_for_veto = pd.DataFrame()  # 데이터 부족 시 거부권 없이 진행
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

            # AUTO 하락장 모드: 데드크로스 매도 신호 처리
            elif (vb_signal.signal_type == StratSignalType.SELL
                    and vb_signal.strength == StratSignalStrength.STRONG
                    and _STRATEGY.name == "auto"):
                ck_sell = f"trend_sell_{ticker}"
                if not cooldown_ok(ck_sell, SignalType.SELL, SignalStrength.STRONG):
                    continue

                header = (
                    f"📉 [추세추종 데드크로스] {name} ({ticker})\n"
                    f"💰 현재가: {current_price:,}원\n"
                    f"📊 {', '.join(vb_signal.reasons[:3])}"
                )
                full_msg = header + CMD_FOOTER
                target_ids = get_users_for_ticker(ticker)
                ok = notifier.send_to_users(target_ids, full_msg)
                if ok:
                    update_cooldown(ck_sell, SignalType.SELL)

                # 자동매도 (보유 중이면)
                compat_signal = SignalResult(
                    signal_type=SignalType.SELL,
                    strength=SignalStrength.STRONG,
                    score=int(vb_signal.score),
                    reasons=vb_signal.reasons,
                )
                _auto_trade(ticker, name, compat_signal, info, notifier, "")

        except Exception as e:
            logger.error("[변동성돌파] %s 에러: %s", ticker, e)

    # ── 추세추종 전략: strategy=trend일 때 매분 체크 ──
    if _STRATEGY.name == "trend_following":
      for ticker, info in data.get("stocks", {}).items():
        try:
            current_price = int(info.get("current_price", 0))
            if current_price == 0:
                continue
            if not is_whitelisted(ticker):
                continue
            name = info.get("name", ticker)

            candles_1d = info.get("candles_1d", [])
            if len(candles_1d) < 61:
                continue

            ctx = MarketContext(
                ticker=ticker,
                name=name,
                current_price=current_price,
                change_rate=float(info.get("change_rate") or 0.0),
                candles_5m=pd.DataFrame(),
                candles_1d=pd.DataFrame(),
                exec_strength=float(info.get("exec_strength", 0.0)),
                orderbook=info.get("orderbook"),
                intraday_high=0,
                candles_1d_raw=candles_1d,
            )

            trend_signal = _STRATEGY.evaluate(ctx)

            # 매수 신호
            if (trend_signal.signal_type == StratSignalType.BUY
                    and trend_signal.strength == StratSignalStrength.STRONG):
                if now.hour >= _TRADING_CONFIG.buy_end_hour:
                    continue
                ck_trend = f"trend_{ticker}"
                if not cooldown_ok(ck_trend, SignalType.BUY, SignalStrength.STRONG):
                    continue

                header = (
                    f"📈 [추세추종 골든크로스] {name} ({ticker})\n"
                    f"💰 현재가: {current_price:,}원\n"
                    f"📊 {', '.join(trend_signal.reasons[:3])}"
                )
                full_msg = header + CMD_FOOTER
                target_ids = get_users_for_ticker(ticker)
                ok = notifier.send_to_users(target_ids, full_msg)
                if ok:
                    if not (now.hour == 9 and now.minute < _TRADING_CONFIG.buy_start_minute):
                        update_cooldown(ck_trend, SignalType.BUY)
                    save_last_signal(ticker, name)

                compat_signal = SignalResult(
                    signal_type=SignalType.BUY,
                    strength=SignalStrength.STRONG,
                    score=int(trend_signal.score),
                    reasons=trend_signal.reasons,
                )
                _auto_trade(ticker, name, compat_signal, info, notifier, "")

            # 매도 신호
            elif (trend_signal.signal_type == StratSignalType.SELL
                    and trend_signal.strength == StratSignalStrength.STRONG):
                header = (
                    f"📉 [추세추종 데드크로스] {name} ({ticker})\n"
                    f"💰 현재가: {current_price:,}원\n"
                    f"📊 {', '.join(trend_signal.reasons[:3])}"
                )
                full_msg = header + CMD_FOOTER
                target_ids = get_users_for_ticker(ticker)
                notifier.send_to_users(target_ids, full_msg)

        except Exception as e:
            logger.error("[추세추종] %s 에러: %s", ticker, e)

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
