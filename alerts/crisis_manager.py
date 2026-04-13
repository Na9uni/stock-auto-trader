"""위기장 평균회귀 전략 — RSI(2) 기반 급락 매수, 트레일링 청산."""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from alerts.file_io import (
    load_auto_positions,
    save_auto_positions,
    candles_to_df,
)
from alerts.market_guard import (
    is_daily_loss_exceeded,
    is_monthly_loss_exceeded,
    is_consec_stoploss_exceeded,
)
from alerts.notifications import get_admin_id, CMD_FOOTER
from alerts.telegram_notifier import TelegramNotifier
import alerts.trade_executor as _trade_exec

logger = logging.getLogger("stock_analysis")

# ---------------------------------------------------------------------------
# 위기MR 상태
# ---------------------------------------------------------------------------

# 069500 (KODEX200) 제거: 화이트리스트에서 제외됨
_CRISIS_MR_TARGETS = ["229200"]  # KODEX 코스닥150
_crisis_mr_position: dict | None = None


# ---------------------------------------------------------------------------
# RSI(2) 계산
# ---------------------------------------------------------------------------

def _rsi2_daily(candles_1d: list[dict]) -> float | None:
    """일봉 RSI(2) 계산. Connors 원논문과 동일한 타임프레임."""
    if not candles_1d or len(candles_1d) < 5:
        return None
    df = candles_to_df(candles_1d)
    if df is None or df.empty:
        return None
    close = pd.to_numeric(df["close"], errors="coerce")
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    ag = gain.ewm(alpha=0.5, min_periods=2, adjust=False).mean()
    al = loss.ewm(alpha=0.5, min_periods=2, adjust=False).mean()
    rs = ag / al
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return float(val) if not pd.isna(val) else None


# ---------------------------------------------------------------------------
# 위기MR 포지션 복구
# ---------------------------------------------------------------------------

def _restore_crisis_mr_position() -> None:
    """재시작 시 auto_positions에서 위기MR 포지션 복구.

    rule_name에 '위기MR'이 포함된 포지션만 복구한다.
    VB/combo 전략으로 산 같은 종목은 건드리지 않는다.
    """
    global _crisis_mr_position
    if _crisis_mr_position is not None:
        return
    positions = load_auto_positions()
    for ticker in _CRISIS_MR_TARGETS:
        if ticker not in positions:
            continue
        pos = positions[ticker]
        if pos.get("manual", False):
            continue
        # rule_name으로 위기MR 포지션 구분
        rule = pos.get("rule_name", "")
        if "위기MR" not in rule:
            continue  # VB/combo로 산 포지션 → 건드리지 않음
        # 매도 진행 중이면 복구 안 함 (중복 매도 방지)
        if pos.get("selling", False):
            continue
        bought_at = pos.get("bought_at", "")
        try:
            entry_time = datetime.strptime(bought_at, "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            entry_time = datetime.now()
        high_price = pos.get("high_price", pos.get("buy_price", 0))
        bp = pos.get("buy_price", 0)
        # 재시작 시 trailing 상태 복원: 고점이 매수가 대비 +2% 이상이면 활성
        trail_active = (high_price - bp) / bp * 100 >= 2.0 if bp > 0 else False
        _crisis_mr_position = {
            "ticker": ticker,
            "qty": int(pos.get("qty", 0)),
            "buy_price": bp,
            "entry_time": entry_time,
            "high_price": high_price,
            "trailing_activated": trail_active,
            "pending": False,
        }
        logger.info("[위기MR] 재시작 복구: %s %d주 @%s",
                    ticker, _crisis_mr_position["qty"],
                    f"{_crisis_mr_position['buy_price']:,}")
        break


# ---------------------------------------------------------------------------
# 위기장 평균회귀 메인
# ---------------------------------------------------------------------------

def _check_crisis_meanrev(data: dict) -> None:
    """위기장 평균회귀.

    진입: 일봉 RSI(2) < 10 + 당일 -2% + 반등 확인 + 14:30 이후(종가 근접)
    청산: 트레일링 스탑 (고정 익절 없음) / 손절 -2% / 시간청산 48h
    """
    global _crisis_mr_position
    from trading.auto_trader import execute_buy, execute_sell

    # 재시작 복구 (매매 비활성이어도 포지션 복구는 필요)
    _restore_crisis_mr_position()

    # 보유 중이면 청산 로직은 항상 실행 (매매 비활성이어도 열린 포지션은 관리)
    # 신규 매수만 차단
    buy_allowed = _trade_exec.AUTO_TRADE_ENABLED and _trade_exec.OPERATION_MODE in ("LIVE", "MOCK")

    notifier = TelegramNotifier()
    now = datetime.now()

    for ticker in _CRISIS_MR_TARGETS:
        stock = data.get("stocks", {}).get(ticker, {})
        current_price = int(stock.get("current_price", 0))
        if current_price <= 0:
            continue
        name = stock.get("name", ticker)

        # 일봉 RSI(2) — 백테스트와 동일한 타임프레임
        candles_1d = stock.get("candles_1d", [])
        rsi2 = _rsi2_daily(candles_1d)
        if rsi2 is None:
            continue

        # 일봉 RSI(2) 전일값 (반등 확인용)
        prev_rsi2 = None
        if candles_1d and len(candles_1d) >= 6:
            prev_val = _rsi2_daily(candles_1d[:-1])
            if prev_val is not None:
                prev_rsi2 = prev_val

        try:
            change_rate = float(stock.get("change_rate") or 0.0)
        except (ValueError, TypeError):
            change_rate = 0.0

        # ── 보유 중: 청산 판단 ──
        if _crisis_mr_position is not None and _crisis_mr_position["ticker"] == ticker:
            if _crisis_mr_position.get("pending"):
                positions = load_auto_positions()
                if ticker in positions:
                    # 체결 완료
                    _crisis_mr_position["pending"] = False
                    _crisis_mr_position["buy_price"] = positions[ticker].get("buy_price", _crisis_mr_position["buy_price"])
                    _crisis_mr_position["qty"] = int(positions[ticker].get("qty", _crisis_mr_position["qty"]))
                    _trade_exec._buy_in_progress.discard(ticker)
                elif ticker not in _trade_exec._buy_in_progress:
                    # 매수 실패 (check_order_status에서 _trade_exec._buy_in_progress 제거됨)
                    logger.warning("[위기MR] %s 매수 실패 → pending 해제", ticker)
                    _crisis_mr_position = None
                    continue
                else:
                    continue  # 아직 체결 대기 중

            buy_price = _crisis_mr_position["buy_price"]
            qty = _crisis_mr_position["qty"]
            pct = (current_price - buy_price) / buy_price * 100
            entry_time = _crisis_mr_position["entry_time"]
            hours_held = (now - entry_time).total_seconds() / 3600

            # 고점 추적 (트레일링용) — auto_positions에도 동기화 (재시작 복구용)
            high_price = _crisis_mr_position.get("high_price", buy_price)
            if current_price > high_price:
                high_price = current_price
                _crisis_mr_position["high_price"] = high_price
                positions_sync = load_auto_positions()
                if ticker in positions_sync:
                    positions_sync[ticker]["high_price"] = high_price
                    save_auto_positions(positions_sync)
            drop_from_high = (high_price - current_price) / high_price * 100 if high_price > 0 else 0

            sell_reason = ""

            # 1) 손절 -2% (고정. 위기장 ATR 폭발 감안)
            if pct <= -2.0:
                sell_reason = f"손절({pct:+.1f}%)"

            # 2) 트레일링: +2% 도달 후 활성, 고점 대비 -1.5% 하락 시 청산
            #    → 반등 +8%까지 타면 +6.5%에서 청산 (기존: +2%에서 잘림)
            elif pct >= 2.0 or _crisis_mr_position.get("trailing_activated"):
                _crisis_mr_position["trailing_activated"] = True
                if drop_from_high >= 1.5:
                    sell_reason = f"트레일링({pct:+.1f}%, 고점-{drop_from_high:.1f}%)"

            # 3) RSI(2) >= 80 + 수익 중일 때만 (기존 65→80, 수익 필터 추가)
            elif rsi2 >= 80 and pct > 0:
                sell_reason = f"RSI2과매수({rsi2:.0f}, {pct:+.1f}%)"

            # 4) 시간청산 48h
            elif hours_held >= 48:
                sell_reason = f"시간청산({hours_held:.0f}h)"

            if sell_reason:
                # rule_name: "자동청산_" prefix → check_order_status에서 정상 처리
                result = execute_sell(ticker, name, qty, current_price,
                                      rule_name=f"자동청산_위기MR")
                if result.get("status") == "pending":
                    # auto_positions에 selling 플래그 세팅 (중복 매도 방지)
                    positions = load_auto_positions()
                    if ticker in positions:
                        positions[ticker]["selling"] = True
                        positions[ticker]["sell_order_id"] = result.get("order_id", "")
                        save_auto_positions(positions)

                    pnl = (current_price - buy_price) * qty
                    notifier.send_to_users(
                        [get_admin_id()],
                        f"[위기MR 매도] {name}\n"
                        f"사유: {sell_reason}\n"
                        f"예상 손익: {pnl:+,}원" + CMD_FOOTER,
                    )
                    logger.info("[위기MR] %s 매도: %s, 예상pnl=%+d", name, sell_reason, pnl)
                    # PnL/손절 카운터는 check_order_status()에서 체결 확인 후 기록
                    # 여기서 기록하면 이중 계상됨
                    _crisis_mr_position = None
            continue

        # ── 미보유: 매수 판단 ──
        if _crisis_mr_position is not None:
            continue

        if is_daily_loss_exceeded() or is_monthly_loss_exceeded() or is_consec_stoploss_exceeded():
            continue

        # 킬스위치: 계좌 잔고가 초기 자본의 90% 이하면 매매 중단
        try:
            from alerts.file_io import load_kiwoom_data as _load_kd
            _kd = _load_kd()
            if _kd:
                _balance = int(_kd.get("account", {}).get("total_eval", 0))
                if 0 < _balance < 100000:  # 10만원 미만이면 차단
                    logger.warning("[킬스위치] 계좌 %s원 < 10만원 — 매매 중단", f"{_balance:,}")
                    continue
        except Exception:
            pass

        # 매수 조건 (3가지 모두 충족):
        # 1) 일봉 RSI(2) < 15 + 반등 시작 (전일보다 상승 또는 바닥권)
        # 2) 당일 등락률 -2% 이상 하락
        # 3) 14:30 이후 (종가 근접 = 일봉 등락률 확정에 가까움)
        is_near_close = (now.hour == 14 and now.minute >= 30) or (now.hour == 15 and now.minute <= 20)
        rsi2_entry = rsi2 < 15
        bounce_confirmed = (prev_rsi2 is not None and prev_rsi2 < 10 and rsi2 > prev_rsi2)
        drop_confirmed = change_rate <= -2.0

        if is_near_close and rsi2_entry and bounce_confirmed and drop_confirmed and buy_allowed:
            # 이미 보유 중이면 중복 매수 방지
            existing = load_auto_positions()
            if ticker in existing or ticker in _trade_exec._buy_in_progress:
                continue
            amount = _trade_exec._calc_trade_amount()
            if amount <= 0:
                continue
            qty = amount // current_price
            if qty <= 0:
                continue

            result = execute_buy(ticker, name, qty, current_price,
                                  rule_name="자동매매_위기MR")
            if result.get("status") == "pending":
                _trade_exec._buy_in_progress.add(ticker)
                _crisis_mr_position = {
                    "ticker": ticker,
                    "qty": qty,
                    "buy_price": current_price,
                    "entry_time": now,
                    "high_price": current_price,
                    "trailing_activated": False,
                    "pending": True,
                }
                notifier.send_to_users(
                    [get_admin_id()],
                    f"[위기MR 매수] {name}\n"
                    f"일봉RSI(2)={rsi2:.0f}, 등락={change_rate:+.1f}%\n"
                    f"수량: {qty}주 @{current_price:,}원" + CMD_FOOTER,
                )
                logger.info(
                    "[위기MR] %s 매수: RSI2=%.0f(전일%.0f), 등락=%.1f%%, %d주 @%s",
                    name, rsi2, prev_rsi2 or 0, change_rate, qty, f"{current_price:,}",
                )
            break
