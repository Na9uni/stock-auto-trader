"""전략 개선 사이클 — 데이터 기반 자동 개선 제안.

주기: 매주 금요일 16:00 실행
흐름:
  1. MOCK 결과 분석 (mock_analyzer)
  2. 로그 분석 (log_analyzer)
  3. 종합 개선 보고서 생성
  4. 텔레그램으로 발송
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger("stock_analysis")


def run_improvement_cycle() -> None:
    """주간 전략 개선 사이클 실행."""
    from analysis.mock_analyzer import analyze_period, generate_improvement_suggestions
    from analysis.log_analyzer import analyze_errors, analyze_filters, analyze_regime_transitions

    logger.info("[개선사이클] 주간 분석 시작")

    # 1. MOCK 결과 분석
    try:
        mock_stats = analyze_period(7)
    except Exception as e:
        logger.error("[개선사이클] MOCK 분석 실패: %s", e)
        mock_stats = {"period": "N/A", "total_trades": 0, "total_pnl": 0,
                      "win_rate": 0, "profit_factor": 0, "by_ticker": {},
                      "by_hour": {}, "by_reason": {}}

    # 2. 로그 분석
    try:
        error_stats = analyze_errors(7)
    except Exception as e:
        logger.error("[개선사이클] 에러 분석 실패: %s", e)
        error_stats = {"error_count": 0, "warning_count": 0}

    try:
        filter_stats = analyze_filters(7)
    except Exception as e:
        logger.error("[개선사이클] 필터 분석 실패: %s", e)
        filter_stats = {"total_evaluations": 0, "pass_rate": 0}

    try:
        regime_stats = analyze_regime_transitions(7)
    except Exception as e:
        logger.error("[개선사이클] 레짐 분석 실패: %s", e)
        regime_stats = {"transition_count": 0, "avg_daily_transitions": 0}

    # 3. 개선 제안 생성
    try:
        suggestions = generate_improvement_suggestions(mock_stats)
    except Exception as e:
        logger.error("[개선사이클] 개선 제안 생성 실패: %s", e)
        suggestions = []

    # 4. 종합 리포트 생성
    report_lines = [
        "📋 [주간 전략 개선 리포트]",
        f"기간: {mock_stats.get('period', 'N/A')}",
        f"생성: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "━" * 23,
        "",
        "📊 매매 성과:",
        f"  거래: {mock_stats.get('total_trades', 0)}건",
        f"  손익: {mock_stats.get('total_pnl', 0):+,}원",
        f"  승률: {mock_stats.get('win_rate', 0):.1f}%",
        f"  PF: {mock_stats.get('profit_factor', 0):.1f}",
    ]

    # 평균 수익/손실
    avg_win = mock_stats.get("avg_win", 0)
    avg_loss = mock_stats.get("avg_loss", 0)
    if avg_win or avg_loss:
        report_lines.append(f"  평균 수익: {avg_win:+,}원 / 손실: {avg_loss:+,}원")

    # 최고/최저 거래
    best = mock_stats.get("best_trade", {})
    worst = mock_stats.get("worst_trade", {})
    if best.get("name"):
        report_lines.append(f"  최고: {best['name']} {best['pnl']:+,}원")
    if worst.get("name"):
        report_lines.append(f"  최저: {worst['name']} {worst['pnl']:+,}원")

    # 에러/경고
    report_lines.append("")
    report_lines.append(
        f"⚠️ 에러: {error_stats.get('error_count', 0)}건 / "
        f"경고: {error_stats.get('warning_count', 0)}건"
    )

    # 주요 에러 (상위 3건)
    top_errors = error_stats.get("top_errors", [])
    if top_errors:
        for msg, cnt in top_errors[:3]:
            report_lines.append(f"  [{cnt}회] {msg[:50]}")

    # 필터 통계
    report_lines.append("")
    report_lines.append("🔍 필터 통계:")
    report_lines.append(f"  총 평가: {filter_stats.get('total_evaluations', 0)}회")
    report_lines.append(f"  통과율: {filter_stats.get('pass_rate', 0):.1f}%")

    filter_blocks = filter_stats.get("filter_blocks", {})
    if filter_blocks:
        # 상위 5개 필터만 표시
        items = list(filter_blocks.items())[:5]
        for name, count in items:
            report_lines.append(f"  {name}: {count}건")

    # 레짐 전환
    report_lines.append("")
    report_lines.append(
        f"🔄 레짐 전환: {regime_stats.get('transition_count', 0)}회 "
        f"(일평균 {regime_stats.get('avg_daily_transitions', 0):.1f}회)"
    )

    # 종목별 성과 요약 (상위 3개 + 하위 3개)
    by_ticker = mock_stats.get("by_ticker", {})
    if by_ticker:
        sorted_tickers = sorted(by_ticker.items(), key=lambda x: x[1]["pnl"], reverse=True)
        report_lines.append("")
        report_lines.append("📊 종목별 성과:")

        # 상위 3개
        for ticker, data in sorted_tickers[:3]:
            name = data.get("name", ticker)
            report_lines.append(
                f"  🟢 {name}: {data['pnl']:+,}원 "
                f"(승률 {data['win_rate']:.0f}%, {data['trades']}건)"
            )

        # 하위 3개 (손실 종목)
        for ticker, data in sorted_tickers[-3:]:
            if data["pnl"] < 0:
                name = data.get("name", ticker)
                report_lines.append(
                    f"  🔴 {name}: {data['pnl']:+,}원 "
                    f"(승률 {data['win_rate']:.0f}%, {data['trades']}건)"
                )

    # 개선 제안
    if suggestions:
        report_lines.append("")
        report_lines.append("💡 개선 제안:")
        for s in suggestions:
            report_lines.append(f"  • {s}")
    else:
        report_lines.append("")
        report_lines.append("✅ 특별한 개선 사항 없음")

    # 이슈 (mock_analyzer에서 감지된 것)
    issues = mock_stats.get("issues", [])
    if issues:
        report_lines.append("")
        report_lines.append("🚨 감지된 이슈:")
        for issue in issues:
            report_lines.append(f"  • {issue}")

    report = "\n".join(report_lines)
    logger.info("[개선사이클] 리포트 생성 완료 (%d자)", len(report))

    # 5. 텔레그램 발송
    try:
        from alerts.telegram_notifier import TelegramNotifier
        from alerts.notifications import get_admin_id, CMD_FOOTER
        TelegramNotifier().send_to_users([get_admin_id()], report + CMD_FOOTER)
        logger.info("[개선사이클] 텔레그램 발송 완료")
    except Exception as e:
        logger.error("[개선사이클] 텔레그램 발송 실패: %s", e)

    logger.info("[개선사이클] 주간 분석 완료")
