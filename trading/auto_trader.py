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
    from trading.kiwoom_order_queue import KiwoomOrderQueue
    logger.info("매수 주문 요청 — %s %s %d주 @%d rule=%s", ticker, name, quantity, price, rule_name)
    return KiwoomOrderQueue().add_order(ticker, "buy", quantity, price, rule_name=rule_name)

def execute_sell(ticker, name, quantity, price=0, rule_name=""):
    from trading.kiwoom_order_queue import KiwoomOrderQueue
    logger.info("매도 주문 요청 — %s %s %d주 @%d rule=%s", ticker, name, quantity, price, rule_name)
    return KiwoomOrderQueue().add_order(ticker, "sell", quantity, price, rule_name=rule_name)
