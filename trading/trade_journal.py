"""매매 일지 — 모든 거래를 영구 기록.

CSV 파일 (data/trade_journal.csv)에 매수/매도 기록을 자동 저장.
7일 후 삭제되는 order_queue.json과 달리 영구 보존.
매매 패턴 분석, 성과 추적, 전략 개선에 활용.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("stock_analysis")

ROOT = Path(__file__).parent.parent
JOURNAL_PATH = ROOT / "data" / "trade_journal.csv"

# CSV 컬럼 정의
COLUMNS = [
    "datetime",      # 거래 시각
    "ticker",        # 종목코드
    "name",          # 종목명
    "side",          # buy / sell
    "quantity",      # 수량
    "price",         # 체결가
    "amount",        # 거래금액 (price x quantity)
    "buy_price",     # 매수가 (매도 시 원래 매수가)
    "pnl",           # 손익 (매도 시)
    "pnl_pct",       # 손익률 (매도 시)
    "hold_time",     # 보유 시간 (매도 시, 분 단위)
    "reason",        # 사유 (VB돌파, 트레일링, 손절, EOD청산 등)
    "strategy",      # 전략 (auto, vb, trend, crisis_mr)
    "regime",        # 레짐 (NORMAL, SWING, DEFENSE, CASH)
    "mock",          # 모의투자 여부
    "exec_strength", # 체결강도
    "ai_decision",   # AI 판단
]


def _ensure_header() -> None:
    """CSV 파일이 없으면 헤더 생성."""
    if JOURNAL_PATH.exists():
        return
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
    logger.info("[매매일지] 새 파일 생성: %s", JOURNAL_PATH)


def record_trade(
    ticker: str,
    name: str,
    side: str,           # "buy" or "sell"
    quantity: int,
    price: int,
    reason: str = "",
    strategy: str = "",
    mock: bool = True,
    buy_price: int = 0,
    buy_time: str = "",
    pnl: int = 0,
    exec_strength: float = 0.0,
    ai_decision: str = "",
) -> None:
    """매매 기록 추가."""
    _ensure_header()

    now = datetime.now()
    amount = price * quantity

    # 손익률 계산 (매도 시)
    pnl_pct = 0.0
    if side == "sell" and buy_price > 0:
        pnl_pct = (price - buy_price) / buy_price * 100

    # 보유 시간 (매도 시, 분 단위)
    hold_time = 0
    if side == "sell" and buy_time:
        try:
            bt = datetime.strptime(buy_time, "%Y-%m-%d %H:%M:%S")
            hold_time = int((now - bt).total_seconds() / 60)
        except (ValueError, TypeError):
            pass

    # 레짐 가져오기
    regime = ""
    try:
        from strategies.regime_engine import get_regime_engine
        regime = get_regime_engine().state.value
    except Exception:
        pass

    row = [
        now.strftime("%Y-%m-%d %H:%M:%S"),
        ticker,
        name,
        side,
        quantity,
        price,
        amount,
        buy_price if side == "sell" else "",
        pnl if side == "sell" else "",
        f"{pnl_pct:.2f}" if side == "sell" else "",
        hold_time if side == "sell" else "",
        reason,
        strategy,
        regime,
        "Y" if mock else "N",
        f"{exec_strength:.0f}" if exec_strength > 0 else "",
        ai_decision,
    ]

    try:
        with open(JOURNAL_PATH, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(row)
        logger.info(
            "[매매일지] %s %s %s %d주 @%s %s",
            "BUY" if side == "buy" else "SELL",
            name, side, quantity, f"{price:,}",
            f"손익 {pnl:+,}" if side == "sell" else "",
        )
    except Exception as e:
        logger.error("[매매일지] 기록 실패: %s", e)


def get_daily_summary(date_str: str = "") -> dict:
    """일일 매매 요약. date_str 미지정 시 오늘."""
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    if not JOURNAL_PATH.exists():
        return {"trades": 0, "buys": 0, "sells": 0, "pnl": 0, "win_rate": 0}

    buys = 0
    sells = 0
    total_pnl = 0
    wins = 0

    with open(JOURNAL_PATH, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("datetime", "").startswith(date_str):
                continue
            if row["side"] == "buy":
                buys += 1
            elif row["side"] == "sell":
                sells += 1
                try:
                    pnl = int(float(row.get("pnl", 0) or 0))
                    total_pnl += pnl
                    if pnl > 0:
                        wins += 1
                except (ValueError, TypeError):
                    pass

    return {
        "trades": buys + sells,
        "buys": buys,
        "sells": sells,
        "pnl": total_pnl,
        "win_rate": (wins / sells * 100) if sells > 0 else 0,
    }


def get_cumulative_summary() -> dict:
    """전체 누적 매매 요약."""
    if not JOURNAL_PATH.exists():
        return {"total_trades": 0, "total_pnl": 0, "win_rate": 0, "best_trade": 0, "worst_trade": 0}

    total_sells = 0
    total_pnl = 0
    wins = 0
    best = 0
    worst = 0

    with open(JOURNAL_PATH, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["side"] == "sell":
                total_sells += 1
                try:
                    pnl = int(float(row.get("pnl", 0) or 0))
                    total_pnl += pnl
                    if pnl > 0:
                        wins += 1
                    if pnl > best:
                        best = pnl
                    if pnl < worst:
                        worst = pnl
                except (ValueError, TypeError):
                    pass

    return {
        "total_trades": total_sells,
        "total_pnl": total_pnl,
        "win_rate": (wins / total_sells * 100) if total_sells > 0 else 0,
        "best_trade": best,
        "worst_trade": worst,
    }
