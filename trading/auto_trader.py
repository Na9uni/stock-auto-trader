"""자동매매 실행기"""
import json, os, logging
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
logger = logging.getLogger("stock_analysis")
KIWOOM_DATA_PATH = ROOT / "data" / "kiwoom_data.json"

def _get_protection_margin(price: int) -> float:
    """가격대별 보호 지정가 마진."""
    if price < 5000:
        return 0.01    # 1.0% (소형주)
    elif price < 20000:
        return 0.005   # 0.5%
    elif price < 50000:
        return 0.004   # 0.4%
    else:
        return 0.003   # 0.3% (대형주)


def get_current_price(ticker: str) -> int:
    try:
        with open(KIWOOM_DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        stock = data.get("stocks", {}).get(ticker, {})
        return int(stock.get("close", stock.get("current_price", 0)))
    except Exception:
        return 0

def execute_buy(ticker, name, quantity, price=0, rule_name=""):
    """매수: 현재가 + 가격대별 보호 지정가"""
    from trading.kiwoom_order_queue import KiwoomOrderQueue
    from utils.tick_size import align_tick_size
    if price > 0:
        margin = _get_protection_margin(price)
        limit_price = align_tick_size(int(price * (1 + margin)), direction="up")
        # 상한가 클리핑 (전일 종가 +30%)
        upper_limit = align_tick_size(int(price * 1.30), direction="down")
        limit_price = min(limit_price, upper_limit)
    else:
        limit_price = 0
    order_type = "limit" if limit_price > 0 else "market"
    logger.info("매수 주문 — %s %s %d주 @%d(보호=%d) rule=%s", ticker, name, quantity, price, limit_price, rule_name)
    return KiwoomOrderQueue().add_order(ticker, "buy", quantity, limit_price, order_type=order_type, rule_name=rule_name)

def execute_sell(ticker, name, quantity, price=0, rule_name=""):
    """매도: 현재가 - 가격대별 보호 지정가"""
    from trading.kiwoom_order_queue import KiwoomOrderQueue
    from utils.tick_size import align_tick_size
    if price > 0:
        margin = _get_protection_margin(price)
        limit_price = align_tick_size(int(price * (1 - margin)), direction="down")
        # 하한가 클리핑 (전일 종가 -30%)
        lower_limit = align_tick_size(int(price * 0.70), direction="up")
        limit_price = max(limit_price, lower_limit)
    else:
        limit_price = 0
    order_type = "limit" if limit_price > 0 else "market"
    logger.info("매도 주문 — %s %s %d주 @%d(보호=%d) rule=%s", ticker, name, quantity, price, limit_price, rule_name)
    return KiwoomOrderQueue().add_order(ticker, "sell", quantity, limit_price, order_type=order_type, rule_name=rule_name)
