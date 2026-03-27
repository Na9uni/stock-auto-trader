"""주식 분석 스케줄러 — 자동매매 + 알림 + 포지션 관리

Part 1: 환경변수, 상수, 화이트리스트, 유틸 함수
Part 2: 자동매매 로직, 포지션 관리, 스케줄러 메인 (하단)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import holidays
import pandas as pd
import schedule
import yfinance as yf
from dotenv import load_dotenv

from alerts.signal_detector import SignalType, SignalStrength, SignalResult
from alerts.telegram_notifier import TelegramNotifier
from analysis.indicators import TechnicalIndicators

# ---------------------------------------------------------------------------
# 환경변수 + 경로
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
logger = logging.getLogger("stock_analysis")

KIWOOM_DATA_PATH = ROOT / "data" / "kiwoom_data.json"
AUTO_POSITIONS_PATH = ROOT / "data" / "auto_positions.json"
ORDER_QUEUE_PATH = ROOT / "data" / "order_queue.json"
MONTHLY_LOSS_PATH = ROOT / "data" / "monthly_loss.json"
TRADE_FILTER_PATH = ROOT / "data" / "trade_filter.json"

# ---------------------------------------------------------------------------
# 운영 모드
# ---------------------------------------------------------------------------

OPERATION_MODE = os.getenv("OPERATION_MODE", "OBSERVE").upper()
MOCK_MODE = os.getenv("KIWOOM_MOCK_MODE", "True").lower() == "true"
AUTO_TRADE_ENABLED = os.getenv("AUTO_TRADE_ENABLED", "false").lower() == "true"

# ---------------------------------------------------------------------------
# 자동매매 설정
# ---------------------------------------------------------------------------

AUTO_TRADE_AMOUNT = int(os.getenv("AUTO_TRADE_AMOUNT", "500000"))
MAX_ORDER_AMOUNT = int(os.getenv("MAX_ORDER_AMOUNT", "1000000"))
MAX_SLOTS = int(os.getenv("MAX_SLOTS", "5"))

# ---------------------------------------------------------------------------
# 손절/익절 (%)
# ---------------------------------------------------------------------------

STOPLOSS_PCT = float(os.getenv("STOPLOSS_PCT", "2.5"))
TRAILING_ACTIVATE_PCT = float(os.getenv("TRAILING_ACTIVATE_PCT", "3.0"))
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "2.0"))

# ---------------------------------------------------------------------------
# 손실 방어
# ---------------------------------------------------------------------------

MAX_MONTHLY_LOSS = int(os.getenv("MAX_MONTHLY_LOSS", "1000000"))
MAX_CONSEC_STOPLOSS = int(os.getenv("MAX_CONSEC_STOPLOSS", "3"))
MAX_DAILY_LOSS = int(os.getenv("MAX_DAILY_LOSS", "300000"))

# ---------------------------------------------------------------------------
# 쿨다운 (분)
# ---------------------------------------------------------------------------

COOLDOWN: dict[SignalStrength, int] = {
    SignalStrength.STRONG: 15,
    SignalStrength.MEDIUM: 30,
    SignalStrength.WEAK: 9999,
}

# ---------------------------------------------------------------------------
# 기타 상수
# ---------------------------------------------------------------------------

INTEREST_SPIKE_THRESHOLD = 3.0
MAX_SELL_FAIL_BEFORE_REMOVE = 5

LARGECAP_DAILY_THRESHOLD: dict[str, str] = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "005380": "현대차",
}
LARGECAP_DAILY_MIN_SCORE = 4

# ---------------------------------------------------------------------------
# 자동매매 화이트리스트 (18종목)
# ---------------------------------------------------------------------------

AUTO_TRADE_WHITELIST: dict[str, str] = {
    # 기존 검증 (2026-03-13)
    "005930": "삼성전자",       # PF 1.25, +15.12%
    "069500": "KODEX 200",     # PF 2.12, +58.64% (ETF)
    "105560": "KB금융",         # PF 1.80, +42.35%
    "055550": "신한지주",       # PF 1.25, +12.94%
    # 전수 검증 추가 (2026-03-18)
    "006910": "보성파워텍",     # PF 5.54, 80%, MDD 6.10%
    "016610": "DB증권",         # PF 3.70, 77%, MDD 12.75%
    "133690": "TIGER 미국나스닥100",  # PF 2.95, 67% (ETF)
    "229200": "KODEX 코스닥150",     # PF 1.79, 58% (ETF)
    "019180": "티에이치엔",     # PF 1.76, 73%, MDD 11.61%
    "131890": "ACE 삼성그룹동일가중", # PF 1.77, 64% (ETF)
    "108450": "ACE 삼성그룹섹터가중", # PF 1.62, 69% (ETF)
    "395160": "KODEX AI반도체",      # PF 1.68, 56% (ETF)
    # 전수 검증 추가 (2026-03-23, PF>=2.0 & MDD<=15%)
    "000500": "가온전선",       # PF 2.85, 63%, MDD 5.2%
    "014790": "HL D&I",        # PF 3.15, 67%, MDD 5.2%
    "103590": "일진전기",       # PF 3.07, 67%, MDD 10.5%
    "009420": "한올바이오파마", # PF 2.44, 57%, MDD 10.5%
    "034020": "두산에너빌리티", # PF 4.46, 67%, MDD 10.5%
    # 수동 추가 (보유 종목)
    "078600": "대주전자재료",
}


def is_whitelisted(ticker: str) -> bool:
    """화이트리스트 포함 여부."""
    return ticker in AUTO_TRADE_WHITELIST


# ---------------------------------------------------------------------------
# 인메모리 상태 변수
# ---------------------------------------------------------------------------

_last_alert: dict[str, datetime] = {}
_notified_orders: set[str] = set()
_order_init_done: bool = False
_sell_fail_count: dict[str, int] = {}
_buy_in_progress: set[str] = set()

CMD_FOOTER = "\n\n💡 /도움말 — 명령어 목록"

# ---------------------------------------------------------------------------
# 텔레그램 헬퍼 (모듈 레벨 싱글턴)
# ---------------------------------------------------------------------------

_notifier = None


def _get_notifier():
    """TelegramNotifier 싱글턴 반환."""
    global _notifier
    if _notifier is None:
        from alerts.telegram_notifier import TelegramNotifier
        _notifier = TelegramNotifier()
    return _notifier


# ---------------------------------------------------------------------------
# 유틸: 파일 I/O
# ---------------------------------------------------------------------------

def load_kiwoom_data() -> dict | None:
    """kiwoom_data.json 로드.

    파일이 없거나 파싱 실패 시 None.
    마지막 갱신이 2분 초과이면 로그 경고.
    """
    if not KIWOOM_DATA_PATH.exists():
        logger.warning("kiwoom_data.json 파일 없음")
        return None
    try:
        with open(KIWOOM_DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("kiwoom_data.json 로드 실패: %s", exc)
        return None

    # 2분 이상 갱신 안 됐으면 경고
    updated_at = data.get("updated_at", "")
    if updated_at:
        try:
            ts = datetime.fromisoformat(updated_at)
            age = (datetime.now() - ts).total_seconds()
            if age > 120:
                logger.warning(
                    "kiwoom_data.json 갱신 지연: %.0f초 전 (%s)",
                    age,
                    updated_at,
                )
        except ValueError:
            pass

    return data


def load_auto_positions() -> dict:
    """auto_positions.json 로드.

    파일 손상 시 텔레그램 관리자 경고 후 빈 dict 반환.
    """
    if not AUTO_POSITIONS_PATH.exists():
        return {}
    try:
        with open(AUTO_POSITIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("auto_positions.json 손상: %s", exc)
        try:
            notifier = _get_notifier()
            admin = get_admin_id()
            if admin:
                notifier.send_message(
                    f"⚠️ auto_positions.json 손상됨!\n{exc}\n수동 복구 필요",
                    chat_id=admin,
                )
        except Exception:
            pass
        return {}


def save_auto_positions(positions: dict) -> None:
    """auto_positions.json 원자적 저장."""
    tmp = AUTO_POSITIONS_PATH.with_suffix(".tmp")
    try:
        AUTO_POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
        tmp.replace(AUTO_POSITIONS_PATH)
    except OSError as exc:
        logger.error("auto_positions.json 저장 실패: %s", exc)
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def load_trade_filter() -> dict:
    """trade_filter.json 로드 (자동매매 제외 종목).

    Returns:
        {종목코드: {"reason": str, "until": str|None}, ...}
    """
    if not TRADE_FILTER_PATH.exists():
        return {}
    try:
        with open(TRADE_FILTER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("trade_filter.json 로드 실패: %s", exc)
        return {}


def is_ticker_filtered(ticker: str) -> tuple[bool, str]:
    """종목이 자동매매 제외 대상인지 확인.

    Returns:
        (True, 사유) 또는 (False, "")
    """
    filters = load_trade_filter()
    if ticker not in filters:
        return False, ""

    entry = filters[ticker]
    reason = entry.get("reason", "수동 제외")

    # until 필드가 있으면 만료 체크
    until_str = entry.get("until")
    if until_str:
        try:
            until_date = datetime.fromisoformat(until_str)
            if datetime.now() > until_date:
                # 만료됨 — 필터에서 제거하고 저장
                del filters[ticker]
                _save_trade_filter(filters)
                logger.info("자동매매 필터 만료 제거: %s", ticker)
                return False, ""
        except ValueError:
            pass

    return True, reason


def _save_trade_filter(filters: dict) -> None:
    """trade_filter.json 원자적 저장."""
    tmp = TRADE_FILTER_PATH.with_suffix(".tmp")
    try:
        TRADE_FILTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(filters, f, ensure_ascii=False, indent=2)
        tmp.replace(TRADE_FILTER_PATH)
    except OSError as exc:
        logger.error("trade_filter.json 저장 실패: %s", exc)
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 유틸: 장 운영 시간
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
# 유틸: 쿨다운
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 유틸: 캔들 → DataFrame → 지표
# ---------------------------------------------------------------------------

_indicators = TechnicalIndicators()


def candles_to_df(candles: list[dict]) -> pd.DataFrame:
    """캔들 리스트 → DataFrame 변환.

    각 캔들은 {"date", "open", "high", "low", "close", "volume"} 형태.
    날짜 오름차순 정렬하여 반환.
    """
    if not candles:
        return pd.DataFrame()

    df = pd.DataFrame(candles)

    # 컬럼 정규화
    rename_map: dict[str, str] = {}
    for col in df.columns:
        lower = col.lower()
        if lower in ("date", "datetime", "time"):
            rename_map[col] = "date"
        elif lower == "open":
            rename_map[col] = "open"
        elif lower == "high":
            rename_map[col] = "high"
        elif lower == "low":
            rename_map[col] = "low"
        elif lower == "close":
            rename_map[col] = "close"
        elif lower in ("volume", "vol"):
            rename_map[col] = "volume"
    if rename_map:
        df = df.rename(columns=rename_map)

    # 숫자 컬럼 변환
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 날짜 정렬
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)

    return df


def calc_indicators(df: pd.DataFrame) -> pd.DataFrame | None:
    """DataFrame에 기술적 지표 추가.

    데이터가 부족하면 None 반환.
    """
    if df is None or df.empty or len(df) < 20:
        logger.debug("지표 계산 불가 — 데이터 부족 (%d행)", 0 if df is None else len(df))
        return None

    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(set(df.columns)):
        logger.warning("지표 계산 불가 — 필수 컬럼 누락: %s", required - set(df.columns))
        return None

    try:
        result = _indicators.get_all_indicators(df)
        return result
    except Exception as exc:
        logger.error("지표 계산 중 오류: %s", exc)
        return None


# ---------------------------------------------------------------------------
# 유틸: 월간/일간 손실 관리
# ---------------------------------------------------------------------------

def _current_month_key() -> str:
    """현재 월 키 (예: '2026-03')."""
    return datetime.now().strftime("%Y-%m")


def _current_week_key() -> str:
    """현재 주 키 (예: '2026-W13')."""
    return datetime.now().strftime("%Y-W%W")


def load_monthly_loss() -> dict:
    """monthly_loss.json 로드.

    Returns:
        {
            "month": "2026-03",
            "loss": 0,
            "consec_stoploss": 0,
            "weekly_loss": {"2026-W13": 0},
        }
    """
    default = {
        "month": _current_month_key(),
        "loss": 0,
        "consec_stoploss": 0,
        "weekly_loss": {},
    }
    if not MONTHLY_LOSS_PATH.exists():
        return default
    try:
        with open(MONTHLY_LOSS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("monthly_loss.json 로드 실패: %s", exc)
        return default

    # 월이 바뀌었으면 리셋
    if data.get("month") != _current_month_key():
        logger.info("월간 손실 데이터 리셋 (%s → %s)", data.get("month"), _current_month_key())
        return default

    # 필드 보정
    data.setdefault("consec_stoploss", 0)
    data.setdefault("weekly_loss", {})
    return data


def save_monthly_loss(data: dict) -> None:
    """monthly_loss.json 저장."""
    tmp = MONTHLY_LOSS_PATH.with_suffix(".tmp")
    try:
        MONTHLY_LOSS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(MONTHLY_LOSS_PATH)
    except OSError as exc:
        logger.error("monthly_loss.json 저장 실패: %s", exc)
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def record_loss_and_stoploss(loss_amount: int) -> None:
    """손실 기록 + 연속손절 카운터 증가.

    Args:
        loss_amount: 손실 금액 (양수)
    """
    data = load_monthly_loss()
    data["loss"] = data.get("loss", 0) + abs(loss_amount)
    data["consec_stoploss"] = data.get("consec_stoploss", 0) + 1

    # 주간 손실도 기록
    week_key = _current_week_key()
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
            orders = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False

    today_str = date.today().isoformat()
    daily_loss = 0

    for order in orders:
        if not isinstance(order, dict):
            continue
        # 오늘 체결된 매도 주문만
        filled_at = order.get("filled_at", "")
        if not filled_at.startswith(today_str):
            continue
        if order.get("side") != "sell":
            continue
        if order.get("status") != "filled":
            continue
        pnl = order.get("pnl", 0)
        if pnl < 0:
            daily_loss += abs(pnl)

    exceeded = daily_loss >= MAX_DAILY_LOSS
    if exceeded:
        logger.warning("당일 손실 한도 초과: %d / %d", daily_loss, MAX_DAILY_LOSS)
    return exceeded


# ---------------------------------------------------------------------------
# 유틸: 관리자 + 사용자
# ---------------------------------------------------------------------------

def get_admin_id() -> str:
    """텔레그램 관리자 ID."""
    return os.getenv("TELEGRAM_ADMIN_ID", "")


def get_users_for_ticker(ticker: str) -> list[str]:
    """특정 종목을 관심 등록한 유저 chat_id 리스트."""
    from data.users_manager import get_users_for_ticker as _get_users
    return _get_users(ticker)


# ---------------------------------------------------------------------------
# 유틸: 마지막 신호 저장
# ---------------------------------------------------------------------------

_LAST_SIGNAL_PATH = ROOT / "data" / "last_signal.json"


def save_last_signal(ticker: str, name: str) -> None:
    """data/last_signal.json 갱신 — 종목별 마지막 신호 시각 기록."""
    try:
        if _LAST_SIGNAL_PATH.exists():
            with open(_LAST_SIGNAL_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
    except (json.JSONDecodeError, OSError):
        data = {}

    data[ticker] = {
        "name": name,
        "time": datetime.now().isoformat(),
    }

    tmp = _LAST_SIGNAL_PATH.with_suffix(".tmp")
    try:
        _LAST_SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(_LAST_SIGNAL_PATH)
    except OSError as exc:
        logger.error("last_signal.json 저장 실패: %s", exc)
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 유틸: 신호 헤더 텍스트
# ---------------------------------------------------------------------------

def build_signal_header(
    ticker: str,
    name: str,
    signal: SignalResult,
    stock: dict,
) -> str:
    """신호 알림 헤더 텍스트 생성.

    Args:
        ticker: 종목코드
        name: 종목명
        signal: SignalResult 객체
        stock: kiwoom_data의 종목 데이터

    Returns:
        포맷된 헤더 문자열
    """
    # 신호 방향 이모지
    if signal.signal_type == SignalType.BUY:
        direction = "🟢 매수"
    elif signal.signal_type == SignalType.SELL:
        direction = "🔴 매도"
    else:
        direction = "⚪ 중립"

    # 강도 텍스트
    strength_map = {
        SignalStrength.STRONG: "강력",
        SignalStrength.MEDIUM: "보통",
        SignalStrength.WEAK: "약함",
    }
    strength_text = strength_map.get(signal.strength, "")

    # 현재가
    current_price = stock.get("current_price", 0)
    change_pct = float(stock.get("change_rate", 0.0))
    change_sign = "+" if change_pct >= 0 else ""

    header_lines = [
        f"{direction} 신호 [{strength_text}]",
        f"📊 {name} ({ticker})",
        f"💰 {current_price:,}원 ({change_sign}{change_pct:.2f}%)",
        f"📈 점수: {signal.score}점",
    ]

    # RSI
    if not pd.isna(signal.rsi):
        header_lines.append(f"RSI: {signal.rsi:.1f}")

    # MACD 크로스
    if signal.macd_cross:
        cross_text = "골든크로스" if signal.macd_cross == "golden" else "데드크로스"
        header_lines.append(f"MACD: {cross_text}")

    # 거래량 비율
    if not pd.isna(signal.vol_ratio):
        header_lines.append(f"거래량비: {signal.vol_ratio:.1f}배")

    # 사유
    if signal.reasons:
        header_lines.append("")
        header_lines.append("📋 근거:")
        for r in signal.reasons:
            header_lines.append(f"  • {r}")

    # 경고
    if signal.warnings:
        header_lines.append("")
        header_lines.append("⚠️ 주의:")
        for w in signal.warnings:
            header_lines.append(f"  • {w}")

    return "\n".join(header_lines)


# ---------------------------------------------------------------------------
# 유틸: 주요 지수 조회
# ---------------------------------------------------------------------------

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


# ===================================================================
# Part 2: 자동매매 로직, 포지션 관리, 스케줄러 메인
# ===================================================================


# ── 매수 금액 계산 ─────────────────────────────────────────────────

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
        holding_count = len(load_auto_positions()) + len(_buy_in_progress)
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


# ── 실패 포지션 정리 ───────────────────────────────────────────────

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


# ── 자동매매 실행 ──────────────────────────────────────────────────

def _auto_trade(ticker: str, name: str, signal: SignalResult,
                stock: dict, notifier, ai_decision: str) -> None:
    """신호 기반 자동매매 실행."""
    from trading.auto_trader import execute_buy, execute_sell

    if not AUTO_TRADE_ENABLED or OPERATION_MODE != "LIVE":
        return
    if not is_whitelisted(ticker):
        return

    # ── 매수 ──
    if signal.signal_type == SignalType.BUY and signal.strength == SignalStrength.STRONG:
        if ai_decision != "매수":
            logger.debug("[자동매매] %s AI 판단=%s → 매수 보류", name, ai_decision)
            return

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

        # 중복 체크
        positions = load_auto_positions()
        if ticker in positions or ticker in _buy_in_progress:
            # 이미 보유 or 매수 진행 중 → 중복 방지
            # 보유 중인데 selling 실패로 돌아온 경우: selling/sell_order_id 복원
            if ticker in positions and positions[ticker].get("selling"):
                return
            if ticker in positions:
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
        if pos.get("selling"):
            return

        qty = int(pos.get("qty", 0))
        if qty <= 0:
            return

        sell_result = execute_sell(ticker, name, qty,
                                   rule_name=f"자동매매_{signal.strength.name}")
        if sell_result.get("status") == "pending":
            fresh = load_auto_positions()
            if ticker in fresh:
                fresh[ticker]["selling"] = True
                fresh[ticker]["sell_order_id"] = sell_result.get("order_id", "")
                save_auto_positions(fresh)
            logger.info("[자동매매] %s 매도 접수 %d주", name, qty)


# ── 포지션 관리 (손절/트레일링) ────────────────────────────────────

def check_auto_positions() -> None:
    """1분마다: 보유 포지션 손절/트레일링/과매수 감시."""
    stale_data = False
    try:
        with open(KIWOOM_DATA_PATH, "r", encoding="utf-8") as f:
            kiwoom = json.load(f)
        updated_at = datetime.strptime(kiwoom["updated_at"], "%Y-%m-%dT%H:%M:%S")
        age = (datetime.now() - updated_at).total_seconds()
        if age > 120:
            logger.warning("kiwoom_data.json이 %.0f초 전 데이터 — 손절만 체크", age)
            stale_data = True
        all_stocks = kiwoom.get("stocks", {})
    except Exception:
        return

    from trading.auto_trader import execute_sell

    positions = _cleanup_failed_positions(load_auto_positions())
    if not positions:
        return

    notifier = TelegramNotifier()
    changed = False
    sl_pct = STOPLOSS_PCT
    ta_pct = TRAILING_ACTIVATE_PCT
    ts_pct = TRAILING_STOP_PCT

    for ticker, pos in list(positions.items()):
        if pos.get("selling"):
            continue

        buy_price = pos.get("buy_price", 0)
        qty = int(pos.get("qty", 0))
        if buy_price <= 0 or qty <= 0:
            continue

        name = pos.get("name", ticker)
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

        # RSI 조회
        rsi = 50.0
        try:
            candles = stock_info.get("candles_1m", [])
            if candles:
                df = candles_to_df(candles)
                df_ind = calc_indicators(df)
                if df_ind is not None and "rsi" in df_ind.columns:
                    rsi = float(df_ind.iloc[-1]["rsi"])
        except Exception:
            pass

        reason = ""

        # 1) 손절
        if pct <= -sl_pct:
            reason = f"손절 ({pct:+.1f}% ≤ -{sl_pct}%)"

        # 데이터 오래된 경우 트레일링/과매수 판단 불가
        elif stale_data:
            continue

        # 2) 과매수 트레일링: RSI≥75, 수익 1%+, 고점 대비 ts_pct*0.5 하락
        elif rsi >= 75 and pct > 1.0 and drop_from_high >= ts_pct * 0.5:
            reason = f"과매수 트레일링 (RSI {rsi:.0f}, 고점 대비 -{drop_from_high:.1f}%)"

        # 3) 트레일링 스탑
        elif trailing_activated and drop_from_high >= ts_pct:
            reason = f"트레일링 스탑 (최고 {high_price:,}원 → {current_price:,}원, -{drop_from_high:.1f}%)"

        else:
            continue

        # 매도 실행
        pnl = (current_price - buy_price) * qty
        sell_res = execute_sell(ticker, name, qty,
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


# ── 신호 감지 래퍼 ─────────────────────────────────────────────────

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
    signal = detect(df_ind, exec_strength=exec_strength, change_rate=change_rate)

    if signal.signal_type == SignalType.NEUTRAL or signal.strength != SignalStrength.STRONG:
        logger.debug("%s: 신호 강도 부족 (strength=%s, score=%d)", name, signal.strength.name, signal.score)
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
        update_cooldown(ticker, signal.signal_type)
        save_last_signal(ticker, name)
        logger.info(
            "[%s] %s %s 알림 전송 (%d명, score=%d, AI판단=%s)",
            signal.strength.name, name, signal.signal_type.value,
            len(target_ids), signal.score, ai_decision or "실패",
        )

    _auto_trade(ticker, name, signal, stock, notifier, ai_decision)


def check_signals() -> None:
    """1분마다: 화이트리스트 종목 신호 감지."""
    if not is_market_hours():
        return
    data = load_kiwoom_data()
    if not data:
        return
    from ai.ai_analyzer import AIAnalyzer
    notifier = TelegramNotifier()
    ai = AIAnalyzer()
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


# ── 일봉 스윙 신호 ────────────────────────────────────────────────

def check_daily_signals() -> None:
    """30분마다: 일봉 스윙 신호 (대형주 임계값 완화)."""
    if not is_market_hours():
        return
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

            # 대형주 완화: score≥4면 STRONG으로 취급
            is_largecap = ticker in LARGECAP_DAILY_THRESHOLD
            if signal.signal_type == SignalType.NEUTRAL:
                continue
            if signal.strength != SignalStrength.STRONG:
                if is_largecap and abs(signal.score) >= LARGECAP_DAILY_MIN_SCORE:
                    logger.info(
                        "[대형주 완화] %s 일봉 score=%d → STRONG 승격",
                        name, signal.score,
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

            _auto_trade(ticker, name, signal, info, notifier, ai_decision)

        except Exception as e:
            logger.error("[일봉 신호] %s 에러: %s", ticker, e)


# ── 급등락 알림 ────────────────────────────────────────────────────

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


# ── 목표가/손절가 감시 ─────────────────────────────────────────────

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


# ── 주문 상태 체크 ─────────────────────────────────────────────────

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

        # 재시작 시 zombie selling 포지션 정리
        positions = load_auto_positions()
        if positions:
            pos_changed = False
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
                }
                save_auto_positions(positions)
                logger.info("[포지션 추가] %s %d주 @%s", name, quantity, f"{_ep:,}")

        # 매수 실패 → _buy_in_progress 정리
        if status == "failed" and side == "buy":
            _buy_in_progress.discard(ticker)


# ── 뉴스/알림 ─────────────────────────────────────────────────────

def send_premarket_news() -> None:
    """08:30: 개장 전 뉴스."""
    if is_market_holiday():
        return
    from data.collector import fetch_market_news
    news = fetch_market_news(10)
    if not news:
        return
    logger.info("개장 전 뉴스 총 %d건 수집", len(news))
    lines = ["📰 [개장 전 뉴스]", "━" * 23]
    for n in news[:10]:
        lines.append(f"  · {n['title'][:50]}")
    msg = "\n".join(lines) + CMD_FOOTER
    TelegramNotifier().broadcast(msg)


def send_news_update() -> None:
    """매시 정각: 장중 뉴스 업데이트."""
    if not is_market_hours():
        return
    from data.collector import fetch_market_news
    news = fetch_market_news(5)
    if not news:
        return
    now = datetime.now().strftime("%H:%M")
    lines = [f"📰 [{now} 뉴스]", "━" * 23]
    for n in news[:5]:
        lines.append(f"  · {n['title'][:50]}")
    msg = "\n".join(lines) + CMD_FOOTER
    notifier = TelegramNotifier()
    notifier.broadcast(msg)
    logger.info("장중 뉴스 %d건 전송 완료", len(news))


def send_market_open() -> None:
    """09:00: 장 시작 알림."""
    if is_market_holiday():
        return
    holding = len(load_auto_positions())
    msg = (
        f"🔔 장 시작 (09:00)\n"
        f"운영 모드: {OPERATION_MODE}\n"
        f"자동매매: {'ON' if AUTO_TRADE_ENABLED else 'OFF'}\n"
        f"보유 종목: {holding}개 / {MAX_SLOTS}슬롯"
        + CMD_FOOTER
    )
    TelegramNotifier().broadcast(msg)


def send_market_close() -> None:
    """15:40: 장 마감 알림."""
    if is_market_holiday():
        return
    positions = load_auto_positions()
    lines = ["🔔 장 마감 (15:40)", "━" * 23]
    if positions:
        lines.append(f"보유 {len(positions)}종목:")
        for t, p in positions.items():
            lines.append(f"  · {p.get('name', t)} {p.get('qty', 0)}주")
    else:
        lines.append("보유 종목 없음")
    msg = "\n".join(lines) + CMD_FOOTER
    TelegramNotifier().broadcast(msg)


def send_daily_report() -> None:
    """16:00: 일일 마감 AI 리포트."""
    if is_market_holiday():
        return
    from ai.ai_analyzer import AIAnalyzer
    ai = AIAnalyzer()
    positions = load_auto_positions()
    monthly = load_monthly_loss()
    report = ai.daily_report(
        portfolio_data={"positions": positions, "monthly_loss": monthly},
        market_data={"indices": fetch_index_prices()},
    )
    msg = f"📋 [일일 마감 리포트]\n━━━━━━━━━━━━━━━━━━━━━━━\n{report}" + CMD_FOOTER
    TelegramNotifier().broadcast(msg)
    logger.info("일일 리포트 전송 완료")


def _prune_order_queue() -> None:
    """06:00: 7일 초과 완료 주문 정리."""
    try:
        with open(ORDER_QUEUE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    from datetime import timedelta
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


# ── 스케줄러 메인 ──────────────────────────────────────────────────

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
