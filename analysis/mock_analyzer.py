"""MOCK 테스트 결과 자동 분석 — 주간/월간 성과 리포트."""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger("stock_analysis")

ROOT = Path(__file__).parent.parent
JOURNAL_PATH = ROOT / "data" / "trade_journal.csv"


def _read_trades(days: int) -> list[dict]:
    """최근 N일간 매매 기록 읽기."""
    if not JOURNAL_PATH.exists():
        return []

    cutoff = datetime.now() - timedelta(days=days)
    trades: list[dict] = []

    with open(JOURNAL_PATH, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt_str = row.get("datetime", "")
            if not dt_str:
                continue
            try:
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue
            if dt < cutoff:
                continue
            trades.append(row)

    return trades


def _safe_int(value: str, default: int = 0) -> int:
    """안전한 int 변환."""
    try:
        return int(float(value)) if value else default
    except (ValueError, TypeError):
        return default


def _safe_float(value: str, default: float = 0.0) -> float:
    """안전한 float 변환."""
    try:
        return float(value) if value else default
    except (ValueError, TypeError):
        return default


def analyze_period(days: int = 7) -> dict:
    """최근 N일간 MOCK 매매 분석.

    Returns:
        {
            "period": "2026-04-07 ~ 2026-04-13",
            "total_trades": 15,
            "buys": 8, "sells": 7,
            "total_pnl": 45000,
            "win_rate": 57.1,
            "avg_win": 12000, "avg_loss": 5000,
            "profit_factor": 2.4,
            "best_trade": {"name": "삼성전자", "pnl": 25000},
            "worst_trade": {"name": "KODEX AI", "pnl": -8000},
            "by_ticker": {"005930": {"trades": 3, "pnl": 15000, "win_rate": 66.7}},
            "by_hour": {"09": {"trades": 5, "pnl": 20000}, "10": {...}},
            "by_reason": {"VB돌파": 5, "트레일링": 3, "손절": 2, "EOD청산": 2},
            "by_regime": {"NORMAL": 10, "SWING": 3},
            "avg_hold_minutes": 180,
            "eod_liquidation_count": 2,
            "issues": ["체결강도 필터로 3건 차단됨", "09시대 승률 40% (전체 대비 낮음)"]
        }
    """
    trades = _read_trades(days)

    now = datetime.now()
    start_date = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    result: dict = {
        "period": f"{start_date} ~ {end_date}",
        "total_trades": 0,
        "buys": 0,
        "sells": 0,
        "total_pnl": 0,
        "win_rate": 0.0,
        "avg_win": 0,
        "avg_loss": 0,
        "profit_factor": 0.0,
        "best_trade": {"name": "", "pnl": 0},
        "worst_trade": {"name": "", "pnl": 0},
        "by_ticker": {},
        "by_hour": {},
        "by_reason": {},
        "by_regime": {},
        "avg_hold_minutes": 0,
        "eod_liquidation_count": 0,
        "issues": [],
    }

    if not trades:
        return result

    # 기본 카운트
    buys = 0
    sells = 0
    wins: list[int] = []
    losses: list[int] = []
    total_pnl = 0
    hold_times: list[int] = []
    eod_count = 0

    best_trade = {"name": "", "pnl": 0}
    worst_trade = {"name": "", "pnl": 0}

    # 그룹별 집계
    by_ticker: dict[str, dict] = defaultdict(lambda: {"trades": 0, "pnl": 0, "wins": 0, "sells": 0, "name": ""})
    by_hour: dict[str, dict] = defaultdict(lambda: {"trades": 0, "pnl": 0, "wins": 0, "sells": 0})
    by_reason: dict[str, int] = defaultdict(int)
    by_regime: dict[str, int] = defaultdict(int)

    for trade in trades:
        side = trade.get("side", "")
        ticker = trade.get("ticker", "")
        name = trade.get("name", ticker)
        reason = trade.get("reason", "")
        regime = trade.get("regime", "")
        dt_str = trade.get("datetime", "")

        if side == "buy":
            buys += 1
        elif side == "sell":
            sells += 1
            pnl = _safe_int(trade.get("pnl", ""))
            total_pnl += pnl

            if pnl > 0:
                wins.append(pnl)
            elif pnl < 0:
                losses.append(pnl)

            # 최고/최저 거래
            if pnl > best_trade["pnl"]:
                best_trade = {"name": name, "pnl": pnl}
            if pnl < worst_trade["pnl"]:
                worst_trade = {"name": name, "pnl": pnl}

            # 보유 시간
            hold_min = _safe_int(trade.get("hold_time", ""))
            if hold_min > 0:
                hold_times.append(hold_min)

            # EOD 청산
            if "EOD" in reason or "장마감" in reason:
                eod_count += 1

            # 종목별 집계
            by_ticker[ticker]["trades"] += 1
            by_ticker[ticker]["sells"] += 1
            by_ticker[ticker]["pnl"] += pnl
            by_ticker[ticker]["name"] = name
            if pnl > 0:
                by_ticker[ticker]["wins"] += 1

            # 시간대별 집계
            hour = dt_str[11:13] if len(dt_str) >= 13 else "??"
            by_hour[hour]["trades"] += 1
            by_hour[hour]["sells"] += 1
            by_hour[hour]["pnl"] += pnl
            if pnl > 0:
                by_hour[hour]["wins"] += 1

        # 사유별 집계 (매수/매도 모두)
        if reason:
            by_reason[reason] += 1

        # 레짐별 집계
        if regime:
            by_regime[regime] += 1

    # 승률 계산
    win_rate = (len(wins) / sells * 100) if sells > 0 else 0.0

    # 평균 수익/손실
    avg_win = int(sum(wins) / len(wins)) if wins else 0
    avg_loss = int(sum(losses) / len(losses)) if losses else 0

    # Profit Factor
    total_win = sum(wins) if wins else 0
    total_loss = abs(sum(losses)) if losses else 0
    profit_factor = (total_win / total_loss) if total_loss > 0 else (float("inf") if total_win > 0 else 0.0)

    # 종목별 승률 계산
    ticker_result: dict[str, dict] = {}
    for ticker, data in by_ticker.items():
        wr = (data["wins"] / data["sells"] * 100) if data["sells"] > 0 else 0.0
        ticker_result[ticker] = {
            "trades": data["trades"],
            "pnl": data["pnl"],
            "win_rate": round(wr, 1),
            "name": data["name"],
        }

    # 시간대별 승률 계산
    hour_result: dict[str, dict] = {}
    for hour, data in sorted(by_hour.items()):
        wr = (data["wins"] / data["sells"] * 100) if data["sells"] > 0 else 0.0
        hour_result[hour] = {
            "trades": data["trades"],
            "pnl": data["pnl"],
            "win_rate": round(wr, 1),
        }

    result.update({
        "total_trades": buys + sells,
        "buys": buys,
        "sells": sells,
        "total_pnl": total_pnl,
        "win_rate": round(win_rate, 1),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": round(profit_factor, 1) if profit_factor != float("inf") else 999.9,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "by_ticker": ticker_result,
        "by_hour": hour_result,
        "by_reason": dict(by_reason),
        "by_regime": dict(by_regime),
        "avg_hold_minutes": int(sum(hold_times) / len(hold_times)) if hold_times else 0,
        "eod_liquidation_count": eod_count,
    })

    # 이슈 감지
    issues: list[str] = []

    # 시간대별 낮은 승률 감지
    overall_wr = win_rate
    for hour, data in hour_result.items():
        if data["trades"] >= 3 and data["win_rate"] < 35:
            issues.append(
                f"{hour}시대 승률 {data['win_rate']:.0f}% "
                f"(전체 {overall_wr:.0f}% 대비 낮음, {data['trades']}건)"
            )

    # 자주 손절되는 종목 감지
    for ticker, data in ticker_result.items():
        if data["trades"] >= 3 and data["win_rate"] < 30:
            issues.append(
                f"종목 {data.get('name', ticker)} ({ticker}) 승률 {data['win_rate']:.0f}% "
                f"({data['trades']}건)"
            )

    # EOD 청산 비율
    if sells > 0 and eod_count / sells > 0.3:
        issues.append(
            f"EOD 청산 비율 높음: {eod_count}/{sells}건 "
            f"({eod_count / sells * 100:.0f}%) — 진입 시점 개선 검토"
        )

    # PF < 1.0
    if sells >= 5 and profit_factor < 1.0:
        issues.append(f"Profit Factor {profit_factor:.1f} — 전략 재검토 필요")

    result["issues"] = issues

    return result


def generate_weekly_report() -> str:
    """주간 리포트 텍스트 생성. 텔레그램 발송용."""
    stats = analyze_period(7)

    lines = [
        "📊 [주간 MOCK 매매 리포트]",
        f"기간: {stats['period']}",
        "━" * 23,
        "",
        f"📈 매매 현황:",
        f"  총 거래: {stats['total_trades']}건 (매수 {stats['buys']} / 매도 {stats['sells']})",
        f"  총 손익: {stats['total_pnl']:+,}원",
        f"  승률: {stats['win_rate']:.1f}%",
        f"  Profit Factor: {stats['profit_factor']:.1f}",
        f"  평균 수익: {stats['avg_win']:+,}원 / 평균 손실: {stats['avg_loss']:+,}원",
        f"  평균 보유: {stats['avg_hold_minutes']}분",
    ]

    # 최고/최저 거래
    best = stats["best_trade"]
    worst = stats["worst_trade"]
    if best["name"]:
        lines.append(f"  최고: {best['name']} {best['pnl']:+,}원")
    if worst["name"]:
        lines.append(f"  최저: {worst['name']} {worst['pnl']:+,}원")

    # 레짐별 분포
    if stats["by_regime"]:
        lines.append("")
        lines.append("🔄 레짐별 분포:")
        for regime, count in sorted(stats["by_regime"].items()):
            lines.append(f"  {regime}: {count}건")

    # 사유별 분포
    if stats["by_reason"]:
        lines.append("")
        lines.append("📋 사유별 분포:")
        for reason, count in sorted(stats["by_reason"].items(), key=lambda x: -x[1]):
            lines.append(f"  {reason}: {count}건")

    # 시간대별 성과
    if stats["by_hour"]:
        lines.append("")
        lines.append("🕐 시간대별 성과:")
        for hour, data in sorted(stats["by_hour"].items()):
            lines.append(
                f"  {hour}시: {data['trades']}건, "
                f"{data['pnl']:+,}원, 승률 {data['win_rate']:.0f}%"
            )

    # 종목별 성과 (상위 5개)
    if stats["by_ticker"]:
        lines.append("")
        lines.append("📊 종목별 성과 (상위 5):")
        sorted_tickers = sorted(
            stats["by_ticker"].items(),
            key=lambda x: x[1]["pnl"],
            reverse=True,
        )
        for ticker, data in sorted_tickers[:5]:
            lines.append(
                f"  {data.get('name', ticker)}: "
                f"{data['trades']}건, {data['pnl']:+,}원, "
                f"승률 {data['win_rate']:.0f}%"
            )

    # 이슈
    if stats["issues"]:
        lines.append("")
        lines.append("⚠️ 감지된 이슈:")
        for issue in stats["issues"]:
            lines.append(f"  • {issue}")

    # 개선 제안
    suggestions = generate_improvement_suggestions(stats)
    if suggestions:
        lines.append("")
        lines.append("💡 개선 제안:")
        for s in suggestions:
            lines.append(f"  • {s}")

    return "\n".join(lines)


def generate_improvement_suggestions(stats: dict) -> list[str]:
    """성과 데이터 기반 개선 제안 생성."""
    suggestions: list[str] = []

    # 승률 40% 미만 종목 제거 검토
    for ticker, data in stats.get("by_ticker", {}).items():
        if data["trades"] >= 3 and data["win_rate"] < 40:
            name = data.get("name", ticker)
            suggestions.append(
                f"종목 {name} ({ticker}) 승률 {data['win_rate']:.0f}% "
                f"— 화이트리스트 제거 검토"
            )

    # 시간대별 분석
    for hour, data in stats.get("by_hour", {}).items():
        if data["trades"] >= 3 and data.get("win_rate", 50) < 35:
            suggestions.append(
                f"{hour}시대 승률 낮음 ({data.get('win_rate', 0):.0f}%) "
                f"— 시간 필터 강화 검토"
            )

    # PF < 1.0
    if stats.get("profit_factor", 1.0) < 1.0:
        suggestions.append("전체 PF < 1.0 — 전략 재검토 필요")

    # PF < 1.5 (주의 구간)
    pf = stats.get("profit_factor", 1.0)
    if 1.0 <= pf < 1.5 and stats.get("sells", 0) >= 10:
        suggestions.append(
            f"PF {pf:.1f} — 수익성 개선 여지 있음 (손절폭 조정 또는 진입 필터 강화)"
        )

    # 손절 비율 과다
    reasons = stats.get("by_reason", {})
    total_sells = sum(v for k, v in reasons.items() if k != "매수")
    stoploss_count = reasons.get("손절", 0)
    if total_sells > 0 and stoploss_count / total_sells > 0.5:
        suggestions.append(
            f"손절 비율 {stoploss_count}/{total_sells} "
            f"({stoploss_count / total_sells * 100:.0f}%) "
            f"— 진입 필터 강화 또는 손절폭 조정 검토"
        )

    # EOD 청산 비율 과다
    eod = stats.get("eod_liquidation_count", 0)
    sells = stats.get("sells", 0)
    if sells > 0 and eod / sells > 0.4:
        suggestions.append(
            f"EOD 청산 비율 {eod}/{sells} ({eod / sells * 100:.0f}%) "
            f"— 매수 시간 제한 (14시 이후 신규 진입 차단) 검토"
        )

    # 평균 보유 시간이 너무 짧으면 (30분 미만)
    avg_hold = stats.get("avg_hold_minutes", 0)
    if avg_hold > 0 and avg_hold < 30 and sells >= 5:
        suggestions.append(
            f"평균 보유 시간 {avg_hold}분 — 너무 짧음, 진입 타이밍 또는 손절 기준 점검"
        )

    # 승률이 매우 낮을 때
    wr = stats.get("win_rate", 0)
    if sells >= 10 and wr < 30:
        suggestions.append(
            f"전체 승률 {wr:.0f}% — 진입 신호 품질 개선 시급"
        )

    return suggestions
