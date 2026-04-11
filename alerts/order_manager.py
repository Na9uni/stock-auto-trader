"""주문 상태 관리 — 체결/실패 감지, 포지션 반영, 큐 정리.

Functions:
    check_order_status  : 1분마다 order_queue.json 체결/실패 → 알림 + 포지션 반영
    _prune_order_queue  : 06:00 7일 초과 완료 주문 정리
    _parse_dt           : datetime 파싱 유틸
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from strategies.base import SignalStrength
from alerts.telegram_notifier import TelegramNotifier

from alerts._state import (
    _notified_orders,
    _order_init_done,
    logger,
)

from alerts.file_io import (
    ORDER_QUEUE_PATH,
    load_auto_positions,
    save_auto_positions,
)

from alerts.market_guard import (
    record_loss_and_stoploss,
    reset_consec_stoploss,
)

from alerts.trade_executor import (
    _buy_in_progress,
)

from alerts.position_manager import (
    _sell_fail_count,
)

from alerts.notifications import (
    CMD_FOOTER,
    get_admin_id,
)


# ---------------------------------------------------------------------------
# 주문 상태 체크
# ---------------------------------------------------------------------------

def check_order_status() -> None:
    """1분마다: order_queue.json 체결/실패 감지 → 알림."""
    import alerts._state as _st
    # _order_init_done 은 모듈-레벨 bool이므로 mutable 참조를 통해 갱신
    # _notified_orders 는 set이므로 직접 변경 가능

    try:
        with open(ORDER_QUEUE_PATH, "r", encoding="utf-8") as f:
            queue = json.load(f)
    except Exception:
        return

    orders = queue.get("orders", [])
    if not orders:
        return

    # 첫 실행: 시딩 (재시작 중복 알림 방지)
    if not _st._order_init_done:
        for order in orders:
            if order.get("status") in ("executed", "failed"):
                _notified_orders.add(order.get("id", ""))
        _st._order_init_done = True

        # 재시작 시 zombie selling + 분할익절 포지션 정리
        positions = load_auto_positions()
        if positions:
            pos_changed = False
            # 분할익절 체결/실패 정리
            for order in orders:
                if order.get("side") != "sell":
                    continue
                if order.get("rule_name") != "자동청산_분할익절":
                    continue
                ticker = order.get("ticker", "")
                if ticker not in positions:
                    continue
                p_sell_oid = positions[ticker].get("partial_sell_order_id", "")
                if p_sell_oid and p_sell_oid == order.get("id", ""):
                    if order.get("status") == "executed":
                        partial_qty = positions[ticker].pop("partial_sell_qty", 0)
                        positions[ticker].pop("partial_sell_order_id", None)
                        if partial_qty > 0:
                            positions[ticker]["qty"] = max(1, int(positions[ticker].get("qty", 0)) - partial_qty)
                        pos_changed = True
                        logger.info("[재시작 정리] %s 분할익절 체결 → qty 조정", ticker)
                    elif order.get("status") == "failed":
                        positions[ticker].pop("partial_sell_qty", None)
                        positions[ticker].pop("partial_sell_order_id", None)
                        pos_changed = True

            # 매도 체결 buy_price 백필 (일일 손실 집계용) + 디스크 저장
            _backfilled = False
            for order in orders:
                if order.get("status") != "executed" or order.get("side") != "sell":
                    continue
                if "buy_price" not in order:
                    ticker = order.get("ticker", "")
                    if ticker in positions:
                        order["buy_price"] = positions[ticker].get("buy_price", 0)
                        _backfilled = True
            if _backfilled:
                try:
                    _oq_tmp = Path(ORDER_QUEUE_PATH).with_suffix(".tmp")
                    with open(_oq_tmp, "w", encoding="utf-8") as f:
                        json.dump(queue, f, ensure_ascii=False, indent=2)
                    _oq_tmp.replace(ORDER_QUEUE_PATH)
                except Exception as e:
                    logger.error("[재시작] buy_price 백필 저장 실패: %s", e)

            # zombie selling 포지션 정리
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

        # 분할 익절 체결/실패 → qty 조정 (포지션 삭제 안 함)
        if side == "sell" and rule_name == "자동청산_분할익절":
            positions = load_auto_positions()
            if ticker in positions:
                p_sell_oid = positions[ticker].get("partial_sell_order_id", "")
                if p_sell_oid and p_sell_oid == order_id:
                    if status == "executed":
                        partial_qty = positions[ticker].pop("partial_sell_qty", 0)
                        positions[ticker].pop("partial_sell_order_id", None)
                        if partial_qty > 0:
                            positions[ticker]["qty"] = max(1, int(positions[ticker].get("qty", 0)) - partial_qty)
                        save_auto_positions(positions)
                        logger.info("[분할 익절 체결] %s %d주 확정", name, partial_qty)
                    elif status == "failed":
                        positions[ticker].pop("partial_sell_qty", None)
                        positions[ticker].pop("partial_sell_order_id", None)
                        save_auto_positions(positions)
                        logger.warning("[분할 익절 실패] %s qty 원복 (변경 없음)", name)
            continue  # 분할 익절은 아래 포지션 삭제 로직으로 가면 안 됨

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
                    # order에 buy_price 기록 (일일 손실 집계용)
                    order["buy_price"] = _bp
                    try:
                        with open(ORDER_QUEUE_PATH, "r", encoding="utf-8") as f:
                            _oq = json.load(f)
                        for _o in _oq.get("orders", []):
                            if _o.get("id") == order_id:
                                _o["buy_price"] = _bp
                                break
                        _oq_tmp = Path(ORDER_QUEUE_PATH).with_suffix(".tmp")
                        with open(_oq_tmp, "w", encoding="utf-8") as f:
                            json.dump(_oq, f, ensure_ascii=False, indent=2)
                        _oq_tmp.replace(ORDER_QUEUE_PATH)
                    except Exception as e:
                        logger.error("[buy_price 기록] order_queue 저장 실패: %s", e)
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
                    "rule_name": rule_name,
                }
                save_auto_positions(positions)
                logger.info("[포지션 추가] %s %d주 @%s", name, quantity, f"{_ep:,}")

        # 매수 실패 → _buy_in_progress 정리
        if status == "failed" and side == "buy":
            _buy_in_progress.discard(ticker)


# ---------------------------------------------------------------------------
# 주문 큐 정리
# ---------------------------------------------------------------------------

def _prune_order_queue() -> None:
    """06:00: 7일 초과 완료 주문 정리."""
    try:
        with open(ORDER_QUEUE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

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
