"""목표가·손절가 감시"""
import json, logging
from pathlib import Path

ROOT = Path(__file__).parent.parent
TARGETS_PATH = ROOT / "data" / "targets.json"
logger = logging.getLogger("stock_analysis")

def load_targets():
    try:
        with open(TARGETS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_targets(targets):
    tmp = TARGETS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(targets, f, ensure_ascii=False, indent=2)
    tmp.replace(TARGETS_PATH)

def check_targets(kiwoom_data):
    targets = load_targets()
    all_stocks = kiwoom_data.get("stocks", {})
    alerts = []
    for ticker, t in targets.items():
        stock = all_stocks.get(ticker)
        if not stock: continue
        cp = int(stock.get("current_price", 0))
        if cp <= 0: continue
        tp = int(t.get("target_price", 0))
        sl = int(t.get("stoploss_price", 0))
        if tp > 0 and cp >= tp:
            alerts.append({"ticker": ticker, "name": t.get("name",""), "type": "target", "price": tp, "current_price": cp})
        if sl > 0 and cp <= sl:
            alerts.append({"ticker": ticker, "name": t.get("name",""), "type": "stoploss", "price": sl, "current_price": cp})
    return alerts
