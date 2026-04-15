"""EOD 청산 vs 보유 비교 추적기.

당일 청산한 종목의 "만약 안 팔았으면?" 데이터를 기록.
다음날 시가와 비교하여 어떤 선택이 나았는지 분석.

data/eod_comparison.json에 영구 보관.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("stock_analysis")

ROOT = Path(__file__).parent.parent
EOD_COMPARISON_PATH = ROOT / "data" / "eod_comparison.json"


def _load() -> list[dict]:
    if not EOD_COMPARISON_PATH.exists():
        return []
    try:
        with open(EOD_COMPARISON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(data: list[dict]) -> None:
    import tempfile, os
    EOD_COMPARISON_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(EOD_COMPARISON_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(EOD_COMPARISON_PATH))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_eod_sell(ticker: str, name: str, qty: int,
                    sell_price: int, buy_price: int, pnl: int) -> None:
    """EOD 청산 시 호출. 다음날 비교용 데이터 기록."""
    records = _load()
    records.append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "ticker": ticker,
        "name": name,
        "qty": qty,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "eod_pnl": pnl,
        "next_day_open": None,      # 다음날 채워짐
        "next_day_close": None,     # 다음날 채워짐
        "hold_pnl": None,           # 다음날 계산
        "better_choice": None,      # "eod" or "hold"
    })
    _save(records)
    logger.info("[EOD비교] %s 기록: 청산가 %s, 손익 %+d", name, f"{sell_price:,}", pnl)


def fill_next_day_prices(kiwoom_data: dict) -> None:
    """다음날 아침 check_signals()에서 호출. 어제 EOD 기록에 오늘 시가/종가 채움."""
    records = _load()
    today = datetime.now().strftime("%Y-%m-%d")
    changed = False

    for rec in records:
        if rec.get("next_day_open") is not None:
            continue  # 이미 채워짐
        if rec["date"] == today:
            continue  # 오늘 청산한 건 내일 채움

        ticker = rec["ticker"]
        stock = kiwoom_data.get("stocks", {}).get(ticker, {})
        candles = stock.get("candles_1d", [])

        if not candles:
            continue

        # 오늘 시가/종가
        today_candle = None
        for c in reversed(candles):
            if c.get("date", "").replace("-", "")[:8] == today.replace("-", ""):
                today_candle = c
                break

        if today_candle is None:
            # 시가라도
            current_price = int(stock.get("current_price", 0))
            if current_price > 0:
                rec["next_day_open"] = current_price
                changed = True
            continue

        open_price = int(today_candle.get("open", 0))
        close_price = int(today_candle.get("close", 0))

        if open_price > 0:
            rec["next_day_open"] = open_price
            qty = rec["qty"]
            buy_price = rec["buy_price"]
            hold_pnl = (open_price - buy_price) * qty
            rec["hold_pnl"] = hold_pnl
            rec["better_choice"] = "eod" if rec["eod_pnl"] >= hold_pnl else "hold"
            changed = True
            logger.info(
                "[EOD비교] %s 결과: EOD %+d원 vs 보유 %+d원 → %s 승",
                rec["name"], rec["eod_pnl"], hold_pnl, rec["better_choice"],
            )

        if close_price > 0:
            rec["next_day_close"] = close_price
            changed = True

    if changed:
        _save(records)


def get_comparison_summary(days: int = 30) -> dict:
    """최근 N일간 EOD vs 보유 비교 요약."""
    records = _load()
    completed = [r for r in records if r.get("better_choice") is not None]

    if not completed:
        return {"total": 0, "eod_wins": 0, "hold_wins": 0, "eod_better_pct": 0}

    recent = completed[-days:] if len(completed) > days else completed
    eod_wins = sum(1 for r in recent if r["better_choice"] == "eod")
    hold_wins = sum(1 for r in recent if r["better_choice"] == "hold")
    total = len(recent)

    eod_total_pnl = sum(r["eod_pnl"] for r in recent)
    hold_total_pnl = sum(r.get("hold_pnl", 0) for r in recent)

    return {
        "total": total,
        "eod_wins": eod_wins,
        "hold_wins": hold_wins,
        "eod_better_pct": eod_wins / total * 100 if total > 0 else 0,
        "eod_total_pnl": eod_total_pnl,
        "hold_total_pnl": hold_total_pnl,
    }
