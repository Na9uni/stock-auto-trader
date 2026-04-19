"""텔레그램 주문 명령 서버 (polling 방식)"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
logger = logging.getLogger("stock_analysis")

# ------------------------------------------------------------------
# 설정 상수
# ------------------------------------------------------------------

ALLOWED_IDS: set[str] = {
    x.strip()
    for x in os.getenv("TELEGRAM_ALLOWED_IDS", "").split(",")
    if x.strip()
}
_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_BASE_URL = f"https://api.telegram.org/bot{_BOT_TOKEN}"

_DATA_DIR = ROOT / "data"
_KIWOOM_DATA = _DATA_DIR / "kiwoom_data.json"
_POSITIONS_DATA = _DATA_DIR / "auto_positions.json"
_ORDER_QUEUE = _DATA_DIR / "order_queue.json"
_INTEREST_DATA = _DATA_DIR / "interest_list.json"

CMD_FOOTER = "\n\n💡 /도움말 — 명령어 목록"

_POLLING_TIMEOUT = 30   # long-polling 대기 시간(초)
_POLLING_INTERVAL = 1   # 폴링 루프 간격(초)


def get_admin_id() -> str:
    return os.getenv("TELEGRAM_ADMIN_ID", "")


# ------------------------------------------------------------------
# 진입점
# ------------------------------------------------------------------

def start_telegram_commander() -> None:
    """별도 데몬 스레드에서 텔레그램 polling 시작.

    .env의 TELEGRAM_COMMANDER_ENABLED=false면 실행 안 함 (서브 PC용).
    같은 봇을 두 PC에서 polling하면 409 Conflict 에러가 반복 발생하므로,
    메인 PC에서만 commander를 돌리고 서브는 알림 발송만 담당한다.
    (아들 PC: true, 아빠 PC: false 권장.)
    """
    if not _BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN 미설정 — 텔레그램 commander 비활성화")
        return
    # master 강건 체크 채택 — false/0/no/off 모두 비활성으로 인식
    enabled = os.getenv("TELEGRAM_COMMANDER_ENABLED", "true").strip().lower()
    if enabled in ("false", "0", "no", "off"):
        logger.info("TELEGRAM_COMMANDER_ENABLED=false — 텔레그램 commander 비활성화 (서브 PC 모드, 알림 송신은 유지)")
        return
    t = threading.Thread(target=_polling_loop, daemon=True, name="TelegramCommander")
    t.start()
    logger.info("텔레그램 commander 스레드 시작")


# ------------------------------------------------------------------
# Polling loop
# ------------------------------------------------------------------

def _polling_loop() -> None:
    """getUpdates long-polling. offset 기반으로 중복 처리 방지."""
    offset: int | None = None

    while True:
        try:
            updates = _get_updates(offset)
            for update in updates:
                update_id: int = update["update_id"]
                offset = update_id + 1
                _process_update(update)
        except Exception as exc:
            logger.error("polling_loop 예외: %s", exc)
            time.sleep(_POLLING_INTERVAL * 5)
        else:
            time.sleep(_POLLING_INTERVAL)


_last_error_code: int | None = None  # 반복 에러 스팸 억제용


def _get_updates(offset: int | None) -> list[dict]:
    """텔레그램 getUpdates 호출.

    같은 에러 코드가 연속 발생하면 1회만 로깅 (예: 409 Conflict — 봇이
    다른 인스턴스에서도 돌아갈 때 발생, 스팸 방지).
    """
    global _last_error_code
    params: dict[str, Any] = {"timeout": _POLLING_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(
            f"{_BASE_URL}/getUpdates",
            params=params,
            timeout=_POLLING_TIMEOUT + 5,
        )
        data = resp.json()
        if data.get("ok"):
            if _last_error_code is not None:
                logger.info("getUpdates 정상 복귀 (이전 에러 %s 해소)", _last_error_code)
                _last_error_code = None
            return data.get("result", [])
        err_code = data.get("error_code")
        if err_code != _last_error_code:
            logger.warning("getUpdates 실패: %s", data)
            _last_error_code = err_code
        # 409 Conflict(다른 인스턴스와 봇 공유)는 긴 대기로 폴링 빈도 낮춤
        if err_code == 409:
            time.sleep(30)
    except requests.RequestException as exc:
        if _last_error_code != -1:
            logger.error("getUpdates 예외: %s", exc)
            _last_error_code = -1
    return []


def _process_update(update: dict) -> None:
    """단일 update에서 chat_id·text 추출 → 권한 체크 → 명령 처리."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return

    # 허용된 사용자만 처리
    if chat_id not in ALLOWED_IDS:
        logger.warning("미허용 사용자 접근 시도: chat_id=%s text=%s", chat_id, text[:50])
        return

    parts = text.split()
    cmd = parts[0].lower() if parts else ""

    try:
        if cmd == "/상태":
            _handle_status(chat_id)
        elif cmd == "/잔고":
            _handle_balance(chat_id)
        elif cmd == "/종목" and len(parts) >= 2:
            _handle_stock(chat_id, parts[1])
        elif cmd == "/매수" and len(parts) >= 3:
            _handle_buy(chat_id, parts[1], parts[2])
        elif cmd == "/매도" and len(parts) >= 3:
            _handle_sell(chat_id, parts[1], parts[2])
        elif cmd == "/관심추가" and len(parts) >= 3:
            _handle_interest_add(chat_id, parts[1], parts[2])
        elif cmd == "/관심삭제" and len(parts) >= 2:
            _handle_interest_remove(chat_id, parts[1])
        elif cmd == "/관심목록":
            _handle_interest_list(chat_id)
        elif cmd == "/손절" and len(parts) >= 3:
            _handle_stop_loss(chat_id, parts[1], parts[2])
        elif cmd == "/목표" and len(parts) >= 3:
            _handle_target_price(chat_id, parts[1], parts[2])
        elif cmd == "/뉴스":
            _handle_news(chat_id)
        elif cmd == "/도움말":
            _handle_help(chat_id)
        else:
            _send(chat_id, f"❓ 알 수 없는 명령어: <code>{text[:50]}</code>" + CMD_FOOTER)
    except Exception as exc:
        logger.error("명령 처리 예외 chat_id=%s cmd=%s error=%s", chat_id, cmd, exc)
        _send(chat_id, "⚠️ 명령 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")


# ------------------------------------------------------------------
# 명령 핸들러
# ------------------------------------------------------------------

def _handle_status(chat_id: str) -> None:
    """/상태 — 시스템 상태 요약."""
    data = _read_json(_KIWOOM_DATA)
    if not data:
        _send(chat_id, "⚠️ 시스템 데이터를 읽을 수 없습니다." + CMD_FOOTER)
        return

    mode = data.get("operation_mode", "UNKNOWN")
    account = data.get("account", {})
    balance = account.get("balance", 0)
    total_eval = account.get("total_eval", 0)

    positions = _read_json(_POSITIONS_DATA) or {}
    pos_count = len([v for v in positions.values() if isinstance(v, dict)])

    stocks = data.get("stocks", {})
    watch_count = len(stocks)

    msg = (
        "📊 <b>시스템 상태</b>\n"
        f"• 운영 모드: <b>{mode}</b>\n"
        f"• 보유 포지션: {pos_count}개\n"
        f"• 모니터링 종목: {watch_count}개\n"
        f"• 예수금: {balance:,.0f}원\n"
        f"• 총평가금액: {total_eval:,.0f}원"
    )
    _send(chat_id, msg + CMD_FOOTER)


def _handle_balance(chat_id: str) -> None:
    """/잔고 — 계좌 잔고 + 보유 포지션 상세."""
    data = _read_json(_KIWOOM_DATA)
    positions = _read_json(_POSITIONS_DATA) or {}

    if not data:
        _send(chat_id, "⚠️ 시스템 데이터를 읽을 수 없습니다." + CMD_FOOTER)
        return

    account = data.get("account", {})
    balance = account.get("balance", 0)
    total_eval = account.get("total_eval", 0)
    est_deposit = account.get("est_deposit", 0)
    stocks = data.get("stocks", {})

    lines = [
        "💰 <b>계좌 잔고</b>",
        f"• 예수금: {balance:,.0f}원",
        f"• 총평가금액: {total_eval:,.0f}원",
        f"• 추정예탁자산: {est_deposit:,.0f}원",
    ]

    # 보유 포지션
    pos_items = [(k, v) for k, v in positions.items() if isinstance(v, dict)]
    if pos_items:
        lines.append("\n📋 <b>보유 포지션</b>")
        for ticker, pos in pos_items:
            qty = pos.get("qty", 0)
            buy_price = pos.get("buy_price", 0)
            # kiwoom_data에서 현재가 매칭
            stock_info = stocks.get(ticker, {})
            cur_price = stock_info.get("current_price", 0) or buy_price
            name = stock_info.get("name", ticker)

            if buy_price > 0 and cur_price > 0:
                pnl_pct = (cur_price - buy_price) / buy_price * 100
                pnl_amt = (cur_price - buy_price) * qty
                sign = "+" if pnl_pct >= 0 else ""
                lines.append(
                    f"  [{ticker}] {name}\n"
                    f"    수량: {qty}주 | 평균가: {buy_price:,.0f}원\n"
                    f"    현재가: {cur_price:,.0f}원 | 손익: {sign}{pnl_pct:.2f}% ({sign}{pnl_amt:,.0f}원)"
                )
            else:
                lines.append(f"  [{ticker}] {name} | 수량: {qty}주 | 평균가: {buy_price:,.0f}원")
    else:
        lines.append("\n보유 포지션 없음")

    _send(chat_id, "\n".join(lines) + CMD_FOOTER)


def _handle_stock(chat_id: str, ticker: str) -> None:
    """/종목 {코드} — 특정 종목 현재가 + 지표 요약."""
    ticker = ticker.strip().upper()
    data = _read_json(_KIWOOM_DATA)
    if not data:
        _send(chat_id, "⚠️ 시스템 데이터를 읽을 수 없습니다." + CMD_FOOTER)
        return

    stocks = data.get("stocks", {})
    stock = stocks.get(ticker)
    if not stock:
        _send(chat_id, f"❌ [{ticker}] 종목을 찾을 수 없습니다." + CMD_FOOTER)
        return

    name = stock.get("name", ticker)
    cur_price = stock.get("current_price", 0)
    change_rate = stock.get("change_rate", 0.0)
    volume = stock.get("volume", 0)
    rsi = stock.get("rsi", None)
    signal = stock.get("signal", "NEUTRAL")
    score = stock.get("signal_score", 0)

    sign = "+" if change_rate >= 0 else ""
    rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"

    msg = (
        f"📈 <b>[{ticker}] {name}</b>\n"
        f"• 현재가: {cur_price:,.0f}원 ({sign}{change_rate:.2f}%)\n"
        f"• 거래량: {volume:,}\n"
        f"• RSI: {rsi_str}\n"
        f"• 신호: {signal} (점수: {score})"
    )
    _send(chat_id, msg + CMD_FOOTER)


def _handle_buy(chat_id: str, ticker: str, qty_str: str) -> None:
    """/매수 {코드} {수량} — 수동 매수 주문."""
    ticker = ticker.strip().upper()
    if not qty_str.isdigit() or int(qty_str) <= 0:
        _send(chat_id, "❌ 수량은 양의 정수여야 합니다. 예: /매수 005930 10" + CMD_FOOTER)
        return

    qty = int(qty_str)
    order = {
        "side": "buy",
        "ticker": ticker,
        "qty": qty,
        "source": "telegram_manual",
        "chat_id": chat_id,
    }
    ok = _enqueue_order(order)
    if ok:
        _send(chat_id, f"✅ 매수 주문 접수: [{ticker}] {qty}주" + CMD_FOOTER)
    else:
        _send(chat_id, f"❌ 매수 주문 접수 실패. 주문 큐를 확인해 주세요." + CMD_FOOTER)


def _handle_sell(chat_id: str, ticker: str, qty_str: str) -> None:
    """/매도 {코드} {수량} — 수동 매도 주문."""
    ticker = ticker.strip().upper()
    if not qty_str.isdigit() or int(qty_str) <= 0:
        _send(chat_id, "❌ 수량은 양의 정수여야 합니다. 예: /매도 005930 10" + CMD_FOOTER)
        return

    qty = int(qty_str)
    order = {
        "side": "sell",
        "ticker": ticker,
        "qty": qty,
        "source": "telegram_manual",
        "chat_id": chat_id,
    }
    ok = _enqueue_order(order)
    if ok:
        _send(chat_id, f"✅ 매도 주문 접수: [{ticker}] {qty}주" + CMD_FOOTER)
    else:
        _send(chat_id, f"❌ 매도 주문 접수 실패. 주문 큐를 확인해 주세요." + CMD_FOOTER)


def _handle_interest_add(chat_id: str, ticker: str, name: str) -> None:
    """/관심추가 {코드} {이름} — 관심종목 추가."""
    ticker = ticker.strip().upper()
    name = name.strip()

    interests = _read_json(_INTEREST_DATA) or {}
    if ticker in interests:
        _send(chat_id, f"ℹ️ [{ticker}] {name}은(는) 이미 관심종목입니다." + CMD_FOOTER)
        return

    updated = {**interests, ticker: {"name": name, "ticker": ticker}}
    ok = _write_json(_INTEREST_DATA, updated)
    if ok:
        _send(chat_id, f"✅ 관심종목 추가: [{ticker}] {name}" + CMD_FOOTER)
    else:
        _send(chat_id, "❌ 관심종목 저장 실패." + CMD_FOOTER)


def _handle_interest_remove(chat_id: str, ticker: str) -> None:
    """/관심삭제 {코드} — 관심종목 삭제."""
    ticker = ticker.strip().upper()
    interests = _read_json(_INTEREST_DATA) or {}

    if ticker not in interests:
        _send(chat_id, f"❌ [{ticker}]은(는) 관심종목에 없습니다." + CMD_FOOTER)
        return

    updated = {k: v for k, v in interests.items() if k != ticker}
    ok = _write_json(_INTEREST_DATA, updated)
    if ok:
        _send(chat_id, f"✅ 관심종목 삭제: [{ticker}]" + CMD_FOOTER)
    else:
        _send(chat_id, "❌ 관심종목 저장 실패." + CMD_FOOTER)


def _handle_interest_list(chat_id: str) -> None:
    """/관심목록 — 관심종목 리스트."""
    interests = _read_json(_INTEREST_DATA) or {}
    if not interests:
        _send(chat_id, "관심종목이 없습니다." + CMD_FOOTER)
        return

    lines = ["⭐ <b>관심종목 목록</b>"]
    for ticker, info in interests.items():
        iname = info.get("name", ticker) if isinstance(info, dict) else str(info)
        lines.append(f"  • [{ticker}] {iname}")

    _send(chat_id, "\n".join(lines) + CMD_FOOTER)


def _handle_stop_loss(chat_id: str, ticker: str, price_str: str) -> None:
    """/손절 {코드} {가격} — 손절가 설정."""
    ticker = ticker.strip().upper()
    try:
        price = float(price_str.replace(",", ""))
        if price <= 0:
            raise ValueError
    except ValueError:
        _send(chat_id, "❌ 가격은 양수 숫자여야 합니다. 예: /손절 005930 60000" + CMD_FOOTER)
        return

    ok = _set_price_target(ticker, "stop_loss", price)
    if ok:
        _send(chat_id, f"✅ [{ticker}] 손절가 설정: {price:,.0f}원" + CMD_FOOTER)
    else:
        _send(chat_id, "❌ 손절가 설정 실패." + CMD_FOOTER)


def _handle_target_price(chat_id: str, ticker: str, price_str: str) -> None:
    """/목표 {코드} {가격} — 목표가 설정."""
    ticker = ticker.strip().upper()
    try:
        price = float(price_str.replace(",", ""))
        if price <= 0:
            raise ValueError
    except ValueError:
        _send(chat_id, "❌ 가격은 양수 숫자여야 합니다. 예: /목표 005930 80000" + CMD_FOOTER)
        return

    ok = _set_price_target(ticker, "target_price", price)
    if ok:
        _send(chat_id, f"✅ [{ticker}] 목표가 설정: {price:,.0f}원" + CMD_FOOTER)
    else:
        _send(chat_id, "❌ 목표가 설정 실패." + CMD_FOOTER)


def _handle_news(chat_id: str) -> None:
    """/뉴스 — 최근 뉴스 (kiwoom_data.json의 news 필드)."""
    data = _read_json(_KIWOOM_DATA)
    if not data:
        _send(chat_id, "⚠️ 시스템 데이터를 읽을 수 없습니다." + CMD_FOOTER)
        return

    news_list = data.get("news", [])
    if not news_list:
        _send(chat_id, "최근 뉴스가 없습니다." + CMD_FOOTER)
        return

    lines = ["📰 <b>최근 뉴스</b>"]
    for item in news_list[:10]:
        if isinstance(item, dict):
            title = item.get("title", "")
            time_str = item.get("time", "")
            lines.append(f"  • {time_str} {title}")
        else:
            lines.append(f"  • {item}")

    _send(chat_id, "\n".join(lines) + CMD_FOOTER)


def _handle_help(chat_id: str) -> None:
    """/도움말 — 명령어 목록."""
    msg = (
        "📖 <b>명령어 목록</b>\n\n"
        "/상태 — 시스템 상태 (운영모드, 보유종목, 예수금)\n"
        "/잔고 — 계좌 잔고 + 보유 포지션 상세\n"
        "/종목 {코드} — 특정 종목 현재가 + 지표\n"
        "/매수 {코드} {수량} — 수동 매수 주문\n"
        "/매도 {코드} {수량} — 수동 매도 주문\n"
        "/관심추가 {코드} {이름} — 관심종목 추가\n"
        "/관심삭제 {코드} — 관심종목 삭제\n"
        "/관심목록 — 관심종목 리스트\n"
        "/손절 {코드} {가격} — 손절가 설정\n"
        "/목표 {코드} {가격} — 목표가 설정\n"
        "/뉴스 — 최근 뉴스\n"
        "/도움말 — 이 도움말"
    )
    _send(chat_id, msg)


# ------------------------------------------------------------------
# 내부 유틸
# ------------------------------------------------------------------

def _send(chat_id: str, text: str) -> bool:
    """텔레그램 메시지 발송. 실패 시 False 반환."""
    url = f"{_BASE_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        logger.error("_send 실패 chat_id=%s status=%s", chat_id, resp.status_code)
        return False
    except requests.RequestException as exc:
        logger.error("_send 예외 chat_id=%s error=%s", chat_id, exc)
        return False


def _read_json(path: Path) -> dict | None:
    """JSON 파일 읽기. 실패 시 None 반환."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.debug("파일 없음: %s", path)
        return None
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("JSON 읽기 실패 %s: %s", path, exc)
        return None


def _write_json(path: Path, data: dict) -> bool:
    """JSON 파일 쓰기. 실패 시 False 반환."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except OSError as exc:
        logger.error("JSON 쓰기 실패 %s: %s", path, exc)
        return False


def _enqueue_order(order: dict) -> bool:
    """order_queue.json에 주문 추가. KiwoomOrderQueue 호환 형식."""
    try:
        # KiwoomOrderQueue가 실행 중이면 order_queue.json을 통해 IPC
        queue = _read_json(_ORDER_QUEUE)
        if queue is None:
            queue = {"orders": []}

        orders: list = queue.get("orders", [])
        # kiwoom_collector 호환 필드 추가
        import uuid
        from datetime import datetime as _dt
        enriched_order = {
            **order,
            "id": str(uuid.uuid4())[:8],
            "status": "pending",
            "quantity": order.get("qty", 0),
            "price": 0,
            "order_type": 1 if order.get("side") == "buy" else 2,
            "price_type": "03",
            "rule_name": "텔레그램_수동",
            "mock_mode": False,
            "created_at": _dt.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "submitted_at": None,
            "executed_at": None,
            "exec_price": None,
            "order_number": None,
            "cumul_exec_qty": 0,
            "fail_reason": None,
        }
        updated_queue = {**queue, "orders": [*orders, enriched_order]}
        return _write_json(_ORDER_QUEUE, updated_queue)
    except Exception as exc:
        logger.error("_enqueue_order 예외: %s", exc)
        return False


def _set_price_target(ticker: str, field: str, price: float) -> bool:
    """auto_positions.json의 특정 종목에 손절가/목표가 설정."""
    try:
        positions = _read_json(_POSITIONS_DATA)
        if positions is None:
            positions = {}

        pos = positions.get(ticker, {})
        if not isinstance(pos, dict):
            pos = {}

        updated_pos = {**pos, field: price}
        updated_positions = {**positions, ticker: updated_pos}
        return _write_json(_POSITIONS_DATA, updated_positions)
    except Exception as exc:
        logger.error("_set_price_target 예외 ticker=%s field=%s: %s", ticker, field, exc)
        return False
