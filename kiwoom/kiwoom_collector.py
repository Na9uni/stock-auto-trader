"""
kiwoom_collector.py
32-bit Python 전용 키움 OpenAPI+ 데이터 수집기.

- 30초마다 감시종목 실시간 데이터 수집 -> data/kiwoom_data.json atomic write
- order_queue.json pending 주문 -> SendOrder 실행
- chejan 이벤트로 체결/거부 감지 -> order_queue.json 갱신
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer
from PyQt5.QtWidgets import QApplication

try:
    import holidays
except ImportError:
    holidays = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
OUTPUT_PATH = ROOT / "data" / "kiwoom_data.json"
ORDER_QUEUE_PATH = ROOT / "data" / "order_queue.json"
WATCH_LIST_PATH = ROOT / "data" / "watch_list.json"
INTEREST_LIST_PATH = ROOT / "data" / "interest_list.json"
AUTO_POSITIONS_PATH = ROOT / "data" / "auto_positions.json"

COLLECT_INTERVAL_SEC = 30
CANDLE_COUNT = 120
INTEREST_INTERVAL_SEC = 90
INTEREST_CANDLE_COUNT = 60
INTEREST_EVERY_N_TICKS = 3       # 90s / 30s
ACCOUNT_EVERY_N_TICKS = 5        # 150s / 30s = 2.5min
DAILY_EVERY_N_TICKS = 20         # 600s / 30s = 10min

TR_SLEEP_SEC = 0.6

logger = logging.getLogger("kiwoom")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically: write to temp file then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=".kiwoom_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # Windows: target must not exist for os.rename
        if os.path.exists(path):
            os.replace(tmp_path, str(path))
        else:
            os.rename(tmp_path, str(path))
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _read_json(path: Path) -> Any:
    """Read JSON file, return None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _strip_sign(value: str) -> str:
    """Remove leading +/- and whitespace from Kiwoom price strings."""
    return value.strip().lstrip("+-").strip()


def _safe_int(value: str, default: int = 0) -> int:
    try:
        return int(_strip_sign(value))
    except (ValueError, TypeError):
        return default


def _safe_float(value: str, default: float = 0.0) -> float:
    try:
        return float(_strip_sign(value))
    except (ValueError, TypeError):
        return default


def align_tick_size(price: int, direction: str = "down") -> int:
    """한국 주식시장 호가 단위에 맞춤.
    direction: "up"=올림, "down"=내림
    """
    if price < 2000:
        tick = 1
    elif price < 5000:
        tick = 5
    elif price < 20000:
        tick = 10
    elif price < 50000:
        tick = 50
    elif price < 200000:
        tick = 100
    elif price < 500000:
        tick = 500
    else:
        tick = 1000
    if direction == "up":
        return ((price + tick - 1) // tick) * tick
    return (price // tick) * tick


def _is_market_open() -> bool:
    """Check if today is a Korean trading day (weekday + not holiday)."""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    if holidays is not None:
        kr_holidays = holidays.KR(years=now.year)
        if now.date() in kr_holidays:
            return False
    return True


def _in_trading_hours() -> bool:
    """Check if current time is within 09:00~15:30 KST."""
    now = datetime.now()
    market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


# ---------------------------------------------------------------------------
# KiwoomAPI  (QAxWidget wrapper)
# ---------------------------------------------------------------------------

class KiwoomAPI(QAxWidget):
    """Thin wrapper around Kiwoom OpenAPI+ COM object."""

    def __init__(self) -> None:
        super().__init__()
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")

        # TR response storage
        self._tr_data: dict[str, Any] = {}
        self._tr_remaining: int = 0
        self._tr_done = False

        # Chejan race-condition buffers
        self._pending_fills: list[dict[str, Any]] = []
        self._pending_fails: list[dict[str, Any]] = []
        self._queue_processing: bool = False

        # Account info
        self.account_number: str = ""
        self._logged_in: bool = False

        # Account TR failure tracking
        self._account_fail_count: int = 0
        self._account_disabled: bool = False

        # Real-data callback (set by KiwoomCollector)
        self._real_data_callback = None

        # Connect event handlers
        self.OnEventConnect.connect(self._on_event_connect)
        self.OnReceiveTrData.connect(self._on_receive_tr_data)
        self.OnReceiveChejanData.connect(self._on_receive_chejan_data)
        self.OnReceiveRealData.connect(self._on_receive_real_data)

        self._event_loop: QEventLoop | None = None

    # ----- Login -----

    def login(self) -> bool:
        """Block until login completes. Returns True on success."""
        self._logged_in = False
        self.dynamicCall("CommConnect()")
        self._event_loop = QEventLoop()
        self._event_loop.exec_()
        if self._logged_in:
            accounts = self.dynamicCall("GetLoginInfo(QString)", "ACCNO")
            if accounts:
                self.account_number = accounts.strip().split(";")[0]
                logger.info("Login OK. Account: %s", self.account_number)
        return self._logged_in

    def _on_event_connect(self, err_code: int) -> None:
        if err_code == 0:
            self._logged_in = True
            logger.info("OnEventConnect: success")
        else:
            logger.error("OnEventConnect: failed (code=%d)", err_code)
        if self._event_loop is not None:
            self._event_loop.quit()
            self._event_loop = None

    # ----- TR request helpers -----

    def _set_input(self, field: str, value: str) -> None:
        self.dynamicCall("SetInputValue(QString, QString)", field, value)

    def _comm_rq_data(
        self, rqname: str, trcode: str, prev_next: int, screen_no: str
    ) -> int:
        return self.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname, trcode, prev_next, screen_no,
        )

    def _get_comm_data(
        self, trcode: str, rqname: str, index: int, field: str
    ) -> str:
        raw = self.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            trcode, rqname, index, field,
        )
        return raw.strip() if raw else ""

    def _get_repeat_cnt(self, trcode: str, rqname: str) -> int:
        return self.dynamicCall(
            "GetRepeatCnt(QString, QString)", trcode, rqname,
        )

    def _wait_tr(self, timeout_ms: int = 10000) -> bool:
        """Wait for TR response via local event loop. Returns True if received."""
        self._tr_done = False
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)

        def _check() -> None:
            if self._tr_done:
                timer.stop()
                loop.quit()

        def _timeout() -> None:
            logger.warning("TR request timed out")
            loop.quit()

        check_timer = QTimer()
        check_timer.timeout.connect(_check)
        check_timer.start(50)
        timer.timeout.connect(_timeout)
        timer.start(timeout_ms)
        loop.exec_()
        check_timer.stop()
        return self._tr_done

    # ----- OnReceiveTrData -----

    def _on_receive_tr_data(
        self,
        screen_no: str,
        rqname: str,
        trcode: str,
        record_name: str,
        prev_next: str,
        _data_len: int = 0,
        _err_code: str = "",
        _msg1: str = "",
        _msg2: str = "",
    ) -> None:
        handler = {
            "opt10001_req": self._handle_opt10001,
            "opt10080_req": self._handle_opt10080,
            "opt10081_req": self._handle_opt10081,
            "opw00004_req": self._handle_opw00004,
            "opw00018_req": self._handle_opw00018,
        }.get(rqname)

        if handler is not None:
            handler(trcode, rqname)
        else:
            logger.warning("Unknown rqname: %s", rqname)

        self._tr_remaining = int(prev_next) if prev_next else 0
        self._tr_done = True

    # ----- opt10001: stock basic info -----

    def request_opt10001(self, ticker: str) -> dict[str, Any] | None:
        self._set_input("종목코드", ticker)
        ret = self._comm_rq_data("opt10001_req", "opt10001", 0, "1001")
        if ret != 0:
            logger.error("opt10001 request failed (ret=%d, ticker=%s)", ret, ticker)
            return None
        if not self._wait_tr():
            return None
        return self._tr_data.get("opt10001")

    def _handle_opt10001(self, trcode: str, rqname: str) -> None:
        price = _safe_int(self._get_comm_data(trcode, rqname, 0, "현재가"))
        change_rate = _safe_float(self._get_comm_data(trcode, rqname, 0, "등락율"))
        open_price = _safe_int(self._get_comm_data(trcode, rqname, 0, "시가"))
        volume = _safe_int(self._get_comm_data(trcode, rqname, 0, "거래량"))
        high = _safe_int(self._get_comm_data(trcode, rqname, 0, "고가"))
        low = _safe_int(self._get_comm_data(trcode, rqname, 0, "저가"))
        exec_strength = _safe_float(self._get_comm_data(trcode, rqname, 0, "체결강도"))
        prev_close = _safe_int(self._get_comm_data(trcode, rqname, 0, "기준가"))
        prev_volume = _safe_int(self._get_comm_data(trcode, rqname, 0, "전일거래량"))
        trade_amount = _safe_int(self._get_comm_data(trcode, rqname, 0, "거래대금"))

        self._tr_data["opt10001"] = {
            "current_price": abs(price),
            "change_rate": change_rate,
            "open": abs(open_price),
            "high": abs(high),
            "low": abs(low),
            "volume": volume,
            "exec_strength": float(exec_strength or 0),
            "prev_close": abs(prev_close),
            "prev_volume": abs(prev_volume),
            "trade_amount": abs(trade_amount),
        }

    # ----- opt10080: 5-minute candles -----

    def request_opt10080(self, ticker: str, count: int = CANDLE_COUNT) -> list[dict] | None:
        self._set_input("종목코드", ticker)
        self._set_input("틱범위", "5")
        self._set_input("수정주가구분", "1")
        ret = self._comm_rq_data("opt10080_req", "opt10080", 0, "2001")
        if ret != 0:
            logger.error("opt10080 request failed (ret=%d, ticker=%s)", ret, ticker)
            return None
        if not self._wait_tr():
            return None
        return self._tr_data.get("opt10080", [])[:count]

    def _handle_opt10080(self, trcode: str, rqname: str) -> None:
        cnt = self._get_repeat_cnt(trcode, rqname)
        candles: list[dict[str, Any]] = []
        for i in range(cnt):
            dt_str = self._get_comm_data(trcode, rqname, i, "체결시간")
            candles.append({
                "datetime": dt_str,
                "open": abs(_safe_int(self._get_comm_data(trcode, rqname, i, "시가"))),
                "high": abs(_safe_int(self._get_comm_data(trcode, rqname, i, "고가"))),
                "low": abs(_safe_int(self._get_comm_data(trcode, rqname, i, "저가"))),
                "close": abs(_safe_int(self._get_comm_data(trcode, rqname, i, "현재가"))),
                "volume": _safe_int(self._get_comm_data(trcode, rqname, i, "거래량")),
            })
        self._tr_data["opt10080"] = candles

    # ----- opt10081: daily candles -----

    def request_opt10081(self, ticker: str, count: int = CANDLE_COUNT) -> list[dict] | None:
        self._set_input("종목코드", ticker)
        self._set_input("기준일자", datetime.now().strftime("%Y%m%d"))
        self._set_input("수정주가구분", "1")
        ret = self._comm_rq_data("opt10081_req", "opt10081", 0, "3001")
        if ret != 0:
            logger.error("opt10081 request failed (ret=%d, ticker=%s)", ret, ticker)
            return None
        if not self._wait_tr():
            return None
        return self._tr_data.get("opt10081", [])[:count]

    def _handle_opt10081(self, trcode: str, rqname: str) -> None:
        cnt = self._get_repeat_cnt(trcode, rqname)
        candles: list[dict[str, Any]] = []
        for i in range(cnt):
            dt_str = self._get_comm_data(trcode, rqname, i, "일자")
            candles.append({
                "date": dt_str,
                "open": abs(_safe_int(self._get_comm_data(trcode, rqname, i, "시가"))),
                "high": abs(_safe_int(self._get_comm_data(trcode, rqname, i, "고가"))),
                "low": abs(_safe_int(self._get_comm_data(trcode, rqname, i, "저가"))),
                "close": abs(_safe_int(self._get_comm_data(trcode, rqname, i, "현재가"))),
                "volume": _safe_int(self._get_comm_data(trcode, rqname, i, "거래량")),
            })
        self._tr_data["opt10081"] = candles

    # ----- opw00004: account balance -----

    def request_opw00004(self) -> dict[str, Any] | None:
        if self._account_disabled:
            return None
        if not self.account_number:
            logger.warning("opw00004: no account number")
            return None
        self._set_input("계좌번호", self.account_number)
        self._set_input("비밀번호", "")
        self._set_input("상장폐지조회구분", "0")
        self._set_input("비밀번호입력매체구분", "00")
        ret = self._comm_rq_data("opw00004_req", "opw00004", 0, "4001")
        if ret != 0:
            logger.error("opw00004 request failed (ret=%d)", ret)
            self._on_account_fail()
            return None
        if not self._wait_tr(timeout_ms=8000):
            self._on_account_fail()
            return None
        result = self._tr_data.get("opw00004")
        if result is not None:
            self._account_fail_count = 0
        return result

    def _on_account_fail(self) -> None:
        self._account_fail_count += 1
        if self._account_fail_count >= 3:
            self._account_disabled = True
            logger.warning(
                "opw00004 disabled for this session after %d consecutive failures",
                self._account_fail_count,
            )

    def _handle_opw00004(self, trcode: str, rqname: str) -> None:
        balance = _safe_int(self._get_comm_data(trcode, rqname, 0, "예수금"))
        total_eval = _safe_int(self._get_comm_data(trcode, rqname, 0, "총평가금액"))
        est_deposit = _safe_int(self._get_comm_data(trcode, rqname, 0, "추정예탁자산"))

        self._tr_data["opw00004"] = {
            "balance": balance,
            "total_eval": total_eval,
            "est_deposit": est_deposit,
        }

    # ----- opw00018: 계좌 보유종목 상세 -----

    def request_opw00018(self) -> list[dict] | None:
        """계좌평가잔고내역 — 보유종목 목록 조회."""
        if self._account_disabled:
            return None
        if not self.account_number:
            return None
        self._set_input("계좌번호", self.account_number)
        self._set_input("비밀번호", "")
        self._set_input("비밀번호입력매체구분", "00")
        self._set_input("조회구분", "1")
        ret = self._comm_rq_data("opw00018_req", "opw00018", 0, "4018")
        if ret != 0:
            logger.error("opw00018 request failed (ret=%d)", ret)
            return None
        if not self._wait_tr(timeout_ms=8000):
            return None
        return self._tr_data.get("opw00018")

    def _handle_opw00018(self, trcode: str, rqname: str) -> None:
        count = self._get_repeat_cnt(trcode, rqname)
        holdings: list[dict] = []
        for i in range(count):
            ticker = self._get_comm_data(trcode, rqname, i, "종목번호").strip().replace("A", "")
            name = self._get_comm_data(trcode, rqname, i, "종목명").strip()
            qty = _safe_int(self._get_comm_data(trcode, rqname, i, "보유수량"))
            avg_price = _safe_int(self._get_comm_data(trcode, rqname, i, "매입가"))
            current_price = _safe_int(self._get_comm_data(trcode, rqname, i, "현재가"))
            eval_amt = _safe_int(self._get_comm_data(trcode, rqname, i, "평가금액"))
            pnl_amt = _safe_int(self._get_comm_data(trcode, rqname, i, "평가손익"))
            pnl_pct = float(self._get_comm_data(trcode, rqname, i, "수익률(%)").strip() or "0")
            if ticker and qty > 0:
                holdings.append({
                    "ticker": ticker,
                    "name": name,
                    "qty": qty,
                    "avg_price": abs(avg_price),
                    "current_price": abs(current_price),
                    "eval_amt": abs(eval_amt),
                    "pnl_amt": pnl_amt,
                    "pnl_pct": pnl_pct / 100.0 if abs(pnl_pct) > 1 else pnl_pct,
                })
        self._tr_data["opw00018"] = holdings
        logger.info("opw00018: %d holdings", len(holdings))

    # ----- SendOrder -----

    def send_order(
        self,
        ticker: str,
        order_type: int,
        quantity: int,
        price: int,
        price_type: str = "00",
    ) -> int:
        """
        Send order to Kiwoom.

        order_type: 1=buy, 2=sell
        price_type: "00"=limit, "03"=market
        Returns: 0 on success, nonzero on failure.
        """
        if not self.account_number:
            logger.error("send_order: no account number")
            return -1

        screen_no = "5001"
        ret = self.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            ["order_req", screen_no, self.account_number,
             order_type, ticker, quantity, price, price_type, ""],
        )
        if ret == 0:
            logger.info(
                "SendOrder OK: type=%d ticker=%s qty=%d price=%d",
                order_type, ticker, quantity, price,
            )
        else:
            logger.error(
                "SendOrder FAIL (ret=%d): type=%d ticker=%s qty=%d price=%d",
                ret, order_type, ticker, quantity, price,
            )
        return ret

    # ----- OnReceiveChejanData -----

    def _on_receive_chejan_data(self, s_gubun: str, _n_item_cnt: int, _s_fid_list: str) -> None:
        if s_gubun == "0":
            self._handle_chejan_fill()
        elif s_gubun == "3":
            self._handle_chejan_reject()
        else:
            logger.debug("Chejan unknown gubun: %s", s_gubun)

    def _get_chejan_data(self, fid: int) -> str:
        raw = self.dynamicCall("GetChejanData(int)", fid)
        return raw.strip() if raw else ""

    def _handle_chejan_fill(self) -> None:
        """Process fill (sGubun=0): 접수/체결."""
        order_no = self._get_chejan_data(9203)
        ticker = self._get_chejan_data(9001).replace("A", "").strip()
        cumul_qty = _safe_int(self._get_chejan_data(911))
        exec_price = _safe_int(self._get_chejan_data(910))
        order_status = self._get_chejan_data(913)  # 접수, 체결, ...

        fill_event: dict[str, Any] = {
            "order_no": order_no,
            "ticker": ticker,
            "cumul_qty": cumul_qty,
            "exec_price": abs(exec_price),
            "order_status": order_status,
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }

        logger.info(
            "Chejan FILL: order_no=%s ticker=%s cumul=%d price=%d status=%s",
            order_no, ticker, cumul_qty, abs(exec_price), order_status,
        )

        if self._queue_processing:
            self._pending_fills.append(fill_event)
            return

        self._update_order_queue_on_fill(fill_event)

    def _handle_chejan_reject(self) -> None:
        """Process reject (sGubun=3): 거부."""
        ticker = self._get_chejan_data(9001).replace("A", "").strip()
        reason = self._get_chejan_data(919)

        fail_event: dict[str, Any] = {
            "ticker": ticker,
            "reason": reason,
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }

        logger.warning("Chejan REJECT: ticker=%s reason=%s", ticker, reason)

        if self._queue_processing:
            self._pending_fails.append(fail_event)
            return

        self._fail_submitted_order(fail_event)

    # ----- Order queue file operations -----

    def _update_order_queue_on_fill(self, fill: dict[str, Any]) -> None:
        """Read order_queue.json, apply fill, write back."""
        data = _read_json(ORDER_QUEUE_PATH)
        if data is None:
            data = {"orders": []}
        orders = data.get("orders", [])
        changed = self._apply_fill_event(orders, fill)
        if changed:
            data["orders"] = orders
            _atomic_write_json(ORDER_QUEUE_PATH, data)

    def _apply_fill_event(self, orders: list[dict], fill: dict[str, Any]) -> bool:
        """Apply a fill event to matching submitted/failed order. Returns True if applied.

        NOTE: 'failed' status is intentionally included in matching because the 3-min
        timeout may mark orders as failed before the actual chejan fill event arrives.
        This allows late fills to be correctly applied to timed-out orders.
        """
        for order in orders:
            if (
                order.get("status") in ("submitted", "failed")
                and order.get("ticker") == fill["ticker"]
                and (
                    not order.get("order_number")
                    or order.get("order_number") == fill["order_no"]
                )
            ):
                order["order_number"] = fill["order_no"]
                order["cumul_exec_qty"] = fill["cumul_qty"]
                order["exec_price"] = fill["exec_price"]
                if fill["cumul_qty"] >= order.get("quantity", 0):
                    order["status"] = "executed"
                    order["executed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                return True
        return False

    def _fail_submitted_order(self, fail: dict[str, Any]) -> None:
        """Mark submitted order for ticker as failed."""
        data = _read_json(ORDER_QUEUE_PATH)
        if data is None:
            return
        orders = data.get("orders", [])
        changed = False
        for order in orders:
            if (
                order.get("status") == "submitted"
                and order.get("ticker") == fail["ticker"]
            ):
                order["status"] = "failed"
                order["fail_reason"] = fail.get("reason", "rejected by server")
                order["failed_at"] = fail["timestamp"]
                changed = True
                break
        if changed:
            data["orders"] = orders
            _atomic_write_json(ORDER_QUEUE_PATH, data)

    # ----- OnReceiveRealData -----

    def _on_receive_real_data(
        self, ticker: str, real_type: str, real_data: str
    ) -> None:
        """실시간 체결/호가 이벤트 수신."""
        ticker = ticker.strip()

        if real_type == "주식체결":
            try:
                price = abs(int(self._get_comm_real_data(ticker, 10) or "0"))
                change_rate = float(self._get_comm_real_data(ticker, 12) or "0")
                volume = abs(int(self._get_comm_real_data(ticker, 15) or "0"))
                exec_strength = float(self._get_comm_real_data(ticker, 228) or "0")
            except (ValueError, TypeError):
                return

            if self._real_data_callback is not None:
                self._real_data_callback(ticker, {
                    "current_price": price,
                    "change_rate": change_rate,
                    "volume": volume,
                    "exec_strength": exec_strength,
                    "real_type": "trade",
                })

        elif real_type == "주식호가잔량":
            orderbook: dict[str, list[dict[str, int]]] = {"ask": [], "bid": []}
            try:
                for i in range(5):  # 호가 5단계
                    ask_price = abs(int(self._get_comm_real_data(ticker, 41 + i) or "0"))
                    bid_price = abs(int(self._get_comm_real_data(ticker, 51 + i) or "0"))
                    ask_qty = abs(int(self._get_comm_real_data(ticker, 61 + i) or "0"))
                    bid_qty = abs(int(self._get_comm_real_data(ticker, 71 + i) or "0"))
                    if ask_price > 0:
                        orderbook["ask"].append({"price": ask_price, "qty": ask_qty})
                    if bid_price > 0:
                        orderbook["bid"].append({"price": bid_price, "qty": bid_qty})
            except (ValueError, TypeError):
                return

            if self._real_data_callback is not None:
                self._real_data_callback(ticker, {
                    "orderbook": orderbook,
                    "real_type": "orderbook",
                })

    def _get_comm_real_data(self, ticker: str, fid: int) -> str:
        """GetCommRealData wrapper — returns stripped string."""
        raw = self.dynamicCall("GetCommRealData(QString, int)", [ticker, fid])
        return raw.strip() if raw else ""


# ---------------------------------------------------------------------------
# KiwoomCollector
# ---------------------------------------------------------------------------

class KiwoomCollector:
    """Orchestrates periodic data collection from Kiwoom API."""

    def __init__(self, api: KiwoomAPI) -> None:
        self.api = api

        # Ticker lists
        self._tickers: dict[str, str] = {}           # ticker -> name (primary/watch)
        self._interest_tickers: dict[str, str] = {}   # ticker -> name (secondary)

        # Collected data
        self._data: dict[str, Any] = {
            "stocks": {},
            "account": {
                "balance": 0,
                "total_eval": 0,
                "est_deposit": 0,
                "stocks": [],
            },
            "updated_at": "",
        }

        # Connect real-data callback
        self.api._real_data_callback = self._on_real_data

        # Restore previous account data if available
        self._restore_previous_data()

        # Load watch lists
        self._load_watch_list()
        self._load_interest_list()

    def _restore_previous_data(self) -> None:
        """Load last saved kiwoom_data.json to preserve account balance on restart."""
        existing = _read_json(OUTPUT_PATH)
        if existing is None:
            return
        prev_account = existing.get("account", {})
        if prev_account.get("balance", 0) > 0:
            self._data["account"]["balance"] = prev_account["balance"]
        if prev_account.get("total_eval", 0) > 0:
            self._data["account"]["total_eval"] = prev_account["total_eval"]
        if prev_account.get("est_deposit", 0) > 0:
            self._data["account"]["est_deposit"] = prev_account["est_deposit"]
        logger.info(
            "Restored account: balance=%d total_eval=%d",
            self._data["account"]["balance"],
            self._data["account"]["total_eval"],
        )

    def _load_watch_list(self) -> None:
        """Load primary watch list from data/watch_list.json."""
        data = _read_json(WATCH_LIST_PATH)
        if data is None:
            logger.warning("watch_list.json not found or invalid")
            return
        tickers = data if isinstance(data, dict) else {}
        # Support both {ticker: name} and {tickers: [{ticker, name}]}
        if "tickers" in tickers:
            for item in tickers["tickers"]:
                code = item.get("ticker", item.get("code", ""))
                name = item.get("name", code)
                if code:
                    self._tickers[code] = name
        else:
            for code, name in tickers.items():
                if isinstance(name, str):
                    self._tickers[code] = name
        logger.info("Watch list loaded: %d tickers", len(self._tickers))

    def _load_interest_list(self) -> None:
        """Load secondary interest list from data/interest_list.json."""
        data = _read_json(INTEREST_LIST_PATH)
        if data is None:
            logger.info("interest_list.json not found, skipping")
            return
        tickers = data if isinstance(data, dict) else {}
        if "tickers" in tickers:
            for item in tickers["tickers"]:
                code = item.get("ticker", item.get("code", ""))
                name = item.get("name", code)
                if code and code not in self._tickers:
                    self._interest_tickers[code] = name
        else:
            for code, name in tickers.items():
                if isinstance(name, str) and code not in self._tickers:
                    self._interest_tickers[code] = name
        logger.info("Interest list loaded: %d tickers", len(self._interest_tickers))

    # ----- Data collection -----

    def collect_basic(self, ticker: str) -> dict[str, Any] | None:
        """Fetch opt10001 basic info for a ticker."""
        result = self.api.request_opt10001(ticker)
        time.sleep(TR_SLEEP_SEC)
        return result

    def collect_candles_1m(self, ticker: str, count: int = CANDLE_COUNT) -> list[dict] | None:
        """Fetch opt10080 5-minute candles."""
        result = self.api.request_opt10080(ticker, count)
        time.sleep(TR_SLEEP_SEC)
        return result

    def collect_candles_1d(self, ticker: str, count: int = CANDLE_COUNT) -> list[dict] | None:
        """Fetch opt10081 daily candles."""
        result = self.api.request_opt10081(ticker, count)
        time.sleep(TR_SLEEP_SEC)
        return result

    def collect_account(self) -> dict[str, Any] | None:
        """Fetch opw00004 account balance."""
        result = self.api.request_opw00004()
        time.sleep(TR_SLEEP_SEC)
        return result

    def _update_stock_data(
        self,
        ticker: str,
        name: str,
        basic: dict[str, Any] | None,
        candles_1m: list[dict] | None = None,
        candles_1d: list[dict] | None = None,
    ) -> None:
        """Merge new data into _data['stocks'][ticker]."""
        existing = self._data["stocks"].get(ticker, {})
        updated = {
            **existing,
            "ticker": ticker,
            "name": name,
            "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if basic is not None:
            updated["current_price"] = basic["current_price"]
            updated["change_rate"] = basic["change_rate"]
            updated["open"] = basic["open"]
            updated["high"] = basic["high"]
            updated["low"] = basic["low"]
            updated["volume"] = basic["volume"]
            updated["exec_strength"] = basic.get("exec_strength", 0.0)
            updated["prev_close"] = basic.get("prev_close", 0)
            updated["prev_volume"] = basic.get("prev_volume", 0)
            updated["trade_amount"] = basic.get("trade_amount", 0)
        if candles_1m is not None:
            updated["candles_1m"] = candles_1m
        if candles_1d is not None:
            updated["candles_1d"] = candles_1d
        self._data["stocks"][ticker] = updated

    def _update_account_data(self, account: dict[str, Any]) -> None:
        """Update account balance, preserving nonzero values."""
        prev = self._data["account"]
        # Only overwrite if new value is nonzero (0 = API glitch)
        if account.get("balance", 0) > 0:
            prev["balance"] = account["balance"]
        if account.get("total_eval", 0) > 0:
            prev["total_eval"] = account["total_eval"]
        if account.get("est_deposit", 0) > 0:
            prev["est_deposit"] = account["est_deposit"]

    def _sync_account_stocks(self) -> None:
        """opw00018 결과가 있으면 보존, 없으면 auto_positions로 폴백."""
        # opw00018에서 가져온 실제 보유종목이 있으면 덮어쓰지 않음
        existing = self._data["account"].get("stocks", [])
        if existing and any(isinstance(s, dict) and s.get("ticker") for s in existing):
            return

        # auto_positions.json 폴백
        positions = _read_json(AUTO_POSITIONS_PATH)
        if positions is None or not isinstance(positions, dict) or not positions:
            return

        account_stocks: list[dict[str, Any]] = []
        for ticker, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            stock_data = self._data["stocks"].get(ticker, {})
            current_price = stock_data.get("current_price", 0)
            buy_price = pos.get("avg_price", pos.get("buy_price", 0))
            quantity = pos.get("qty", pos.get("quantity", 0))
            eval_amount = current_price * quantity if current_price > 0 else 0
            profit_rate = (
                ((current_price - buy_price) / buy_price * 100)
                if buy_price > 0 and current_price > 0
                else 0.0
            )
            account_stocks.append({
                "ticker": ticker,
                "name": pos.get("name", stock_data.get("name", ticker)),
                "quantity": quantity,
                "avg_price": buy_price,
                "current_price": current_price,
                "eval_amount": eval_amount,
                "profit_rate": round(profit_rate, 2),
            })
        self._data["account"]["stocks"] = account_stocks

    # ----- Collection rounds -----

    def collect_primary(self) -> None:
        """Collect basic info + 5min candles for primary watch list."""
        for ticker, name in self._tickers.items():
            logger.debug("Collecting primary: %s (%s)", ticker, name)
            basic = self.collect_basic(ticker)
            candles = self.collect_candles_1m(ticker)
            self._update_stock_data(ticker, name, basic, candles_1m=candles)

    def collect_interest(self) -> None:
        """Collect basic info + 5min candles for interest list (fewer candles)."""
        for ticker, name in self._interest_tickers.items():
            logger.debug("Collecting interest: %s (%s)", ticker, name)
            basic = self.collect_basic(ticker)
            candles = self.collect_candles_1m(ticker, count=INTEREST_CANDLE_COUNT)
            self._update_stock_data(ticker, name, basic, candles_1m=candles)

    def collect_daily_candles(self) -> None:
        """Collect daily candles for all tickers."""
        all_tickers = {**self._tickers, **self._interest_tickers}
        for ticker, name in all_tickers.items():
            logger.debug("Collecting daily: %s (%s)", ticker, name)
            candles = self.collect_candles_1d(ticker)
            self._update_stock_data(ticker, name, None, candles_1d=candles)

    def collect_account_balance(self) -> None:
        """Collect account balance via opw00004 + holdings via opw00018."""
        account = self.collect_account()
        if account is not None:
            self._update_account_data(account)
        else:
            logger.warning("Account query returned None")

        # 보유종목 조회
        time.sleep(0.7)
        holdings = self.api.request_opw00018()
        if holdings is not None:
            self._data["account"]["stocks"] = holdings
            logger.info("Account holdings: %d stocks", len(holdings))
        else:
            logger.debug("opw00018 returned None")

    # ----- Order queue processing -----

    def process_order_queue(self) -> None:
        """Read pending orders from order_queue.json and execute via SendOrder."""
        self.api._queue_processing = True
        try:
            data = _read_json(ORDER_QUEUE_PATH)
            if data is None:
                return
            orders = data.get("orders", [])
            changed = False

            for order in orders:
                if order.get("status") != "pending":
                    continue

                ticker = order.get("ticker", "")
                order_type = order.get("order_type", 1)  # 1=buy, 2=sell
                quantity = order.get("quantity", 0)
                price = order.get("price", 0)
                price_type = order.get("price_type", "00")

                if not ticker or quantity <= 0:
                    order["status"] = "failed"
                    order["fail_reason"] = "invalid ticker or quantity"
                    order["failed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                    changed = True
                    continue

                # Mark as submitted before sending
                order["status"] = "submitted"
                order["submitted_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                changed = True

                ret = self.api.send_order(ticker, order_type, quantity, price, price_type)
                if ret != 0:
                    order["status"] = "failed"
                    order["fail_reason"] = f"SendOrder returned {ret}"
                    order["failed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

                time.sleep(TR_SLEEP_SEC)

            if changed:
                data["orders"] = orders
                _atomic_write_json(ORDER_QUEUE_PATH, data)

        finally:
            self.api._queue_processing = False

        # Drain buffered chejan events (always runs, outside try/finally)
        self._drain_chejan_buffers()

    def _drain_chejan_buffers(self) -> None:
        """Apply any chejan events that arrived during process_order_queue."""
        if not self.api._pending_fills and not self.api._pending_fails:
            return

        data = _read_json(ORDER_QUEUE_PATH)
        if data is None:
            data = {"orders": []}
        orders = data.get("orders", [])
        changed = False

        while self.api._pending_fills:
            fill = self.api._pending_fills.pop(0)
            if self.api._apply_fill_event(orders, fill):
                changed = True

        while self.api._pending_fails:
            fail = self.api._pending_fails.pop(0)
            for order in orders:
                if (
                    order.get("status") == "submitted"
                    and order.get("ticker") == fail["ticker"]
                ):
                    order["status"] = "failed"
                    order["fail_reason"] = fail.get("reason", "rejected")
                    order["failed_at"] = fail["timestamp"]
                    changed = True
                    break

        if changed:
            data["orders"] = orders
            _atomic_write_json(ORDER_QUEUE_PATH, data)

    # ----- Real-time registration -----

    def register_real_data(self) -> None:
        """감시종목 실시간 체결+호가 등록 (SetRealReg)."""
        tickers = list(self._tickers.keys())
        if not tickers:
            logger.warning("register_real_data: 감시종목 없음, 실시간 등록 건너뜀")
            return
        ticker_str = ";".join(tickers)
        # FID 목록: 10=현재가, 11=전일대비, 12=등락율, 13=누적거래량,
        #           15=거래량, 228=체결강도, 41=매도호가1, 51=매수호가1,
        #           61=매도잔량1, 71=매수잔량1, 42~45=매도호가2~5, 52~55=매수호가2~5,
        #           62~65=매도잔량2~5, 72~75=매수잔량2~5
        fid_list = (
            "10;11;12;13;15;228;"
            "41;51;61;71;"
            "42;52;62;72;"
            "43;53;63;73;"
            "44;54;64;74;"
            "45;55;65;75"
        )
        self.api.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            ["9001", ticker_str, fid_list, "0"],
        )
        logger.info("실시간 등록 완료: %d종목", len(tickers))

    # ----- Real-time data callback -----

    def _on_real_data(self, ticker: str, data: dict) -> None:
        """실시간 데이터 콜백 — _data['stocks']에 즉시 반영."""
        if ticker not in self._data["stocks"]:
            return

        stock = self._data["stocks"][ticker]

        if data.get("real_type") == "trade":
            if data.get("current_price", 0) > 0:
                stock["current_price"] = data["current_price"]
            if data.get("change_rate") is not None:
                stock["change_rate"] = data["change_rate"]
            if data.get("exec_strength", 0) > 0:
                stock["exec_strength"] = data["exec_strength"]

        elif data.get("real_type") == "orderbook":
            stock["orderbook"] = data.get("orderbook", {})

    # ----- Save -----

    def save(self) -> None:
        """Atomic write collected data to kiwoom_data.json."""
        self._sync_account_stocks()
        self._data["updated_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        _atomic_write_json(OUTPUT_PATH, self._data)
        logger.debug("Saved kiwoom_data.json")


# ---------------------------------------------------------------------------
# main + tick loop
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                str(ROOT / "logs" / "kiwoom_collector.log"),
                encoding="utf-8",
            ),
        ],
    )
    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    (ROOT / "data").mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)

    api = KiwoomAPI()
    if not api.login():
        logger.error("Login failed. Exiting.")
        sys.exit(1)

    collector = KiwoomCollector(api)

    tick_count = 0
    tick_running = False

    def _tick() -> None:
        nonlocal tick_count, tick_running

        # Re-entrancy guard
        if tick_running:
            logger.warning("_tick re-entrance blocked")
            return
        tick_running = True

        try:
            if not _is_market_open():
                logger.info("Market closed today, skipping tick")
                return
            if not _in_trading_hours():
                logger.debug("Outside trading hours, skipping tick")
                return

            tick_count += 1
            logger.info("=== TICK %d ===", tick_count)

            # 1. Primary watch: basic + 5min candles (every tick)
            collector.collect_primary()

            # 2. Interest stocks (every INTEREST_EVERY_N_TICKS)
            if tick_count % INTEREST_EVERY_N_TICKS == 0:
                collector.collect_interest()

            # 3. Account balance (every ACCOUNT_EVERY_N_TICKS)
            if tick_count % ACCOUNT_EVERY_N_TICKS == 0:
                collector.collect_account_balance()

            # 4. Daily candles (every DAILY_EVERY_N_TICKS)
            if tick_count % DAILY_EVERY_N_TICKS == 0:
                collector.collect_daily_candles()

            # 5. Process order queue
            collector.process_order_queue()

            # 6. Save
            collector.save()

            logger.info("=== TICK %d DONE ===", tick_count)

        except Exception:
            logger.exception("Error in _tick")
        finally:
            tick_running = False

    # First tick immediately
    _tick()

    # 첫 tick 완료 후 실시간 감시 등록 (_tickers가 채워진 상태)
    collector.register_real_data()

    # Timer for subsequent ticks
    timer = QTimer()
    timer.timeout.connect(_tick)
    timer.start(COLLECT_INTERVAL_SEC * 1000)

    logger.info(
        "Kiwoom collector started. Interval=%ds, Watch=%d, Interest=%d",
        COLLECT_INTERVAL_SEC,
        len(collector._tickers),
        len(collector._interest_tickers),
    )

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
