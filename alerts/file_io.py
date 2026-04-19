"""파일 I/O 유틸리티 — 경로 상수, JSON 로드/저장, 캔들 변환, 지표 계산."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from analysis.indicators import TechnicalIndicators

# ---------------------------------------------------------------------------
# 경로 상수
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

# 로거 설정
(ROOT / "logs").mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("stock_analysis")
if not logger.handlers:
    logger.setLevel(logging.DEBUG)  # 로거 자체는 DEBUG (핸들러별 레벨 제어)
    _fh = logging.FileHandler(
        str(ROOT / "logs" / "stock_analysis.log"), encoding="utf-8"
    )
    _fh.setLevel(logging.INFO)
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
    _ch = logging.StreamHandler()
    _ch.setLevel(logging.INFO)
    _ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_fh)
    logger.addHandler(_ch)

    # 디버그 로그: 모든 판단 과정 기록 (문제 추적용)
    from logging.handlers import TimedRotatingFileHandler
    _debug_handler = TimedRotatingFileHandler(
        str(ROOT / "logs" / "debug.log"),
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    _debug_handler.setLevel(logging.DEBUG)
    _debug_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s %(message)s"
    ))
    logger.addHandler(_debug_handler)

KIWOOM_DATA_PATH = ROOT / "data" / "kiwoom_data.json"
AUTO_POSITIONS_PATH = ROOT / "data" / "auto_positions.json"
ORDER_QUEUE_PATH = ROOT / "data" / "order_queue.json"
MONTHLY_LOSS_PATH = ROOT / "data" / "monthly_loss.json"
MONTHLY_LOSS_PATH_MOCK = ROOT / "data" / "monthly_loss_mock.json"
TRADE_FILTER_PATH = ROOT / "data" / "trade_filter.json"


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
# 파일 I/O
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
            from alerts.notifications import get_admin_id
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
# 월간 손실 파일 I/O
# ---------------------------------------------------------------------------

def _current_month_key() -> str:
    """현재 월 키 (예: '2026-03')."""
    return datetime.now().strftime("%Y-%m")


def _current_week_key() -> str:
    """현재 주 키 (예: '2026-W13')."""
    return datetime.now().strftime("%Y-W%W")


def _monthly_loss_path(mock: bool = False):
    """MOCK 여부에 따라 경로 선택. LIVE 손실 누적과 MOCK 시뮬레이션 누적을 분리."""
    return MONTHLY_LOSS_PATH_MOCK if mock else MONTHLY_LOSS_PATH


def load_monthly_loss(mock: bool = False) -> dict:
    """monthly_loss.json 로드.

    Args:
        mock: True이면 monthly_loss_mock.json에서 로드 (MOCK 전용).

    Returns:
        {
            "month": "2026-03",
            "loss": 0,
            "consec_stoploss": 0,
            "weekly_loss": {"2026-W13": 0},
        }
    """
    path = _monthly_loss_path(mock)
    default = {
        "month": _current_month_key(),
        "loss": 0,
        "consec_stoploss": 0,
        "weekly_loss": {},
    }
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("%s 로드 실패: %s", path.name, exc)
        return default

    # 월이 바뀌었으면 리셋
    if data.get("month") != _current_month_key():
        logger.info("월간 손실 데이터 리셋 (%s → %s)", data.get("month"), _current_month_key())
        return default

    # 필드 보정
    data.setdefault("consec_stoploss", 0)
    data.setdefault("weekly_loss", {})
    return data


def save_monthly_loss(data: dict, mock: bool = False) -> None:
    """monthly_loss.json 저장.

    Args:
        mock: True이면 monthly_loss_mock.json에 저장.
    """
    path = _monthly_loss_path(mock)
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except OSError as exc:
        logger.error("%s 저장 실패: %s", path.name, exc)
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 캔들 → DataFrame → 지표
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
