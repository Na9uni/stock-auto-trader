"""시장 방어 로직 — 손실 관리, 급락 감지, 장 운영 시간, 쿨다운."""

from __future__ import annotations

import json
import logging
from datetime import datetime, date

import holidays
import yfinance as yf

from alerts.file_io import (
    ORDER_QUEUE_PATH,
    load_monthly_loss,
    save_monthly_loss,
)
from alerts.signal_detector import SignalType, SignalStrength

logger = logging.getLogger("stock_analysis")

# ---------------------------------------------------------------------------
# 설정 상수 (analysis_scheduler에서 초기화 후 주입)
# ---------------------------------------------------------------------------

# 이 값들은 analysis_scheduler 모듈 로드 시 설정됨
MAX_MONTHLY_LOSS: int = 0
MAX_CONSEC_STOPLOSS: int = 0
MAX_DAILY_LOSS: int = 0

COOLDOWN: dict[SignalStrength, int] = {
    SignalStrength.STRONG: 15,
    SignalStrength.MEDIUM: 30,
    SignalStrength.WEAK: 9999,
}


def _configure(max_monthly_loss: int, max_consec_stoploss: int, max_daily_loss: int) -> None:
    """analysis_scheduler에서 호출하여 설정값 주입."""
    global MAX_MONTHLY_LOSS, MAX_CONSEC_STOPLOSS, MAX_DAILY_LOSS
    MAX_MONTHLY_LOSS = max_monthly_loss
    MAX_CONSEC_STOPLOSS = max_consec_stoploss
    MAX_DAILY_LOSS = max_daily_loss


# ---------------------------------------------------------------------------
# 손실 기록
# ---------------------------------------------------------------------------

def record_loss_and_stoploss(loss_amount: int) -> None:
    """손실 기록 + 연속손절 카운터 증가.

    Args:
        loss_amount: 손실 금액 (양수)
    """
    data = load_monthly_loss()
    data["loss"] = data.get("loss", 0) + abs(loss_amount)
    data["consec_stoploss"] = data.get("consec_stoploss", 0) + 1

    # 주간 손실도 기록
    week_key = datetime.now().strftime("%Y-W%W")
    weekly = data.get("weekly_loss", {})
    weekly[week_key] = weekly.get(week_key, 0) + abs(loss_amount)
    data["weekly_loss"] = weekly

    save_monthly_loss(data)
    logger.info(
        "손실 기록: %d원 (월누적: %d원, 연속손절: %d회)",
        loss_amount,
        data["loss"],
        data["consec_stoploss"],
    )


def reset_consec_stoploss() -> None:
    """연속손절 카운터 리셋 (익절 시)."""
    data = load_monthly_loss()
    if data.get("consec_stoploss", 0) > 0:
        data["consec_stoploss"] = 0
        save_monthly_loss(data)
        logger.info("연속손절 카운터 리셋 (익절)")


def is_monthly_loss_exceeded() -> bool:
    """월간 손실 한도 초과 여부."""
    data = load_monthly_loss()
    exceeded = data.get("loss", 0) >= MAX_MONTHLY_LOSS
    if exceeded:
        logger.warning(
            "월간 손실 한도 초과: %d / %d",
            data.get("loss", 0),
            MAX_MONTHLY_LOSS,
        )
    return exceeded


def is_consec_stoploss_exceeded() -> bool:
    """연속 손절 한도 초과 여부."""
    data = load_monthly_loss()
    exceeded = data.get("consec_stoploss", 0) >= MAX_CONSEC_STOPLOSS
    if exceeded:
        logger.warning(
            "연속손절 한도 초과: %d / %d",
            data.get("consec_stoploss", 0),
            MAX_CONSEC_STOPLOSS,
        )
    return exceeded


def is_daily_loss_exceeded() -> bool:
    """당일 손실 한도 초과 여부 (order_queue.json 기반).

    오늘 체결된 매도 주문 중 손실 합계를 계산.
    """
    if not ORDER_QUEUE_PATH.exists():
        return False
    try:
        with open(ORDER_QUEUE_PATH, "r", encoding="utf-8") as f:
            queue_data = json.load(f)
        orders = queue_data.get("orders", [])
    except (json.JSONDecodeError, OSError):
        return False

    today_str = date.today().isoformat()
    daily_loss = 0

    for order in orders:
        if not isinstance(order, dict):
            continue
        # 오늘 체결된 매도 주문만
        executed_at = order.get("executed_at", "")
        if not executed_at or not executed_at[:10] == today_str:
            continue
        if order.get("side") != "sell":
            continue
        if order.get("status") != "executed":
            continue
        # 손익 계산: (매도 체결가 - 매수가) * 수량
        sell_price = int(order.get("exec_price") or 0)
        buy_price = int(order.get("buy_price", 0))
        qty = int(order.get("quantity", 0))
        if sell_price > 0 and buy_price > 0 and qty > 0:
            pnl = (sell_price - buy_price) * qty
            if pnl < 0:
                daily_loss += abs(pnl)

    exceeded = daily_loss >= MAX_DAILY_LOSS
    if exceeded:
        logger.warning("당일 손실 한도 초과: %d / %d", daily_loss, MAX_DAILY_LOSS)
    return exceeded


# ---------------------------------------------------------------------------
# 서킷브레이커/급락 감지
# ---------------------------------------------------------------------------

def _is_market_crash() -> bool:
    """KOSPI/KOSDAQ 지수 -3% 이상 급락 시 매수 전면 중단."""
    try:
        indices = fetch_index_prices()
        for idx_name, data in indices.items():
            change = data.get("change_pct", 0)
            if change <= -3.0:
                logger.warning("[급락 감지] %s %.1f%% — 매수 전면 중단", idx_name, change)
                return True
    except Exception:
        pass
    return False


def fetch_index_prices() -> dict:
    """yfinance로 주요 지수 조회.

    Returns:
        {
            "KOSPI": {"price": float, "change_pct": float},
            "KOSDAQ": {"price": float, "change_pct": float},
            "S&P500": {"price": float, "change_pct": float},
            "NASDAQ": {"price": float, "change_pct": float},
        }
    """
    symbols = {
        "KOSPI": "^KS11",
        "KOSDAQ": "^KQ11",
        "S&P500": "^GSPC",
        "NASDAQ": "^IXIC",
    }
    result: dict[str, dict] = {}

    for label, symbol in symbols.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="2d")
            if hist.empty or len(hist) < 1:
                logger.debug("지수 데이터 없음: %s", label)
                continue

            current = float(hist["Close"].iloc[-1])
            if len(hist) >= 2:
                prev = float(hist["Close"].iloc[-2])
                change_pct = ((current - prev) / prev) * 100 if prev != 0 else 0.0
            else:
                change_pct = 0.0

            result[label] = {
                "price": round(current, 2),
                "change_pct": round(change_pct, 2),
            }
        except Exception as exc:
            logger.warning("지수 조회 실패 [%s]: %s", label, exc)

    return result


# ---------------------------------------------------------------------------
# 장 운영 시간
# ---------------------------------------------------------------------------

_KR_HOLIDAYS = holidays.KR()


def is_market_holiday(d: date | None = None) -> bool:
    """한국 휴장일 (주말 + 법정공휴일)."""
    target = d or date.today()
    if target.weekday() >= 5:
        return True
    return target in _KR_HOLIDAYS


def is_market_hours() -> bool:
    """장 시간 (평일 09:00~15:35).

    주말/공휴일이면 False.
    """
    now = datetime.now()
    if is_market_holiday(now.date()):
        return False
    market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=35, second=0, microsecond=0)
    return market_open <= now <= market_close


# ---------------------------------------------------------------------------
# 쿨다운
# ---------------------------------------------------------------------------

_last_alert: dict[str, datetime] = {}


def cooldown_ok(ticker: str, sig_type: SignalType, strength: SignalStrength) -> bool:
    """쿨다운 체크. 마지막 알림 이후 충분한 시간이 지났는지 확인."""
    key = f"{ticker}:{sig_type.value}"
    last = _last_alert.get(key)
    if last is None:
        return True
    minutes = COOLDOWN.get(strength, 9999)
    elapsed = (datetime.now() - last).total_seconds() / 60
    return elapsed >= minutes


def update_cooldown(ticker: str, sig_type: SignalType) -> None:
    """쿨다운 갱신 — 현재 시각으로 기록."""
    key = f"{ticker}:{sig_type.value}"
    _last_alert[key] = datetime.now()
