"""알림 — 텔레그램 뉴스/시장/리포트 전송, 신호 헤더, 사용자 관리."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import pandas as pd

from alerts.file_io import (
    ROOT,
    load_auto_positions,
    load_monthly_loss,
)
from alerts.signal_detector import SignalType, SignalStrength, SignalResult
from alerts.telegram_notifier import TelegramNotifier

logger = logging.getLogger("stock_analysis")

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

CMD_FOOTER = "\n\n💡 /도움말 — 명령어 목록"

_LAST_SIGNAL_PATH = ROOT / "data" / "last_signal.json"


# ---------------------------------------------------------------------------
# 설정 (analysis_scheduler에서 주입)
# ---------------------------------------------------------------------------

OPERATION_MODE: str = "PAPER"
AUTO_TRADE_ENABLED: bool = False
MAX_SLOTS: int = 0


def _configure(operation_mode: str, auto_trade_enabled: bool, max_slots: int) -> None:
    """analysis_scheduler에서 호출하여 설정값 주입."""
    global OPERATION_MODE, AUTO_TRADE_ENABLED, MAX_SLOTS
    OPERATION_MODE = operation_mode
    AUTO_TRADE_ENABLED = auto_trade_enabled
    MAX_SLOTS = max_slots


# ---------------------------------------------------------------------------
# 관리자 + 사용자
# ---------------------------------------------------------------------------

def get_admin_id() -> str:
    """텔레그램 관리자 ID."""
    return os.getenv("TELEGRAM_ADMIN_ID", "")


def get_users_for_ticker(ticker: str) -> list[str]:
    """특정 종목을 관심 등록한 유저 chat_id 리스트."""
    from data.users_manager import get_users_for_ticker as _get_users
    return _get_users(ticker)


# ---------------------------------------------------------------------------
# 마지막 신호 저장
# ---------------------------------------------------------------------------

def save_last_signal(ticker: str, name: str) -> None:
    """data/last_signal.json 갱신 — 종목별 마지막 신호 시각 기록."""
    try:
        if _LAST_SIGNAL_PATH.exists():
            with open(_LAST_SIGNAL_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
    except (json.JSONDecodeError, OSError):
        data = {}

    data[ticker] = {
        "name": name,
        "time": datetime.now().isoformat(),
    }

    tmp = _LAST_SIGNAL_PATH.with_suffix(".tmp")
    try:
        _LAST_SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(_LAST_SIGNAL_PATH)
    except OSError as exc:
        logger.error("last_signal.json 저장 실패: %s", exc)
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 신호 헤더 텍스트
# ---------------------------------------------------------------------------

def build_signal_header(
    ticker: str,
    name: str,
    signal: SignalResult,
    stock: dict,
) -> str:
    """신호 알림 헤더 텍스트 생성.

    Args:
        ticker: 종목코드
        name: 종목명
        signal: SignalResult 객체
        stock: kiwoom_data의 종목 데이터

    Returns:
        포맷된 헤더 문자열
    """
    # 신호 방향 이모지
    if signal.signal_type == SignalType.BUY:
        direction = "🟢 매수"
    elif signal.signal_type == SignalType.SELL:
        direction = "🔴 매도"
    else:
        direction = "⚪ 중립"

    # 강도 텍스트
    strength_map = {
        SignalStrength.STRONG: "강력",
        SignalStrength.MEDIUM: "보통",
        SignalStrength.WEAK: "약함",
    }
    strength_text = strength_map.get(signal.strength, "")

    # 현재가
    current_price = stock.get("current_price", 0)
    change_pct = float(stock.get("change_rate", 0.0))
    change_sign = "+" if change_pct >= 0 else ""

    header_lines = [
        f"{direction} 신호 [{strength_text}]",
        f"📊 {name} ({ticker})",
        f"💰 {current_price:,}원 ({change_sign}{change_pct:.2f}%)",
        f"📈 점수: {signal.score}점",
    ]

    # RSI
    if not pd.isna(signal.rsi):
        header_lines.append(f"RSI: {signal.rsi:.1f}")

    # MACD 크로스
    if signal.macd_cross:
        cross_text = "골든크로스" if signal.macd_cross == "golden" else "데드크로스"
        header_lines.append(f"MACD: {cross_text}")

    # 거래량 비율
    if not pd.isna(signal.vol_ratio):
        header_lines.append(f"거래량비: {signal.vol_ratio:.1f}배")

    # 사유
    if signal.reasons:
        header_lines.append("")
        header_lines.append("📋 근거:")
        for r in signal.reasons:
            header_lines.append(f"  • {r}")

    # 경고
    if signal.warnings:
        header_lines.append("")
        header_lines.append("⚠️ 주의:")
        for w in signal.warnings:
            header_lines.append(f"  • {w}")

    return "\n".join(header_lines)


# ---------------------------------------------------------------------------
# 뉴스/알림 전송
# ---------------------------------------------------------------------------

def send_premarket_news() -> None:
    """08:30: 개장 전 뉴스."""
    from alerts.market_guard import is_market_holiday
    if is_market_holiday():
        return
    from data.collector import fetch_market_news
    news = fetch_market_news(10)
    if not news:
        return
    logger.info("개장 전 뉴스 총 %d건 수집", len(news))
    lines = ["📰 [개장 전 뉴스]", "━" * 23]
    for n in news[:10]:
        lines.append(f"  · {n['title'][:50]}")
    msg = "\n".join(lines) + CMD_FOOTER
    TelegramNotifier().broadcast(msg)


def send_news_update() -> None:
    """매시 정각: 장중 뉴스 업데이트."""
    from alerts.market_guard import is_market_hours
    if not is_market_hours():
        return
    from data.collector import fetch_market_news
    news = fetch_market_news(5)
    if not news:
        return
    now = datetime.now().strftime("%H:%M")
    lines = [f"📰 [{now} 뉴스]", "━" * 23]
    for n in news[:5]:
        lines.append(f"  · {n['title'][:50]}")
    msg = "\n".join(lines) + CMD_FOOTER
    notifier = TelegramNotifier()
    notifier.broadcast(msg)
    logger.info("장중 뉴스 %d건 전송 완료", len(news))


def send_market_open() -> None:
    """09:00: 장 시작 알림."""
    from alerts.market_guard import is_market_holiday
    if is_market_holiday():
        return
    holding = len(load_auto_positions())
    msg = (
        f"🔔 장 시작 (09:00)\n"
        f"운영 모드: {OPERATION_MODE}\n"
        f"자동매매: {'ON' if AUTO_TRADE_ENABLED else 'OFF'}\n"
        f"보유 종목: {holding}개 / {MAX_SLOTS}슬롯"
        + CMD_FOOTER
    )
    TelegramNotifier().broadcast(msg)


def send_market_close() -> None:
    """15:40: 장 마감 알림."""
    from alerts.market_guard import is_market_holiday
    if is_market_holiday():
        return
    positions = load_auto_positions()
    lines = ["🔔 장 마감 (15:40)", "━" * 23]
    if positions:
        lines.append(f"보유 {len(positions)}종목:")
        for t, p in positions.items():
            lines.append(f"  · {p.get('name', t)} {p.get('qty', 0)}주")
    else:
        lines.append("보유 종목 없음")
    msg = "\n".join(lines) + CMD_FOOTER
    TelegramNotifier().broadcast(msg)


def send_daily_report() -> None:
    """16:00: 일일 마감 AI 리포트."""
    from alerts.market_guard import is_market_holiday, fetch_index_prices
    if is_market_holiday():
        return
    from ai.ai_analyzer import AIAnalyzer
    ai = AIAnalyzer()
    positions = load_auto_positions()
    monthly = load_monthly_loss()
    report = ai.daily_report(
        portfolio_data={"positions": positions, "monthly_loss": monthly},
        market_data={"indices": fetch_index_prices()},
    )
    msg = f"📋 [일일 마감 리포트]\n━━━━━━━━━━━━━━━━━━━━━━━\n{report}" + CMD_FOOTER
    TelegramNotifier().broadcast(msg)
    logger.info("일일 리포트 전송 완료")
