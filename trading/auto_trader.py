"""자동매매 실행기"""
import json, os, logging
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
logger = logging.getLogger("stock_analysis")
KIWOOM_DATA_PATH = ROOT / "data" / "kiwoom_data.json"

def get_current_price(ticker: str) -> int:
    try:
        with open(KIWOOM_DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        stock = data.get("stocks", {}).get(ticker, {})
        return int(stock.get("close", stock.get("current_price", 0)))
    except Exception:
        return 0

def execute_buy(ticker, name, quantity, price=0, rule_name=""):
    """매수: 현재가 +1% 보호 지정가"""
    from trading.kiwoom_order_queue import KiwoomOrderQueue
    from kiwoom.kiwoom_collector import align_tick_size
    if price > 0:
        limit_price = align_tick_size(int(price * 1.01), direction="up")
    else:
        limit_price = 0  # price=0이면 시장가 유지
    order_type = "limit" if limit_price > 0 else "market"
    logger.info("매수 주문 요청 — %s %s %d주 @%d(보호지정가=%d) rule=%s", ticker, name, quantity, price, limit_price, rule_name)
    return KiwoomOrderQueue().add_order(ticker, "buy", quantity, limit_price, order_type=order_type, rule_name=rule_name)

def execute_sell(ticker, name, quantity, price=0, rule_name=""):
    """매도: 현재가 -1% 보호 지정가"""
    from trading.kiwoom_order_queue import KiwoomOrderQueue
    from kiwoom.kiwoom_collector import align_tick_size
    if price > 0:
        limit_price = align_tick_size(int(price * 0.99), direction="down")
    else:
        limit_price = 0
    order_type = "limit" if limit_price > 0 else "market"
    logger.info("매도 주문 요청 — %s %s %d주 @%d(보호지정가=%d) rule=%s", ticker, name, quantity, price, limit_price, rule_name)
    return KiwoomOrderQueue().add_order(ticker, "sell", quantity, limit_price, order_type=order_type, rule_name=rule_name)
