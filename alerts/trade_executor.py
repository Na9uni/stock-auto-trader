"""자동매매 실행 — 매수/매도 로직."""

from __future__ import annotations

import json
import logging
from datetime import datetime

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
# 설정 (analysis_scheduler에서 주입)
# ---------------------------------------------------------------------------

AUTO_TRADE_ENABLED: bool = False
OPERATION_MODE: str = "PAPER"
MOCK_MODE: bool = False
AUTO_TRADE_AMOUNT: int = 0
MAX_ORDER_AMOUNT: int = 0
MAX_SLOTS: int = 0
_buy_start_minute: int = 10
_buy_end_hour: int = 14


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

def _calc_trade_amount() -> int:
    """예수금 기반 슬롯별 매수 금액 계산."""
    try:
        with open(KIWOOM_DATA_PATH, "r", encoding="utf-8") as f:
            kd = json.load(f)
        balance = int(kd.get("account", {}).get("balance", 0))
        if balance <= 0 and not MOCK_MODE:
            logger.info("[시드머니] 예수금 0 → 매수 불가")
            return 0
        if balance <= 0 and MOCK_MODE:
            # MOCK 모드에서 예수금 0이면 AUTO_TRADE_AMOUNT 사용
            return AUTO_TRADE_AMOUNT
        # manual 포지션은 슬롯에서 제외
        all_pos = load_auto_positions()
        auto_count = sum(1 for p in all_pos.values() if not p.get("manual", False))
        holding_count = auto_count + len(_buy_in_progress)
        free_slots = MAX_SLOTS - holding_count
        if free_slots <= 0:
            logger.info("[시드머니] 슬롯 꽉참 (%d/%d)", holding_count, MAX_SLOTS)
            return 0
        amount = balance // free_slots
        if amount > MAX_ORDER_AMOUNT:
            amount = MAX_ORDER_AMOUNT
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
        return
    if OPERATION_MODE not in ("LIVE", "MOCK"):
        return
    if not is_whitelisted(ticker):
        return

    # ── 시간 제한 (TradingConfig 기반) ──
    now = datetime.now()
    buy_start = _buy_start_minute
    buy_end = _buy_end_hour
    # 장 시작 N분 이내 매수 차단 (매도는 허용)
    if now.hour == 9 and now.minute < buy_start and signal.signal_type == SignalType.BUY:
        logger.debug("[시간 제한] 장 시작 %d분 이내 — 매수 보류", buy_start)
        return
    # buy_end_hour 이후 신규 매수 차단 (매도는 허용)
    if now.hour >= buy_end and signal.signal_type == SignalType.BUY:
        logger.debug("[시간 제한] %d시 이후 — 신규 매수 차단", buy_end)
        return

    # ── 서킷브레이커/급락 대응 ──
    if signal.signal_type == SignalType.BUY and _is_market_crash():
        return

    # ── 매수 ──
    if signal.signal_type == SignalType.BUY and signal.strength == SignalStrength.STRONG:
        # AI 판단: "매도"이면 차단, 실패/빈값은 경고 후 진행
        if ai_decision == "매도":
            logger.info("[자동매매] %s AI 판단=매도 → 매수 차단", name)
            return
        if ai_decision and ai_decision != "매수":
            logger.info("[자동매매] %s AI 판단=%s → 경고 후 진행", name, ai_decision)
        if not ai_decision:
            logger.debug("[자동매매] %s AI 미응답 → AI 없이 진행", name)

        # 방어 체크
        if is_monthly_loss_exceeded():
            logger.warning("[자동매매] 월간 손실 한도 초과 → 매수 차단")
            return
        if is_consec_stoploss_exceeded():
            logger.warning("[자동매매] 연속 손절 한도 초과 → 매수 차단")
            return
        if is_daily_loss_exceeded():
            logger.warning("[자동매매] 일일 손실 한도 초과 → 매수 차단")
            return

        # 중복 체크 (manual 포지션은 자동매매 대상 아님)
        positions = load_auto_positions()
        auto_positions = {k: v for k, v in positions.items() if not v.get("manual", False)}
        if ticker in auto_positions or ticker in _buy_in_progress:
            if ticker in auto_positions and auto_positions[ticker].get("selling"):
                return
            if ticker in auto_positions:
                return
            if ticker in _buy_in_progress:
                return

        # 필터 체크
        filtered, reason = is_ticker_filtered(ticker)
        if filtered:
            logger.info("[자동매매] %s 필터 제외: %s", name, reason)
            return

        # 금액 계산
        amount = _calc_trade_amount()
        if amount <= 0:
            return

        price = int(stock.get("current_price", 0))
        if price <= 0:
            return
        quantity = amount // price
        if quantity <= 0:
            return

        _buy_in_progress.add(ticker)

        if OPERATION_MODE == "MOCK":
            # MOCK 모드: 실제 주문 안 보내고 가상 체결 알림
            logger.info(
                "[가상매매] %s 매수 체결 %d주 @%s (금액 %s)",
                name, quantity, f"{price:,}", f"{amount:,}",
            )
            notifier.send_to_users(
                [get_admin_id()],
                f"🛒 [가상 매수 체결] {name} ({ticker})\n"
                f"💰 수량: {quantity}주 / 가격: {price:,}원\n"
                f"💵 투자금액: {amount:,}원\n"
                f"📊 사유: {', '.join(signal.reasons[:3])}\n"
                f"⚠️ 모의투자 — 실제 돈은 사용되지 않았습니다"
                + CMD_FOOTER,
            )
            # 가상 포지션 기록
            positions = load_auto_positions()
            positions[ticker] = {
                "name": name,
                "qty": quantity,
                "buy_price": price,
                "buy_amount": amount,
                "buy_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "mock": True,
            }
            save_auto_positions(positions)
            _buy_in_progress.discard(ticker)
        else:
            buy_result = execute_buy(ticker, name, quantity, price,
                                     rule_name=f"자동매매_{signal.strength.name}")
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
            else:
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
