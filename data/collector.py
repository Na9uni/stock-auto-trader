"""뉴스·경제이벤트 수집기"""
import json
import logging
from pathlib import Path

ROOT = Path(__file__).parent.parent
logger = logging.getLogger("stock_analysis")

_NEWS_RSS_URL = (
    "https://news.google.com/rss/search"
    "?q=한국+주식+시장&hl=ko&gl=KR&ceid=KR:ko"
)
_ECONOMIC_EVENTS_PATH = ROOT / "data" / "economic_events.json"


def fetch_market_news(count: int = 10) -> list[dict]:
    """Google News RSS에서 한국 주식 시장 최신 뉴스 수집.

    Args:
        count: 가져올 기사 수 (기본 10)

    Returns:
        [{"title", "link", "pubDate", "source"}, ...]
    """
    try:
        import feedparser  # 런타임 임포트 — 선택적 의존성
    except ImportError:
        logger.error("feedparser 패키지가 설치되어 있지 않습니다: pip install feedparser")
        return []

    try:
        feed = feedparser.parse(_NEWS_RSS_URL)
        results: list[dict] = []
        for entry in feed.entries[:count]:
            results.append({
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "pubDate": entry.get("published", ""),
                "source": entry.get("source", {}).get("title", ""),
            })
        logger.debug(f"뉴스 {len(results)}건 수집 완료")
        return results
    except Exception as e:
        logger.error(f"뉴스 수집 실패: {e}")
        return []


def load_economic_events() -> list[dict]:
    """data/economic_events.json에서 경제 이벤트 로드.

    Returns:
        이벤트 딕셔너리 리스트. 파일 없거나 파싱 실패 시 빈 리스트.
    """
    try:
        with open(_ECONOMIC_EVENTS_PATH, "r", encoding="utf-8") as f:
            events: list[dict] = json.load(f)
        logger.debug(f"경제 이벤트 {len(events)}건 로드 완료")
        return events
    except FileNotFoundError:
        logger.warning(f"경제 이벤트 파일 없음: {_ECONOMIC_EVENTS_PATH}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"경제 이벤트 JSON 파싱 실패: {e}")
        return []
    except Exception as e:
        logger.error(f"경제 이벤트 로드 실패: {e}")
        return []
