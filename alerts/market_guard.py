"""시장 방어 로직 — 손실 관리, 급락 감지, 장 운영 시간, 쿨다운."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, date

import holidays
import yfinance as yf

from alerts.file_io import (
    ORDER_QUEUE_PATH,
    load_monthly_loss,
    save_monthly_loss,
)
from strategies.base import SignalType, SignalStrength

logger = logging.getLogger("stock_analysis")

# ---------------------------------------------------------------------------
# 설정 상수 (analysis_scheduler에서 초기화 후 주입)
# ---------------------------------------------------------------------------

# 이 값들은 analysis_scheduler 모듈 로드 시 설정됨
MAX_MONTHLY_LOSS: int = 0
MAX_CONSEC_STOPLOSS: int = 0
MAX_DAILY_LOSS: int = 0

# 매매 스타일: .env의 TRADING_STYLE 환경변수로 결정 (기본: swing)
# - daytrading: 하루 여러 번 짧게 (쿨다운 짧고 재진입 허용)
# - swing: 하루 길게 보유 (쿨다운 길고 재매수 엄격)
_TRADING_STYLE = os.getenv("TRADING_STYLE", "swing").strip().lower()

if _TRADING_STYLE == "daytrading":
    # 아빠 PC — 데이트레이딩: 팔고 나면 바로 재진입 가능
    COOLDOWN: dict[SignalStrength, int] = {
        SignalStrength.STRONG: 1,    # 1분
        SignalStrength.MEDIUM: 5,    # 5분
        SignalStrength.WEAK: 9999,
    }
else:
    # 아들 PC / 기본값 — 스윙: 한 번 사면 길게
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
    """KOSPI/KOSDAQ 지수 -3% 이상 급락 시 매수 전면 중단.

    fail-safe 정책: 지수 조회 실패(예외) 또는 **빈 응답**인 경우에도
    급락으로 간주하여 매수 차단. 데이터 없이 매수 진행하는 것이 가장 위험.
    """
    try:
        indices = fetch_index_prices()
        if not indices:
            logger.warning("[급락감지] 지수 데이터 비어있음 — 안전을 위해 매수 차단")
            return True  # fail-safe: 데이터 없으면 급락 가정
        for idx_name, data in indices.items():
            change = data.get("change_pct", 0)
            if change <= -3.0:
                logger.warning("[급락 감지] %s %.1f%% — 매수 전면 중단", idx_name, change)
                return True
    except Exception as e:
        logger.error("[급락감지] 지수 조회 실패: %s — 안전을 위해 매수 차단", e)
        return True  # fail-safe: assume crash
    return False


def fetch_index_prices() -> dict:
    """지수 가격 조회. 키움 실시간 데이터 우선, yfinance 폴백.

    Returns:
        {
            "KOSPI": {"price": float, "change_pct": float},
            "KOSDAQ": {"price": float, "change_pct": float},
            "S&P500": {"price": float, "change_pct": float},
            "NASDAQ": {"price": float, "change_pct": float},
        }
    """
    result: dict[str, dict] = {}

    # 1차: kiwoom_data.json에서 실시간 데이터 (딜레이 0)
    try:
        from alerts.file_io import load_kiwoom_data
        data = load_kiwoom_data()
        if data:
            # KOSPI ETF(069500)로 KOSPI 지수 대체
            kospi_etf = data.get("stocks", {}).get("069500", {})
            if kospi_etf.get("current_price", 0) > 0:
                result["KOSPI"] = {
                    "price": float(kospi_etf["current_price"]),
                    "change_pct": float(kospi_etf.get("change_rate", 0)),
                }
            # KOSDAQ ETF(229200)로 KOSDAQ 지수 대체
            kosdaq_etf = data.get("stocks", {}).get("229200", {})
            if kosdaq_etf.get("current_price", 0) > 0:
                result["KOSDAQ"] = {
                    "price": float(kosdaq_etf["current_price"]),
                    "change_pct": float(kosdaq_etf.get("change_rate", 0)),
                }
    except Exception as e:
        logger.debug("[지수] 키움 데이터 실패, yfinance 폴백: %s", e)

    # 2차: 부족한 지수는 yfinance로 보충 (US 지수는 항상 yfinance)
    yf_symbols = {
        "KOSPI": "^KS11",
        "KOSDAQ": "^KQ11",
        "S&P500": "^GSPC",
        "NASDAQ": "^IXIC",
    }

    for label, symbol in yf_symbols.items():
        if label in result:
            continue  # 키움에서 이미 가져온 지수는 스킵
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
# 쿨다운 (파일 영속화 — 프로세스 재시작 후에도 유지)
# ---------------------------------------------------------------------------

# 프로세스 재시작 시 in-memory dict가 초기화되면 재진입 폭주 위험(특히 daytrading
# 쿨다운 1분). 파일에 ISO 시각으로 저장/복원.
_COOLDOWN_PATH = __import__("pathlib").Path(__file__).parent.parent / "data" / "cooldown_state.json"
_last_alert: dict[str, datetime] = {}


def _load_cooldown_state() -> None:
    """시작 시 1회 호출 — 파일에서 쿨다운 상태 복원."""
    global _last_alert
    try:
        if not _COOLDOWN_PATH.exists():
            return
        with open(_COOLDOWN_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        restored = {}
        for k, v in raw.items():
            try:
                restored[k] = datetime.fromisoformat(v)
            except (ValueError, TypeError):
                continue
        # 24시간 이상 지난 엔트리는 드롭 (디스크 누적 방지)
        now = datetime.now()
        _last_alert = {
            k: t for k, t in restored.items()
            if (now - t).total_seconds() < 86400
        }
        logger.info("[쿨다운] 복원: %d개 엔트리", len(_last_alert))
    except Exception as e:
        logger.warning("[쿨다운] 복원 실패: %s — 빈 상태로 시작", e)


def _save_cooldown_state() -> None:
    """쿨다운 갱신 후 파일에 저장 (atomic write)."""
    try:
        _COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _COOLDOWN_PATH.with_suffix(".tmp")
        data = {k: t.isoformat() for k, t in _last_alert.items()}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        import os as _os
        _os.replace(tmp, _COOLDOWN_PATH)
    except Exception as e:
        logger.warning("[쿨다운] 저장 실패: %s", e)


# 모듈 로드 시 자동 복원
_load_cooldown_state()


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
    """쿨다운 갱신 — 현재 시각으로 기록 + 파일 영속화."""
    key = f"{ticker}:{sig_type.value}"
    _last_alert[key] = datetime.now()
    _save_cooldown_state()


# ---------------------------------------------------------------------------
# 하루 동일 종목 매매 횟수 제한 (데이트레이딩 재진입 무한루프 방지)
# ---------------------------------------------------------------------------

# 왕복 거래 비용 (한국 주식):
#   - 매수/매도 수수료 ~0.015% 각 (=왕복 0.03%)
#   - 증권거래세 0.18% (매도 시)
#   - 왕복 비용 총 ~0.21%
# break-even 승률 계산 (목표 +1% / 손절 -1.5%):
#   p × (1.0 - 0.21) >= (1-p) × (1.5 + 0.21)  →  p >= 68.4%
# 68% 승률은 현실적으로 어려움 → 재진입 횟수를 보수적으로 제한하여 누적 손실 방지.
# 환경변수로 오버라이드 가능.
MAX_DAILY_ROUNDTRIPS = int(os.getenv("MAX_DAILY_ROUNDTRIPS", "3"))


def daily_buy_count_ok(ticker: str) -> bool:
    """오늘 해당 종목을 몇 번이나 샀는지 체크. MAX 이상이면 False.

    trade_journal에서 오늘 날짜 + BUY 엔트리 카운트. 데이트레이딩 재진입 무한 반복 방지용.
    """
    try:
        from pathlib import Path
        import csv
        today = date.today().isoformat()
        journal = Path(__file__).parent.parent / "data" / "trade_journal.csv"
        if not journal.exists():
            return True
        count = 0
        with open(journal, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if (row.get("date", "").startswith(today)
                        and row.get("ticker") == ticker
                        and row.get("side", "").lower() == "buy"):
                    count += 1
        if count >= MAX_DAILY_ROUNDTRIPS:
            logger.info("[재진입 차단] %s 오늘 이미 %d회 매수 (한도 %d)",
                        ticker, count, MAX_DAILY_ROUNDTRIPS)
            return False
        return True
    except Exception as e:
        logger.warning("[재진입 체크 실패] %s: %s — 통과 처리", ticker, e)
        return True
