"""로그 자동 분석 — 오류 패턴, 필터 통계, 이상 감지."""

from __future__ import annotations

import re
import logging
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter, defaultdict

logger = logging.getLogger("stock_analysis")

ROOT = Path(__file__).parent.parent
DEBUG_LOG_PATH = ROOT / "logs" / "debug.log"

# ── 로그 라인 파싱 패턴 ──
# 일반 로그 형식: "2026-04-13 09:01:23,456 - stock_analysis - DEBUG - [VB] ..."
_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}),?\d*\s*-\s*\S+\s*-\s*(\w+)\s*-\s*(.+)$"
)

# ── 필터 패턴 매칭 ──
_FILTER_PATTERNS: dict[str, re.Pattern] = {
    "레짐필터": re.compile(r"레짐필터\s*실패"),
    "마켓필터": re.compile(r"마켓필터\s*실패"),
    "변동성필터": re.compile(r"변동성.*부족|변동성필터\s*실패"),
    "체결강도": re.compile(r"체결강도\s*부족"),
    "호가불균형": re.compile(r"호가\s*불균형"),
    "다이버전스": re.compile(r"다이버전스\s*감지"),
    "거래량부족": re.compile(r"거래량\s*부족"),
    "목표가미돌파": re.compile(r"목표가\s*체크.*vs|목표가 미돌파"),
    "스크리너제외": re.compile(r"\[스크리너\].*제외"),
    "쿨다운": re.compile(r"쿨다운\s*중"),
    "강도부족": re.compile(r"강도\s*부족"),
    "시간초과": re.compile(r"매수\s*시간\s*초과|장\s*시작.*미만"),
}

# ── 레짐 전환 패턴 ──
_REGIME_TRANSITION_RE = re.compile(
    r"\[레짐\s*전환\]\s*(\w+)\s*->\s*(\w+)\s*\(사유:\s*(.+?)\)"
)

# ── 필터 통과 패턴 (전체 평가 횟수 추정용) ──
_FILTER_PASS_RE = re.compile(r"(레짐필터|마켓필터|변동성필터)\s*통과")

# ── 매수 신호 발생 패턴 ──
_BUY_SIGNAL_RE = re.compile(r"BUY.*신호|매수.*접수|auto_trade.*매수")


def _read_log_lines(days: int) -> list[tuple[datetime, str, str]]:
    """최근 N일간 로그 라인 읽기.

    Returns:
        [(datetime, level, message), ...]
    """
    if not DEBUG_LOG_PATH.exists():
        return []

    cutoff = datetime.now() - timedelta(days=days)
    lines: list[tuple[datetime, str, str]] = []

    try:
        with open(DEBUG_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                raw_line = raw_line.rstrip("\n\r")
                m = _LOG_LINE_RE.match(raw_line)
                if not m:
                    continue
                dt_str, level, message = m.group(1), m.group(2), m.group(3)
                try:
                    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    continue
                if dt < cutoff:
                    continue
                lines.append((dt, level.upper(), message))
    except OSError as e:
        logger.error("[로그분석] 로그 파일 읽기 실패: %s", e)

    return lines


def analyze_errors(days: int = 1) -> dict:
    """최근 N일 에러/경고 분석.

    Returns:
        {
            "errors": [{"message": "...", "count": 5, "last_seen": "..."}],
            "warnings": [{"message": "...", "count": 3, "last_seen": "..."}],
            "error_count": 12,
            "warning_count": 25,
            "top_errors": [("yfinance 조회 실패", 5), ("텔레그램 발송 실패", 3)],
        }
    """
    lines = _read_log_lines(days)

    error_counter: Counter = Counter()
    warning_counter: Counter = Counter()
    error_last_seen: dict[str, str] = {}
    warning_last_seen: dict[str, str] = {}

    for dt, level, message in lines:
        # 에러 메시지 정규화 (숫자/변동 부분 제거하여 그룹핑)
        normalized = _normalize_message(message)
        dt_str = dt.strftime("%Y-%m-%d %H:%M:%S")

        if level == "ERROR":
            error_counter[normalized] += 1
            error_last_seen[normalized] = dt_str
        elif level == "WARNING":
            warning_counter[normalized] += 1
            warning_last_seen[normalized] = dt_str

    # 에러 목록 (빈도순)
    errors = [
        {"message": msg, "count": cnt, "last_seen": error_last_seen.get(msg, "")}
        for msg, cnt in error_counter.most_common()
    ]

    warnings = [
        {"message": msg, "count": cnt, "last_seen": warning_last_seen.get(msg, "")}
        for msg, cnt in warning_counter.most_common()
    ]

    top_errors = error_counter.most_common(10)

    return {
        "errors": errors,
        "warnings": warnings,
        "error_count": sum(error_counter.values()),
        "warning_count": sum(warning_counter.values()),
        "top_errors": top_errors,
    }


def _normalize_message(message: str) -> str:
    """에러 메시지 정규화 — 수치/ID 부분을 일반화하여 그룹핑.

    예: "텔레그램 발송 실패 chat_id=12345" -> "텔레그램 발송 실패 chat_id=..."
    """
    # chat_id, status 등의 변수값 마스킹
    result = re.sub(r"chat_id=\S+", "chat_id=...", message)
    result = re.sub(r"status=\d+", "status=...", result)
    # 구체적 숫자값 마스킹 (4자리 이상)
    result = re.sub(r"\b\d{4,}\b", "...", result)
    # 긴 body/traceback 부분 자르기
    result = re.sub(r"body=.{50,}", "body=...", result)
    # 최대 길이 제한
    if len(result) > 120:
        result = result[:120] + "..."
    return result.strip()


def analyze_filters(days: int = 1) -> dict:
    """필터별 차단 통계.

    Returns:
        {
            "total_evaluations": 500,
            "filter_blocks": {
                "레짐필터": 50,
                "마켓필터": 30,
                "변동성필터": 20,
                "체결강도": 15,
                "호가불균형": 10,
                "목표가미돌파": 300,
            },
            "pass_rate": 15.0,
        }
    """
    lines = _read_log_lines(days)

    filter_blocks: Counter = Counter()
    total_evaluations = 0
    buy_signals = 0

    for dt, level, message in lines:
        # 필터 차단 카운트
        for filter_name, pattern in _FILTER_PATTERNS.items():
            if pattern.search(message):
                filter_blocks[filter_name] += 1
                break  # 한 라인은 하나의 필터만 카운트

        # 전체 평가 횟수 추정: 레짐필터 통과/실패 = 1회 평가
        # 레짐필터가 가장 첫 번째 필터이므로 이를 기준으로 사용
        if "레짐필터" in message:
            total_evaluations += 1

        # 매수 신호 카운트
        if _BUY_SIGNAL_RE.search(message):
            buy_signals += 1

    # total_evaluations가 0이면 다른 방법으로 추정
    if total_evaluations == 0:
        # 모든 필터 차단 + 신호 발생 합계
        total_evaluations = sum(filter_blocks.values()) + buy_signals

    pass_rate = 0.0
    if total_evaluations > 0:
        pass_rate = buy_signals / total_evaluations * 100

    return {
        "total_evaluations": total_evaluations,
        "filter_blocks": dict(filter_blocks.most_common()),
        "buy_signals": buy_signals,
        "pass_rate": round(pass_rate, 1),
    }


def analyze_regime_transitions(days: int = 7) -> dict:
    """레짐 전환 이력 분석.

    Returns:
        {
            "transitions": [
                {"datetime": "2026-04-10 10:30:00", "from": "NORMAL", "to": "SWING", "reason": "지수 -2.0%"},
                ...
            ],
            "transition_count": 5,
            "by_transition": {"NORMAL->SWING": 3, "SWING->NORMAL": 2},
            "avg_daily_transitions": 0.7,
            "most_common_reason": "지수 하락",
        }
    """
    lines = _read_log_lines(days)

    transitions: list[dict] = []
    transition_counter: Counter = Counter()
    reason_counter: Counter = Counter()

    for dt, level, message in lines:
        m = _REGIME_TRANSITION_RE.search(message)
        if m:
            from_state = m.group(1)
            to_state = m.group(2)
            reason = m.group(3)

            transitions.append({
                "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "from": from_state,
                "to": to_state,
                "reason": reason,
            })

            transition_counter[f"{from_state}->{to_state}"] += 1
            reason_counter[reason] += 1

    # 일별 평균 전환 횟수
    avg_daily = len(transitions) / days if days > 0 else 0

    # 가장 흔한 사유
    most_common_reason = reason_counter.most_common(1)[0][0] if reason_counter else ""

    return {
        "transitions": transitions,
        "transition_count": len(transitions),
        "by_transition": dict(transition_counter.most_common()),
        "avg_daily_transitions": round(avg_daily, 1),
        "most_common_reason": most_common_reason,
    }


def generate_diagnostic_report() -> str:
    """진단 리포트 텍스트. 텔레그램 발송용."""
    error_stats = analyze_errors(1)
    filter_stats = analyze_filters(1)
    regime_stats = analyze_regime_transitions(7)

    lines = [
        "🔍 [시스템 진단 리포트]",
        f"생성: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "━" * 23,
    ]

    # 에러/경고 요약
    lines.append("")
    lines.append(
        f"⚠️ 에러: {error_stats['error_count']}건 / "
        f"경고: {error_stats['warning_count']}건 (최근 24시간)"
    )

    if error_stats["top_errors"]:
        lines.append("  주요 에러:")
        for msg, cnt in error_stats["top_errors"][:5]:
            lines.append(f"    [{cnt}회] {msg[:60]}")

    # 필터 통계
    lines.append("")
    lines.append("🔧 필터 통계 (최근 24시간):")
    lines.append(f"  총 평가: {filter_stats['total_evaluations']}회")
    lines.append(f"  매수 신호: {filter_stats.get('buy_signals', 0)}건")
    lines.append(f"  통과율: {filter_stats['pass_rate']:.1f}%")

    if filter_stats["filter_blocks"]:
        lines.append("  차단 내역:")
        for filter_name, count in filter_stats["filter_blocks"].items():
            lines.append(f"    {filter_name}: {count}건")

    # 레짐 전환
    lines.append("")
    lines.append(
        f"🔄 레짐 전환: {regime_stats['transition_count']}회 "
        f"(최근 7일, 일평균 {regime_stats['avg_daily_transitions']:.1f}회)"
    )

    if regime_stats["by_transition"]:
        for transition, count in regime_stats["by_transition"].items():
            lines.append(f"  {transition}: {count}회")

    # 최근 전환 이력 (최대 3건)
    recent = regime_stats["transitions"][-3:] if regime_stats["transitions"] else []
    if recent:
        lines.append("  최근 전환:")
        for t in recent:
            lines.append(
                f"    {t['datetime']} {t['from']}->{t['to']} ({t['reason'][:40]})"
            )

    # 이상 감지
    anomalies: list[str] = []

    if error_stats["error_count"] > 50:
        anomalies.append(f"에러 과다: {error_stats['error_count']}건 (기준: 50건)")

    if filter_stats["pass_rate"] > 0 and filter_stats["pass_rate"] < 1.0:
        anomalies.append(
            f"필터 통과율 극히 낮음: {filter_stats['pass_rate']:.1f}% "
            f"— 필터 조건 과도한지 점검 필요"
        )

    if regime_stats["avg_daily_transitions"] > 3:
        anomalies.append(
            f"레짐 전환 빈번: 일평균 {regime_stats['avg_daily_transitions']:.1f}회 "
            f"— 진동 방지 쿨다운 확인"
        )

    if anomalies:
        lines.append("")
        lines.append("🚨 이상 감지:")
        for a in anomalies:
            lines.append(f"  • {a}")

    return "\n".join(lines)
