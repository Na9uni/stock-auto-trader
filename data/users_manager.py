"""사용자·관심종목 관리"""
import json
import logging
from pathlib import Path

ROOT = Path(__file__).parent.parent
USERS_PATH = ROOT / "data" / "users.json"
INTEREST_PATH = ROOT / "data" / "interest_list.json"
logger = logging.getLogger("stock_analysis")


# ------------------------------------------------------------------
# 내부 헬퍼 — 원자적 파일 쓰기
# ------------------------------------------------------------------

def _write_json_atomic(path: Path, data: dict | list) -> None:
    """tmp 파일에 쓴 뒤 rename으로 원자적 교체."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ------------------------------------------------------------------
# 사용자 관리
# ------------------------------------------------------------------

def load_users() -> dict:
    """users.json 전체 로드.

    Returns:
        {chat_id: {"interests": [...], ...}, ...}
        파일 없으면 빈 dict.
    """
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"사용자 파일 없음: {USERS_PATH}")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"사용자 파일 JSON 파싱 실패: {e}")
        return {}
    except Exception as e:
        logger.error(f"사용자 파일 로드 실패: {e}")
        return {}


def save_users(users: dict) -> None:
    """users.json 전체 저장 (원자적)."""
    try:
        _write_json_atomic(USERS_PATH, users)
    except Exception as e:
        logger.error(f"사용자 파일 저장 실패: {e}")
        raise


def get_users_for_ticker(ticker: str) -> list[str]:
    """특정 종목을 관심 등록한 유저 chat_id 리스트.

    Args:
        ticker: 종목 코드 (예: "005930")

    Returns:
        해당 종목을 관심 등록한 chat_id 리스트
    """
    users = load_users()
    return [
        uid
        for uid, udata in users.items()
        if ticker in udata.get("interests", [])
    ]


# ------------------------------------------------------------------
# 관심종목 관리
# ------------------------------------------------------------------

def get_all_interests() -> dict:
    """interest_list.json에서 전체 관심종목 로드.

    Returns:
        {종목코드: 종목명, ...}
        파일 없으면 빈 dict.
    """
    try:
        with open(INTEREST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"관심종목 파일 없음: {INTEREST_PATH}")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"관심종목 파일 JSON 파싱 실패: {e}")
        return {}
    except Exception as e:
        logger.error(f"관심종목 파일 로드 실패: {e}")
        return {}


def add_interest(ticker: str, name: str) -> bool:
    """관심종목 추가.

    Args:
        ticker: 종목 코드
        name:   종목명

    Returns:
        True (항상 성공 또는 이미 존재)
    """
    try:
        interests = get_all_interests()
        interests[ticker] = name
        _write_json_atomic(INTEREST_PATH, interests)
        logger.info(f"관심종목 추가: {ticker} ({name})")
        return True
    except Exception as e:
        logger.error(f"관심종목 추가 실패 [{ticker}]: {e}")
        return False


def remove_interest(ticker: str) -> bool:
    """관심종목 삭제.

    Args:
        ticker: 종목 코드

    Returns:
        True — 삭제 성공, False — 종목 없음 또는 오류
    """
    try:
        interests = get_all_interests()
        if ticker not in interests:
            logger.warning(f"관심종목 삭제 실패 — 존재하지 않는 종목: {ticker}")
            return False
        del interests[ticker]
        _write_json_atomic(INTEREST_PATH, interests)
        logger.info(f"관심종목 삭제: {ticker}")
        return True
    except Exception as e:
        logger.error(f"관심종목 삭제 실패 [{ticker}]: {e}")
        return False
