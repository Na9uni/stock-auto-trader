"""포지션 관리 — 손절/트레일링/과매수 감시, 실패 포지션 정리."""

from __future__ import annotations

import json
import logging
from datetime import datetime

import pandas as pd

from alerts.file_io import (
    KIWOOM_DATA_PATH,
    ORDER_QUEUE_PATH,
    load_auto_positions,
    save_auto_positions,
    candles_to_df,
    calc_indicators,
)
from alerts.market_guard import record_loss_and_stoploss, reset_consec_stoploss
from alerts.notifications import get_admin_id, CMD_FOOTER
from alerts.telegram_notifier import TelegramNotifier
from alerts.trade_executor import _buy_in_progress

logger = logging.getLogger("stock_analysis")

# ---------------------------------------------------------------------------
# 인메모리 상태
# ---------------------------------------------------------------------------

_sell_fail_count: dict[str, int] = {}

# manual -15% 비상경보 중복 방지용 (티커별 30분 쿨다운 — 장시간 하락 시 스팸 방지)
_last_manual_emergency_alert: dict[str, datetime] = {}


# hard_stale 경보 파일 영속화 — 프로세스 재시작 플래핑 방지 (10분 쿨다운)
_HARD_STALE_PATH = __import__("pathlib").Path(__file__).parent.parent / "data" / "hard_stale_alert.json"
_HARD_STALE_COOLDOWN_SEC = 600


def _read_last_hard_stale() -> datetime | None:
    try:
        if not _HARD_STALE_PATH.exists():
            return None
        with open(_HARD_STALE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        ts = raw.get("last_alert_at")
        return datetime.fromisoformat(ts) if ts else None
    except Exception:
        return None


def _write_last_hard_stale(ts: datetime) -> None:
    try:
        _HARD_STALE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _HARD_STALE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"last_alert_at": ts.isoformat()}, f)
        import os as _os
        _os.replace(tmp, _HARD_STALE_PATH)
    except Exception as e:
        logger.warning("[hard_stale] 파일 저장 실패: %s", e)


def _notify_hard_stale(age: float) -> None:
    """Hard stale 상태를 텔레그램으로 1회 경보 (10분 쿨다운, 파일 영속화).

    재시작 시에도 쿨다운이 유지되어 재시작 플래핑 구간의 경보 중복을 방지.
    """
    now = datetime.now()
    last = _read_last_hard_stale()
    if last is not None and (now - last).total_seconds() < _HARD_STALE_COOLDOWN_SEC:
        return
    _write_last_hard_stale(now)
    try:
        TelegramNotifier().send_to_users(
            [get_admin_id()],
            f"⚠️ [수집기 데이터 지연] {int(age)}초 전 데이터\n"
            f"자동 포지션 손절/트레일링 일시 중단\n"
            f"→ 키움 수집기 상태 확인 필요"
            + CMD_FOOTER,
        )
    except Exception as e:
        logger.warning("[hard_stale] 텔레그램 경보 실패: %s", e)

# ---------------------------------------------------------------------------
# 설정 (analysis_scheduler에서 주입)
# ---------------------------------------------------------------------------

STOPLOSS_PCT: float = 0.0
TRAILING_ACTIVATE_PCT: float = 0.0
TRAILING_STOP_PCT: float = 0.0


def _configure(stoploss_pct: float, trailing_activate_pct: float, trailing_stop_pct: float) -> None:
    """analysis_scheduler에서 호출하여 설정값 주입."""
    global STOPLOSS_PCT, TRAILING_ACTIVATE_PCT, TRAILING_STOP_PCT
    STOPLOSS_PCT = stoploss_pct
    TRAILING_ACTIVATE_PCT = trailing_activate_pct
    TRAILING_STOP_PCT = trailing_stop_pct


# ---------------------------------------------------------------------------
# 실패 포지션 정리
# ---------------------------------------------------------------------------

def _cleanup_failed_positions(positions: dict) -> dict:
    """order_queue에서 매수/매도 실패한 포지션 정리."""
    try:
        with open(ORDER_QUEUE_PATH, "r", encoding="utf-8") as f:
            queue_data = json.load(f)
    except Exception:
        return positions

    orders = queue_data.get("orders", [])

    # 매수 실패 → 포지션 삭제
    failed_buy_tickers = {
        o.get("ticker") for o in orders
        if o.get("status") == "failed" and o.get("side") == "buy"
    }
    to_delete = []
    for ticker in failed_buy_tickers:
        if ticker in positions and ticker in _buy_in_progress:
            to_delete.append(ticker)
            _buy_in_progress.discard(ticker)
    for t in to_delete:
        logger.info("[매수 실패 정리] %s 포지션 삭제", t)
        del positions[t]

    # 매도 실패 → selling 플래그 해제 (sell_order_id 매칭)
    _changed = bool(to_delete)
    failed_sell_orders = [
        o for o in orders
        if o.get("status") == "failed" and o.get("side") == "sell"
    ]
    for ticker, pos in positions.items():
        if not pos.get("selling"):
            continue
        pos_sell_oid = pos.get("sell_order_id", "")
        if pos_sell_oid:
            matched = any(
                o.get("id") == pos_sell_oid
                for o in failed_sell_orders if o.get("ticker") == ticker
            )
            if not matched:
                continue
        else:
            if not any(o.get("ticker") == ticker for o in failed_sell_orders):
                continue
        pos["selling"] = False
        pos.pop("sell_order_id", None)
        _sell_fail_count[ticker] = _sell_fail_count.get(ticker, 0) + 1
        _changed = True
        logger.warning(
            "[포지션 정리] %s 매도 실패 → selling 플래그 해제, 손절 재시도 가능",
            pos.get("name", ticker),
        )

    if _changed:
        save_auto_positions(positions)

    return positions


# ---------------------------------------------------------------------------
# 포지션 관리 (손절/트레일링)
# ---------------------------------------------------------------------------

def check_auto_positions() -> None:
    """1분마다: 보유 포지션 손절/트레일링/과매수 감시.

    Stale data 2단계 정책:
      - age > 120초(STALE_SOFT): 트레일링/과매수 스킵, 손절만 허용 (정상 업데이트 직전의 일시적 지연일 수 있음)
      - age > 300초(STALE_HARD): 자동 손절/트레일링 스킵, 텔레그램 1회 경보(10분 쿨다운).
        stale 시세 기반 자동 판정은 슬리피지 위험. manual 비상 -15% 경보는 계속 수행
        (데이터가 오래돼도 포지션 위험을 알리는 가치가 더 큼).
    """
    STALE_SOFT_SEC = 120
    STALE_HARD_SEC = 300

    stale_data = False
    hard_stale = False
    try:
        with open(KIWOOM_DATA_PATH, "r", encoding="utf-8") as f:
            kiwoom = json.load(f)
        updated_at = datetime.strptime(kiwoom["updated_at"], "%Y-%m-%dT%H:%M:%S")
        age = (datetime.now() - updated_at).total_seconds()
        if age > STALE_HARD_SEC:
            logger.warning(
                "kiwoom_data.json이 %.0f초 전 데이터 — HARD STALE, 자동 청산 스킵 (수집기 복구 대기)",
                age,
            )
            hard_stale = True
            stale_data = True
            # 텔레그램 1회 경보 (10분 쿨다운)
            _notify_hard_stale(age)
        elif age > STALE_SOFT_SEC:
            logger.warning("kiwoom_data.json이 %.0f초 전 데이터 — 손절만 체크 (트레일링 스킵)", age)
            stale_data = True
        all_stocks = kiwoom.get("stocks", {})
    except Exception:
        return

    from trading.auto_trader import execute_sell
    from alerts.trade_executor import OPERATION_MODE

    positions = _cleanup_failed_positions(load_auto_positions())
    if not positions:
        return

    # 레짐별 파라미터 오버라이드
    try:
        from strategies.regime_engine import get_regime_engine
        _regime_params = get_regime_engine().params
        _regime_sl = _regime_params.stoploss_pct
        _regime_ta = _regime_params.trailing_activate_pct
        _regime_ts = _regime_params.trailing_stop_pct
    except Exception:
        _regime_sl = STOPLOSS_PCT
        _regime_ta = TRAILING_ACTIVATE_PCT
        _regime_ts = TRAILING_STOP_PCT

    # 방어 모드 해제 시 defense_cut 플래그 정리
    try:
        from strategies.regime_engine import get_regime_engine, RegimeState
        engine = get_regime_engine()
        if engine.state not in (RegimeState.DEFENSE, RegimeState.CASH):
            for pos in positions.values():
                pos.pop("defense_cut", None)
    except Exception:
        pass

    notifier = TelegramNotifier()
    changed = False
    sl_pct = _regime_sl
    ta_pct = _regime_ta
    ts_pct = _regime_ts

    # ── P1-8: 비상 모니터링 — 미실현 손실이 일일한도의 2배 초과 시 경고 ──
    from alerts.market_guard import MAX_DAILY_LOSS
    total_unrealized_loss = 0
    for _t, _p in positions.items():
        if _p.get("manual"):
            continue
        _bp = _p.get("buy_price", 0)
        _cp = int(all_stocks.get(_t, {}).get("current_price", 0))
        if _bp > 0 and _cp > 0:
            _qty = int(_p.get("qty", 0))
            _unrealized = (_cp - _bp) * _qty
            if _unrealized < 0:
                total_unrealized_loss += abs(_unrealized)

    if MAX_DAILY_LOSS > 0 and total_unrealized_loss > MAX_DAILY_LOSS * 2:
        logger.warning(
            "[비상 경고] 미실현 손실 %s원 > 일일한도 2배 (%s원) — 전량 매도 검토 필요",
            f"{total_unrealized_loss:,}", f"{MAX_DAILY_LOSS * 2:,}",
        )
        try:
            notifier.send_to_users(
                [get_admin_id()],
                f"🚨 [비상 경고] 미실현 손실 초과!\n"
                f"미실현 손실: {total_unrealized_loss:,}원\n"
                f"일일한도 2배: {MAX_DAILY_LOSS * 2:,}원\n"
                f"⚠️ 전량 매도를 검토하세요!"
                + CMD_FOOTER,
            )
        except Exception:
            pass

    for ticker, pos in list(positions.items()):
        # 분할 매수 2차 진입: 현재가가 1차 매수가 이상이면 추가 매수
        # ⚠️ hard_stale 상태에서는 신규 진입(=2차 매수)을 실행하지 않음. stale 시세 기반
        # 매수는 슬리피지 폭발 위험. manual 경보 이후, 자동 청산 루프에서 같이 스킵.
        split_remaining = pos.get("split_remaining", 0)
        if split_remaining > 0 and not pos.get("manual") and not hard_stale:
            name = pos.get("name", ticker)
            current_price = int(all_stocks.get(ticker, {}).get("current_price", 0))
            buy_price = pos.get("buy_price", 0)
            split_price = pos.get("split_price", buy_price)
            if current_price > 0 and current_price >= split_price:
                # 2차 매수 실행
                from alerts.trade_executor import OPERATION_MODE
                if OPERATION_MODE == "MOCK":
                    # 평균 단가 계산: (1차 qty*1차가 + 2차 qty*현재가) / 총 qty
                    # 주의: qty 갱신 전에 계산해야 함 (이전 코드는 순서 의존으로 난해)
                    first_qty = int(pos.get("qty", 0))
                    first_price = int(pos["buy_price"])
                    new_qty = first_qty + split_remaining
                    new_avg_price = int(
                        (first_price * first_qty + current_price * split_remaining) / new_qty
                    )
                    pos["qty"] = new_qty
                    pos["buy_price"] = new_avg_price
                    pos.pop("split_remaining", None)
                    pos.pop("split_price", None)
                    logger.info("[분할매수 2차] %s +%d주 @%s → 총 %d주", name, split_remaining, f"{current_price:,}", pos["qty"])
                    try:
                        notifier.send_to_users(
                            [get_admin_id()],
                            f"🛒 [가상 매수 2차] {name} ({ticker})\n"
                            f"💰 추가: {split_remaining}주 / 가격: {current_price:,}원 (40%)\n"
                            f"📊 평균단가: {pos['buy_price']:,}원 / 총 {pos['qty']}주\n"
                            f"⚠️ 모의투자"
                            + CMD_FOOTER,
                        )
                    except Exception:
                        pass
                    save_auto_positions(positions)
            elif current_price > 0:
                # 현재가가 1차 매수가 아래 → 2차 매수 취소
                pos.pop("split_remaining", None)
                pos.pop("split_price", None)
                logger.info("[분할매수 취소] %s 현재가 %s < 1차매수가 %s → 2차 취소", name, f"{current_price:,}", f"{split_price:,}")
                save_auto_positions(positions)
            continue  # 분할매수 처리 후 나머지 손절/트레일링은 다음 사이클에서

        if pos.get("manual", False):
            # ── P1-9: manual 포지션도 비상 손절만 적용 (-15%) ──
            # 경보 쿨다운 30분 — 매분 호출이라 쿨다운 없으면 하락장 내내 스팸.
            buy_price = pos.get("buy_price", 0)
            name = pos.get("name", ticker)
            current_price = int(all_stocks.get(ticker, {}).get("current_price", 0))
            if buy_price > 0 and current_price > 0:
                pct = (current_price - buy_price) / buy_price * 100
                if pct <= -15:
                    now = datetime.now()
                    last = _last_manual_emergency_alert.get(ticker)
                    if last is None or (now - last).total_seconds() >= 1800:  # 30분
                        _last_manual_emergency_alert[ticker] = now
                        logger.warning(
                            "[비상손절] %s manual 포지션 -15%% 도달 (현재가 %s, 매수가 %s)",
                            name, current_price, buy_price,
                        )
                        try:
                            notifier.send_to_users(
                                [get_admin_id()],
                                f"🚨 [비상 경고] {name} ({ticker})\n"
                                f"💰 매수가: {buy_price:,}원 → 현재가: {current_price:,}원\n"
                                f"📉 손실: {pct:.1f}%\n"
                                f"⚠️ manual 포지션이라 자동매도 안 됨. 직접 확인 필요!"
                                + CMD_FOOTER,
                            )
                        except Exception:
                            pass
            continue
        if pos.get("selling"):
            continue

        # hard_stale: 자동 포지션 손절/트레일링 판정 전체 스킵
        # (manual 비상경보는 위에서 이미 처리됨)
        if hard_stale:
            continue

        buy_price = pos.get("buy_price", 0)
        qty = int(pos.get("qty", 0))
        if buy_price <= 0 or qty <= 0:
            continue

        name = pos.get("name", ticker)
        # NOTE: 과거 "위기MR" 포지션은 crisis_manager._check_crisis_meanrev에서 전용 관리 했으나
        # 해당 함수는 현 아키텍처에서 스케줄 미등록 dead code. AutoStrategy 경로로 매수된
        # 평균회귀 포지션은 이제 일반 손절/트레일링으로 관리됨 (EOD 청산만 rule_name으로 스킵).
        # 위기MR 전용 트레일링(-1.5%)/시간청산(48h)은 향후 별도 설계로 분리 필요.

        stock_info = all_stocks.get(ticker, {})
        current_price = int(stock_info.get("current_price", 0))
        if current_price <= 0:
            continue

        # high_price 갱신
        high_price = pos.get("high_price", buy_price)
        if current_price > high_price:
            high_price = current_price
            positions[ticker]["high_price"] = high_price
            changed = True

        pct = (current_price - buy_price) / buy_price * 100
        drop_from_high = (high_price - current_price) / high_price * 100

        # trailing_activated 영구 플래그
        trailing_activated = pos.get("trailing_activated", False)
        if not trailing_activated and pct >= ta_pct:
            trailing_activated = True
            positions[ticker]["trailing_activated"] = True
            changed = True
            logger.info("[트레일링] %s 활성화 (수익 %.1f%% ≥ %.1f%%)", name, pct, ta_pct)

            # 분할 익절: 보유 수량의 50% 매도
            half_qty = qty // 2
            if half_qty > 0:
                if OPERATION_MODE == "MOCK":
                    # MOCK: 가상 분할 익절
                    pnl_half = (current_price - buy_price) * half_qty
                    positions[ticker]["qty"] = qty - half_qty
                    changed = True
                    logger.info("[가상 분할익절] %s %d주 @%s (수익 %.1f%%, 손익 %+d)", name, half_qty, f"{current_price:,}", pct, pnl_half)
                    notifier.send_to_users(
                        [get_admin_id()],
                        f"💸 [가상 분할 익절] {name}\n"
                        f"수량: {half_qty}주 / 매도가: {current_price:,}원\n"
                        f"수익: {pct:+.1f}% / 손익: {pnl_half:+,}원\n"
                        f"잔여 {qty - half_qty}주 트레일링 관리\n"
                        f"⚠️ 모의투자"
                        + CMD_FOOTER,
                    )
                    try:
                        from trading.trade_journal import record_trade
                        record_trade(ticker=ticker, name=name, side="sell", quantity=half_qty,
                                     price=current_price, reason="분할익절", mock=True,
                                     buy_price=buy_price, pnl=pnl_half)
                    except Exception:
                        pass
                else:
                    sell_res = execute_sell(ticker, name, half_qty,
                                            current_price, rule_name="자동청산_분할익절")
                    if sell_res.get("status") == "pending":
                        positions[ticker]["partial_sell_qty"] = half_qty
                        positions[ticker]["partial_sell_order_id"] = sell_res.get("order_id", "")
                        logger.info("[분할 익절] %s %d주 매도 접수 (수익 %.1f%%)", name, half_qty, pct)
                        notifier.send_to_users(
                            [get_admin_id()],
                            f"📊 [분할 익절] {name}\n"
                            f"수량: {half_qty}주 / 수익: {pct:+.1f}%\n"
                            f"잔여 {qty - half_qty}주 트레일링 관리"
                            + CMD_FOOTER,
                        )

        # RSI + ATR 조회
        rsi = 50.0
        atr = 0.0
        try:
            candles = stock_info.get("candles_1m", [])
            if candles:
                df = candles_to_df(candles)
                df_ind = calc_indicators(df)
                if df_ind is not None:
                    if "rsi" in df_ind.columns:
                        rsi = float(df_ind.iloc[-1]["rsi"])
                    if "atr" in df_ind.columns:
                        _atr_val = df_ind.iloc[-1]["atr"]
                        if not pd.isna(_atr_val):
                            atr = float(_atr_val)
        except Exception:
            pass

        # ATR 기반 동적 손절 계산
        atr_sl_mult = 1.5
        if atr > 0 and buy_price > 0:
            atr_sl_pct = (atr * atr_sl_mult) / buy_price * 100
            dynamic_sl = min(atr_sl_pct, sl_pct)  # 상한: 고정 손절%
            dynamic_sl = max(dynamic_sl, 1.0)      # 하한: 1%
        else:
            dynamic_sl = sl_pct

        # ATR 기반 동적 트레일링
        atr_ts_mult = 1.0
        if atr > 0 and high_price > 0:
            atr_ts_pct = (atr * atr_ts_mult) / high_price * 100
            dynamic_ts = min(atr_ts_pct, ts_pct * 2)
            dynamic_ts = max(dynamic_ts, 0.5)
        else:
            dynamic_ts = ts_pct

        reason = ""

        # 1) 손절 (ATR 기반 동적)
        if pct <= -dynamic_sl:
            reason = f"손절 ({pct:+.1f}% ≤ -{dynamic_sl:.1f}%, ATR={atr:.0f})"

        # 데이터 오래된 경우 트레일링/과매수 판단 불가
        elif stale_data:
            continue

        # 2) 과매수 트레일링: RSI≥75, 수익 1%+, 고점 대비 dynamic_ts*0.5 하락
        elif rsi >= 75 and pct > 1.0 and drop_from_high >= dynamic_ts * 0.5:
            reason = f"과매수 트레일링 (RSI {rsi:.0f}, 고점 대비 -{drop_from_high:.1f}%)"

        # 3) 트레일링 스탑 (ATR 기반 동적)
        elif trailing_activated and drop_from_high >= dynamic_ts:
            reason = f"트레일링 스탑 (최고 {high_price:,}원 → {current_price:,}원, -{drop_from_high:.1f}%, ATR={atr:.0f})"

        else:
            continue

        # 매도 실행 — 분할 익절 수량 차감
        partial_sell_qty = pos.get("partial_sell_qty", 0)
        partial_oid = pos.get("partial_sell_order_id", "")
        if partial_sell_qty > 0 and partial_oid:
            # 분할 익절 주문이 아직 pending이면 추가 매도 보류
            # (체결 전에 전량 매도하면 포지션 추적 불일치)
            logger.info("[자동청산] %s 분할 익절 pending → 추가 매도 보류", name)
            continue
        if partial_sell_qty > 0:
            adjusted_qty = qty - partial_sell_qty
            if adjusted_qty <= 0:
                logger.info("[자동청산] %s 분할 익절이 전량 커버 → 매도 스킵", name)
                continue
            sell_qty = adjusted_qty
        else:
            sell_qty = qty
        pnl = (current_price - buy_price) * sell_qty

        if OPERATION_MODE == "MOCK":
            # MOCK: 가상 청산
            pnl_str = f"+{pnl:,}" if pnl >= 0 else f"{pnl:,}"
            emoji = "📈" if pnl >= 0 else "📉"
            logger.info("[가상 청산] %s %s %d주 @%s (손익 %s)", name, reason, sell_qty, f"{current_price:,}", pnl_str)
            notifier.send_to_users(
                [get_admin_id()],
                f"💸 [가상 매도 체결] {name}\n"
                f"수량: {sell_qty}주 / 매도가: {current_price:,}원\n"
                f"매수가: {buy_price:,}원\n"
                f"{emoji} 손익: {pnl_str}원\n"
                f"사유: {reason}\n"
                f"⚠️ 모의투자"
                + CMD_FOOTER,
            )
            if pnl < 0:
                from alerts.market_guard import record_loss_and_stoploss
                record_loss_and_stoploss(abs(pnl))
            else:
                from alerts.market_guard import reset_consec_stoploss
                reset_consec_stoploss()
            try:
                from trading.trade_journal import record_trade
                record_trade(ticker=ticker, name=name, side="sell", quantity=sell_qty,
                             price=current_price, reason=reason, mock=True,
                             buy_price=buy_price, buy_time=pos.get("buy_time", ""), pnl=pnl)
            except Exception:
                pass
            del positions[ticker]
            changed = True
        else:
            sell_res = execute_sell(ticker, name, sell_qty, current_price,
                                    rule_name=f"자동청산_{reason[:10]}")
            if sell_res.get("status") == "pending":
                positions[ticker]["selling"] = True
                positions[ticker]["sell_order_id"] = sell_res.get("order_id", "")
                changed = True
                logger.info(
                    "[자동청산] %s %s → 매도 접수 (pnl=%+d)",
                    name, reason, pnl,
                )
                pnl_str = f"+{pnl:,}" if pnl >= 0 else f"{pnl:,}"
                notifier.send_to_users(
                    [get_admin_id()],
                    f"🔔 [자동청산] {name}\n"
                    f"사유: {reason}\n"
                    f"예상 손익: {pnl_str}원"
                    + CMD_FOOTER,
                )

    if changed:
        save_auto_positions(positions)
