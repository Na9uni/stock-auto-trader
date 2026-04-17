"""신호 감지 — 공통 헬퍼 + 단일 루프 메인 + 급등락/목표가 감시.

Functions:
    _build_market_context  : 공통 MarketContext 생성
    _process_signal        : 신호 처리 (알림 + AI + 매매)
    check_signals          : 1분마다 화이트리스트 종목 신호 감지
    check_interest_spikes  : 1분마다 급등락 감지
    check_targets          : 1분마다 목표가/손절가 감시
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from alerts.telegram_notifier import TelegramNotifier
from config.whitelist import is_whitelisted
from strategies.base import MarketContext, SignalResult, SignalType, SignalStrength

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


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------

def _build_market_context(
    ticker: str, name: str, info: dict, now: datetime,
) -> MarketContext | None:
    """공통 MarketContext 생성. 데이터 부족 시 None 반환."""
    candles_1d = info.get("candles_1d", [])
    if len(candles_1d) < 12:
        logger.warning("[신호] %s 일봉 부족 (%d개 < 12) — 전략 평가 불가", name, len(candles_1d))
        return None

    current_price = int(info.get("current_price", 0))
    if current_price == 0:
        return None

    # 5분봉 DataFrame (거부권 체크용)
    # NOTE: "candles_1m" 키는 역사적 이유로 5분봉 데이터를 저장합니다.
    # opt10080 (5분봉 조회) 결과이며, 실제 1분봉이 아닙니다.
    # 키 이름 변경 시 signal_runner.py, position_manager.py 등 전체 수정 필요.
    df_5m = candles_to_df(info.get("candles_1m", []))
    df_5m_ind = calc_indicators(df_5m)

    # combo/auto: 거부권용 5분봉 (20봉 이상 필요)
    # 그 외: 거부권 불필요 → 빈 DataFrame으로 통과
    candles_5m_for_veto = pd.DataFrame()
    if _STRATEGY.name in ("combo", "auto", "score_veto"):
        if df_5m_ind is not None and len(df_5m_ind) > 20:
            candles_5m_for_veto = df_5m_ind.iloc[:-1].copy()

    # 장중 고가: 당일 세션 5분봉 high 최대값만 사용
    intraday_high = 0
    if df_5m is not None and not df_5m.empty and "high" in df_5m.columns:
        today_str = now.strftime("%Y%m%d")
        if "date" in df_5m.columns:
            # candles_to_df가 date를 datetime으로 변환하므로 양쪽 형식 모두 대응
            date_col = df_5m["date"].astype(str)
            # "2026-04-03 09:05:00" → "20260403" 또는 "20260403" 그대로
            today_bars = df_5m[
                date_col.str.replace("-", "").str.replace(" ", "").str[:8] == today_str
            ]
            if not today_bars.empty:
                intraday_high = int(today_bars["high"].max())
        # 날짜 필터 실패 시 intraday_high=0 유지 (이전 세션 고가로 false breakout 방지)

    logger.debug(
        "[신호] %s 컨텍스트 생성: 현재가=%s, 일봉=%d개, 5분봉=%d개, 장중고가=%s",
        name, f"{current_price:,}", len(candles_1d),
        len(candles_5m_for_veto) if candles_5m_for_veto is not None and not candles_5m_for_veto.empty else 0,
        f"{intraday_high:,}",
    )
    return MarketContext(
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


def _process_signal(
    ticker: str, name: str, info: dict,
    signal: SignalResult, notifier, ai, now: datetime,
) -> None:
    """신호 처리: 알림 + AI + 매매 (BUY/SELL 공통)."""
    logger.debug(
        "[신호] %s 전략결과: %s %s (score=%.1f, 사유=%s)",
        name, signal.signal_type.value, signal.strength.value if hasattr(signal.strength, 'value') else signal.strength.name,
        signal.score, "; ".join(signal.reasons[:2]) if signal.reasons else "없음",
    )
    if signal.signal_type == SignalType.NEUTRAL:
        return
    if signal.strength != SignalStrength.STRONG:
        logger.debug("[신호] %s 강도 부족 (%s != STRONG) → 매매 스킵", name, signal.strength.name)
        return

    # 매수 시간 제한
    if signal.signal_type == SignalType.BUY:
        if now.hour >= _TRADING_CONFIG.buy_end_hour:
            logger.debug("[신호] %s 매수 시간 초과 (%d시 >= %d시) → 스킵", name, now.hour, _TRADING_CONFIG.buy_end_hour)
            return
        if now.hour == 9 and now.minute < _TRADING_CONFIG.buy_start_minute:
            logger.debug("[신호] %s 장 시작 %d분 미만 → 매수 보류", name, _TRADING_CONFIG.buy_start_minute)
            return

    # 쿨다운
    ck = f"{_STRATEGY.name}_{ticker}"
    if not cooldown_ok(ck, signal.signal_type, signal.strength):
        logger.debug("[신호] %s 쿨다운 중 → 스킵", name)
        return

    # AI 분석 (매수 신호만)
    ai_decision = ""
    ai_text = ""
    if signal.signal_type == SignalType.BUY:
        # 5분봉 지표에서 RSI/MACD/거래량 추출 (VB 등 일봉 전략은 자체 값 없으므로)
        _sig_rsi = signal.rsi
        _sig_macd = signal.macd_cross
        _sig_vol = signal.vol_ratio
        if pd.isna(_sig_rsi):
            # NOTE: "candles_1m" 키는 실제로 5분봉 (opt10080) 데이터
            df_5m = candles_to_df(info.get("candles_1m", []))
            df_5m_ind = calc_indicators(df_5m)
            if df_5m_ind is not None and len(df_5m_ind) > 0:
                _last = df_5m_ind.iloc[-1]
                _sig_rsi = float(_last.get("rsi", float("nan")))
                _h = _last.get("macd_hist", None)
                _ph = (
                    df_5m_ind.iloc[-2].get("macd_hist", None)
                    if len(df_5m_ind) > 1 else None
                )
                if _h is not None and _ph is not None:
                    _sig_macd = (
                        "golden" if _ph < 0 and _h >= 0
                        else ("dead" if _ph > 0 and _h <= 0 else None)
                    )

        # vol_ratio가 nan이면 kiwoom_data에서 직접 계산
        import math
        if math.isnan(_sig_vol):
            _cur_vol = info.get("volume", 0)
            _prev_vol = info.get("prev_volume", 0)
            if _prev_vol > 0:
                _sig_vol = _cur_vol / _prev_vol
        _exec = float(info.get("exec_strength", 0.0))

        ai_result = ai.quick_signal_alert(
            ticker=ticker, name=name,
            price=int(info.get("current_price", 0)),
            change_rate=info.get("change_rate", 0),
            signal_reasons=signal.reasons,
            rsi=_sig_rsi, macd_cross=_sig_macd,
            vol_ratio=_sig_vol,
            exec_strength=_exec,
        )
        ai_decision = ai_result.get("decision", "")
        ai_text = ai_result.get("text", "")

    # 알림
    header = build_signal_header(ticker, name, signal, info)
    ai_block = f"\n[AI 분석]\n{ai_text}\n" if ai_text else ""
    full_msg = header + ai_block + CMD_FOOTER

    target_ids = get_users_for_ticker(ticker)
    ok = notifier.send_to_users(target_ids, full_msg)
    if ok:
        # 매수 시간 전(09:00~09:09)이면 쿨다운 안 걸어서 시작 시간에 재시도
        in_buy_window = not (now.hour == 9 and now.minute < _TRADING_CONFIG.buy_start_minute)
        if in_buy_window or signal.signal_type != SignalType.BUY:
            update_cooldown(ck, signal.signal_type)
        save_last_signal(ticker, name)
        logger.info(
            "[%s] %s %s 알림 전송 (score=%.0f, AI판단=%s)",
            signal.strength.name, name, signal.signal_type.value,
            signal.score, ai_decision or "없음",
        )

    # 자동매매
    _auto_trade(ticker, name, signal, info, notifier, ai_decision)


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

    # ── EOD vs 보유 비교: 어제 청산 기록에 오늘 시가 채움 ──
    try:
        from trading.eod_tracker import fill_next_day_prices
        fill_next_day_prices(data)
    except Exception:
        pass

    # ── 4-Mode 레짐 감지 ──
    from strategies.regime_engine import get_regime_engine, RegimeState
    from alerts.market_guard import fetch_index_prices
    from strategies.macro_regime import assess_current

    engine = get_regime_engine()
    index_data = fetch_index_prices()
    macro_status = assess_current()

    # ATR/전일고저 조기 감지용: KOSPI ETF(069500) 일봉 데이터 전달
    kospi_candles = data.get("stocks", {}).get("069500", {}).get("candles_1d", [])
    regime = engine.detect(index_data, macro_status, kospi_candles=kospi_candles)
    regime_params = engine.params
    logger.debug("[신호] 레짐: %s, 파라미터: slots=%d, buy=%s", regime.value, regime_params.max_slots, regime_params.buy_allowed)

    # CASH/DEFENSE 레짐 처리
    from alerts.trade_executor import OPERATION_MODE as _OP_MODE
    if regime == RegimeState.CASH:
        if _OP_MODE == "MOCK":
            # MOCK 모드: 청산 스킵 (매 사이클 청산→매수 루프 방지). 스윙 유지 테스트 목적.
            logger.info("[MOCK] CASH 레짐 — 청산 스킵, 가상 매매 평가 계속")
        else:
            _execute_regime_liquidation(data, engine)
            return

    if regime == RegimeState.DEFENSE:
        if _OP_MODE == "MOCK":
            logger.info("[MOCK] DEFENSE 레짐 — 축소 스킵, 가상 매매 평가 계속")
        else:
            _execute_defense_cuts(data, engine)
            return

    from ai.ai_analyzer import AIAnalyzer
    notifier = TelegramNotifier()
    ai = AIAnalyzer()

    now = datetime.now()

    # ── 수집기 사망 시 신규 매수 전면 차단 (청산만 허용) ──
    if not _collector_alive:
        logger.warning("[수집기 사망] 신규 매수 전면 차단")
        return

    # ── score_veto: 5분봉 경계에서만 실행 ──
    if _STRATEGY.name == "score_veto" and now.minute % 5 != 0:
        return

    # ── 전략 평가: 단일 루프 ──
    from config.stock_screener import screen_ticker

    for ticker, info in data.get("stocks", {}).items():
        try:
            if not is_whitelisted(ticker):
                continue
            name = info.get("name", ticker)

            # 종목 품질 스크리너: 불량주 사전 필터링
            candles_1d = info.get("candles_1d", [])
            passed, reason = screen_ticker(ticker, info, candles_1d)
            if not passed:
                logger.debug("[스크리너] %s 제외: %s", name, reason)
                continue

            ctx = _build_market_context(ticker, name, info, now)
            if ctx is None:
                continue

            signal = _STRATEGY.evaluate(ctx)
            _process_signal(ticker, name, info, signal, notifier, ai, now)
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
# 레짐 기반 포지션 관리
# ---------------------------------------------------------------------------


def _execute_defense_cuts(data: dict, engine) -> None:
    """DEFENSE 모드: 비-manual 포지션 50% 축소 (1회만)."""
    from alerts.file_io import save_auto_positions
    from alerts.trade_executor import OPERATION_MODE

    positions = load_auto_positions()
    notifier = TelegramNotifier()

    for ticker, pos in list(positions.items()):
        if pos.get("manual") or pos.get("selling") or pos.get("defense_cut"):
            continue

        name = pos.get("name", ticker)
        qty = int(pos.get("qty", 0))
        cut_qty = qty // 2
        if cut_qty <= 0:
            continue

        cp = int(data.get("stocks", {}).get(ticker, {}).get("current_price", 0))
        if cp <= 0:
            continue

        bp = int(pos.get("buy_price", 0))
        pnl = (cp - bp) * cut_qty if bp > 0 else 0

        if OPERATION_MODE == "MOCK":
            logger.warning("[DEFENSE 축소] %s %d주 → %d주 (가상)", name, qty, qty - cut_qty)
            notifier.send_to_users(
                [get_admin_id()],
                f"[DEFENSE 비중 축소] {name}\n"
                f"수량: {qty}주 → {qty - cut_qty}주 ({cut_qty}주 매도)\n"
                f"손익: {pnl:+,}원\n"
                f"주의: 모의투자"
                + CMD_FOOTER,
            )
            from trading.trade_journal import record_trade
            record_trade(
                ticker=ticker, name=name, side="sell",
                quantity=cut_qty, price=cp,
                reason="DEFENSE 비중 축소",
                strategy="",
                mock=True,
                buy_price=bp,
                buy_time=pos.get("buy_time", ""),
                pnl=pnl,
            )
            pos["qty"] = qty - cut_qty
            pos["defense_cut"] = True
        else:
            from trading.auto_trader import execute_sell
            result = execute_sell(ticker, name, cut_qty, cp, rule_name="자동청산_DEFENSE축소")
            if result.get("status") == "pending":
                pos["defense_cut"] = True
                pos["selling"] = True
                pos["sell_order_id"] = result.get("order_id", "")
                logger.warning("[DEFENSE 축소] %s %d주 매도 접수", name, cut_qty)

    save_auto_positions(positions)


def _execute_regime_liquidation(data: dict, engine) -> None:
    """CASH 모드: 비-manual 포지션 전량 청산."""
    from alerts.file_io import save_auto_positions
    from alerts.trade_executor import OPERATION_MODE

    positions = load_auto_positions()
    notifier = TelegramNotifier()
    liquidated = []

    for ticker, pos in list(positions.items()):
        if pos.get("manual") or pos.get("selling"):
            continue

        name = pos.get("name", ticker)
        qty = int(pos.get("qty", 0))
        if qty <= 0:
            continue

        cp = int(data.get("stocks", {}).get(ticker, {}).get("current_price", 0))
        if cp <= 0:
            continue

        bp = int(pos.get("buy_price", 0))
        pnl = (cp - bp) * qty if bp > 0 else 0

        if OPERATION_MODE == "MOCK":
            logger.warning("[CASH 청산] %s 전량 %d주 매도 (가상)", name, qty)
            notifier.send_to_users(
                [get_admin_id()],
                f"[CASH 전량 청산] {name}\n"
                f"수량: {qty}주 / 매도가: {cp:,}원\n"
                f"손익: {pnl:+,}원\n"
                f"주의: 모의투자"
                + CMD_FOOTER,
            )
            from trading.trade_journal import record_trade
            record_trade(
                ticker=ticker, name=name, side="sell",
                quantity=qty, price=cp,
                reason="CASH 전량 청산",
                strategy="",
                mock=True,
                buy_price=bp,
                buy_time=pos.get("buy_time", ""),
                pnl=pnl,
            )
            if pnl < 0:
                record_loss_and_stoploss(abs(pnl), mock=True)
            else:
                reset_consec_stoploss(mock=True)
            del positions[ticker]
            liquidated.append(name)
        else:
            from trading.auto_trader import execute_sell
            result = execute_sell(ticker, name, qty, cp, rule_name="자동청산_CASH전량")
            if result.get("status") == "pending":
                pos["selling"] = True
                pos["sell_order_id"] = result.get("order_id", "")
                liquidated.append(name)

    save_auto_positions(positions)
    if liquidated:
        logger.warning("[CASH 청산] %d건: %s", len(liquidated), ", ".join(liquidated))


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
# 장 마감 전 강제 청산 (데이트레이딩 당일 청산 원칙)
# ---------------------------------------------------------------------------

def check_eod_liquidation() -> None:
    """15:20 실행: 상승장(VB) 모드 가상/실전 포지션 당일 강제 청산.

    - AUTO 전략의 상승장(데이트레이딩) 포지션만 대상
    - 추세추종(하락장) 포지션은 제외 (며칠 보유 가능)
    - manual 포지션 제외
    - EOD_LIQUIDATION=false면 스킵 (스윙 모드)
    """
    # EOD 청산 비활성화 (.env EOD_LIQUIDATION=false → 스윙 모드)
    if not _TRADING_CONFIG.eod_liquidation:
        logger.info("[장마감 청산] EOD_LIQUIDATION=false → 스킵 (스윙 모드)")
        return

    from alerts.file_io import save_auto_positions
    from alerts.trade_executor import OPERATION_MODE

    data = load_kiwoom_data()
    if not data:
        return

    positions = load_auto_positions()
    if not positions:
        return

    notifier = TelegramNotifier()
    liquidated = []

    for ticker, pos in list(positions.items()):
        # manual, 추세추종, 이미 매도 중인 포지션 제외
        if pos.get("manual"):
            continue
        if pos.get("strategy") == "trend_following":
            continue
        if pos.get("selling"):
            continue

        name = pos.get("name", ticker)
        qty = int(pos.get("qty", 0))
        bp = int(pos.get("buy_price", 0))
        cp = int(data.get("stocks", {}).get(ticker, {}).get("current_price", 0))

        if qty <= 0 or cp <= 0:
            continue

        pnl = (cp - bp) * qty
        pnl_pct = ((cp - bp) / bp * 100) if bp > 0 else 0
        emoji = "📈" if pnl >= 0 else "📉"

        if OPERATION_MODE == "MOCK":
            # 가상 매도
            logger.info(
                "[장마감 청산] %s 가상 매도 %d주 @%s (손익 %s원)",
                name, qty, f"{cp:,}", f"{pnl:+,}",
            )
            notifier.send_to_users(
                [get_admin_id()],
                f"💸 [장마감 강제 청산] {name} ({ticker})\n"
                f"💰 수량: {qty}주 / 매도가: {cp:,}원\n"
                f"📊 매수가: {bp:,}원\n"
                f"{emoji} 손익: {pnl:+,}원 ({pnl_pct:+.1f}%)\n"
                f"⏰ 데이트레이딩 당일 청산 원칙\n"
                f"⚠️ 모의투자 — 실제 돈은 사용되지 않았습니다"
                + CMD_FOOTER,
            )
            from trading.trade_journal import record_trade
            record_trade(
                ticker=ticker, name=name, side="sell",
                quantity=qty, price=cp,
                reason="EOD 장마감 청산",
                strategy="",
                mock=True,
                buy_price=bp,
                buy_time=pos.get("buy_time", ""),
                pnl=pnl,
            )
            # EOD vs 보유 비교 기록
            try:
                from trading.eod_tracker import record_eod_sell
                record_eod_sell(ticker, name, qty, cp, bp, pnl)
            except Exception:
                pass
            # 손실 기록
            if pnl < 0:
                record_loss_and_stoploss(abs(pnl), mock=True)
            else:
                reset_consec_stoploss(mock=True)
            del positions[ticker]
            liquidated.append(f"{name} {pnl:+,}원")
        else:
            # 실전: 매도 주문 접수
            from trading.auto_trader import execute_sell
            sell_result = execute_sell(ticker, name, qty, cp,
                                       rule_name="자동청산_장마감")
            if sell_result.get("status") == "pending":
                pos["selling"] = True
                pos["sell_order_id"] = sell_result.get("order_id", "")
                logger.info("[장마감 청산] %s 매도 접수 %d주", name, qty)
                notifier.send_to_users(
                    [get_admin_id()],
                    f"💸 [장마감 강제 청산] {name} ({ticker})\n"
                    f"💰 수량: {qty}주 / 현재가: {cp:,}원\n"
                    f"⏰ 데이트레이딩 당일 청산 원칙"
                    + CMD_FOOTER,
                )
                liquidated.append(name)

    save_auto_positions(positions)

    if liquidated:
        logger.info("[장마감 청산] %d건 완료: %s", len(liquidated), ", ".join(liquidated))


# ---------------------------------------------------------------------------
# 개장 전 US 선물 체크 (08:40 스케줄)
# ---------------------------------------------------------------------------

def check_premarket_us() -> None:
    """08:40 실행: US 야간 선물 확인 → 급락 시 레짐 선제 전환 + 텔레그램 경고."""
    from alerts.market_guard import fetch_index_prices
    from strategies.regime_engine import get_regime_engine, RegimeState
    from strategies.macro_regime import assess_current
    import math

    try:
        index_data = fetch_index_prices()
    except Exception as e:
        logger.warning("[개장전 US 체크] 지수 데이터 가져오기 실패: %s", e)
        return

    sp500 = index_data.get("S&P500", {})
    nasdaq = index_data.get("NASDAQ", {})
    sp500_change = sp500.get("change_pct", 0.0)
    nasdaq_change = nasdaq.get("change_pct", 0.0)
    if isinstance(sp500_change, float) and math.isnan(sp500_change):
        sp500_change = 0.0
    if isinstance(nasdaq_change, float) and math.isnan(nasdaq_change):
        nasdaq_change = 0.0

    worst_us = min(sp500_change, nasdaq_change)

    if worst_us >= -1.0:
        logger.info("[개장전 US 체크] S&P500 %+.1f%%, NASDAQ %+.1f%% — 정상", sp500_change, nasdaq_change)
        return

    # US 급락 감지 → 레짐 선제 전환
    engine = get_regime_engine()
    macro_status = assess_current()

    # detect() 호출하여 레짐 전환 (US 데이터가 반영됨)
    regime = engine.detect(index_data, macro_status)

    # 텔레그램 경고
    notifier = TelegramNotifier()
    if worst_us <= -5.0:
        emoji = "🔴"
        level = "긴급"
    elif worst_us <= -3.0:
        emoji = "🟠"
        level = "경고"
    else:
        emoji = "🟡"
        level = "주의"

    notifier.send_to_users(
        [get_admin_id()],
        f"{emoji} [개장 전 US 시장 {level}]\n"
        f"S&P500: {sp500_change:+.1f}%\n"
        f"NASDAQ: {nasdaq_change:+.1f}%\n"
        f"현재 레짐: {regime.value.upper()}\n"
        f"{'⚠️ 매수 차단 중' if not engine.params.buy_allowed else '매수 가능'}"
        + CMD_FOOTER,
    )
    logger.warning(
        "[개장전 US 체크] S&P500 %+.1f%%, NASDAQ %+.1f%% → 레짐 %s",
        sp500_change, nasdaq_change, regime.value,
    )


# ---------------------------------------------------------------------------
# 30분 정상 작동 알림 (아빠 요청)
# ---------------------------------------------------------------------------

def check_heartbeat() -> None:
    """30분마다: 시스템 정상 작동 알림 텔레그램 발송."""
    from alerts.market_guard import is_market_hours
    if not is_market_hours():
        return

    from alerts._state import _STRATEGY, _TRADING_CONFIG
    from alerts.file_io import load_auto_positions, load_kiwoom_data
    from strategies.regime_engine import get_regime_engine, RegimeState

    try:
        engine = get_regime_engine()
        regime = engine.state
        regime_emoji = {
            RegimeState.NORMAL: "🟢",
            RegimeState.SWING: "🟡",
            RegimeState.DEFENSE: "🟠",
            RegimeState.CASH: "🔴",
        }

        positions = load_auto_positions()
        auto_count = sum(1 for p in positions.values() if not p.get("manual"))
        manual_count = sum(1 for p in positions.values() if p.get("manual"))

        # 오늘 매매 건수 (trade_journal에서)
        today_buys = 0
        today_sells = 0
        today_pnl = 0
        try:
            from trading.trade_journal import get_daily_summary
            summary = get_daily_summary()
            today_buys = summary.get("buys", 0)
            today_sells = summary.get("sells", 0)
            today_pnl = summary.get("pnl", 0)
        except Exception:
            pass

        from datetime import datetime
        now = datetime.now()

        emoji = regime_emoji.get(regime, "⚪")
        msg = (
            f"✅ [정상 작동 중] {now.strftime('%H:%M')}\n"
            f"레짐: {emoji} {regime.value.upper()}\n"
            f"보유: 자동 {auto_count}종목 / manual {manual_count}종목\n"
            f"오늘: 매수 {today_buys}건 / 매도 {today_sells}건"
        )
        if today_pnl != 0:
            msg += f" / 손익 {today_pnl:+,}원"

        notifier = TelegramNotifier()
        notifier.send_to_users([get_admin_id()], msg)
        logger.info("[하트비트] 정상 작동 알림 발송")
    except Exception as e:
        logger.error("[하트비트] 알림 실패: %s", e)
