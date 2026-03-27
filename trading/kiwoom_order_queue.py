"""키움 주문 큐 — JSON IPC 기반 주문 중개"""
import json
import uuid
import logging
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
QUEUE_PATH = ROOT / "data" / "order_queue.json"
QUEUE_TMP = QUEUE_PATH.with_suffix(".tmp")
logger = logging.getLogger("stock_analysis")


class KiwoomOrderQueue:
    """OBSERVE 모드면 주문 차단, LIVE면 JSON에 기록"""

    def __init__(self):
        from dotenv import load_dotenv
        import os
        load_dotenv(ROOT / ".env")
        self.observe_mode = os.getenv("OPERATION_MODE", "OBSERVE").upper() == "OBSERVE"
        self.mock_mode = os.getenv("KIWOOM_MOCK_MODE", "True").lower() == "true"

    def _load_queue(self) -> dict:
        try:
            with open(QUEUE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"orders": []}

    def _save_queue(self, data: dict):
        with open(QUEUE_TMP, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        QUEUE_TMP.replace(QUEUE_PATH)

    def add_order(
        self,
        ticker: str,
        side: str,
        quantity: int,
        price: int = 0,
        order_type: str = "market",
        rule_name: str = "",
    ) -> dict:
        """주문 추가. OBSERVE면 차단. limit+price=0이면 에러 반환."""
        if self.observe_mode:
            logger.info(
                "[OBSERVE] 주문 차단 — ticker=%s side=%s qty=%d",
                ticker, side, quantity,
            )
            return {"order_id": "", "status": "blocked", "message": "OBSERVE 모드"}

        if order_type == "limit" and price <= 0:
            return {
                "order_id": None, "status": "error",
                "message": f"지정가 주문은 가격 > 0 필요: {price}",
            }

        order_id = str(uuid.uuid4())[:8]
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        order = {
            "id": order_id,
            "ticker": ticker,
            "side": side,
            "quantity": quantity,
            "price": price,
            "order_type": order_type,
            "rule_name": rule_name,
            "status": "pending",
            "mock_mode": self.mock_mode,
            "created_at": now,
            "submitted_at": None,
            "executed_at": None,
            "exec_price": None,
            "order_number": None,
            "cumul_exec_qty": 0,
            "fail_reason": None,
        }

        data = self._load_queue()
        data.setdefault("orders", []).append(order)
        self._save_queue(data)

        logger.info(
            "주문 큐 추가 — id=%s ticker=%s side=%s qty=%d price=%d",
            order_id, ticker, side, quantity, price,
        )
        return {"order_id": order_id, "status": "pending", "message": "주문 큐 추가 완료"}
