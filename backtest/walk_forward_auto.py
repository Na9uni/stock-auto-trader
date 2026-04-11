"""Walk-Forward 자동 재최적화 — 분기별 K값 검증 + 과적합 탐지.

2년 데이터를 train(18개월) / test(6개월)로 분할하여
종목별 최적 K값을 찾고, train vs test Sharpe 비교로 과적합을 탐지한다.

실행: python -m backtest.walk_forward_auto
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd

from backtest.backtester_v2 import BacktesterV2, download
from config.trading_config import TradingConfig

logger = logging.getLogger("stock_analysis")

# ── 화이트리스트 (yfinance ticker 매핑) ──
TICKERS: dict[str, tuple[str, str]] = {
    # ETF
    "069500": ("069500.KS", "KODEX 200"),
    "229200": ("229200.KS", "KODEX 코스닥150"),
    "133690": ("133690.KS", "TIGER 나스닥100"),
    "131890": ("131890.KS", "ACE 삼성그룹동일가중"),
    "108450": ("108450.KS", "ACE 삼성그룹섹터가중"),
    "395160": ("395160.KS", "KODEX AI반도체"),
    "261220": ("261220.KS", "KODEX WTI원유선물"),
    "130730": ("130730.KS", "KODEX 인버스"),
    "132030": ("132030.KS", "KODEX 골드선물"),
    # 개별주
    "005930": ("005930.KS", "삼성전자"),
    "105560": ("105560.KS", "KB금융"),
    "055550": ("055550.KS", "신한지주"),
    "016610": ("016610.KS", "DB증권"),
    "019180": ("019180.KS", "티에이치엔"),
    "000500": ("000500.KS", "가온전선"),
    "014790": ("014790.KS", "HL D&I"),
    "103590": ("103590.KS", "일진전기"),
    "009420": ("009420.KS", "한올바이오파마"),
    "034020": ("034020.KS", "두산에너빌리티"),
    "078600": ("078600.KS", "대주전자재료"),
}

ETF_CODES = frozenset({
    "069500", "229200", "133690", "131890", "108450",
    "395160", "261220", "130730", "132030",
})

K_RANGE = [round(0.3 + 0.1 * i, 1) for i in range(6)]  # 0.3 ~ 0.8

# 과적합 판정 기준: test Sharpe가 train 대비 50% 이상 하락
OVERFIT_DROP_THRESHOLD = 0.50
# 개선 임계값: 15% 이상 Sharpe 개선 시 추천
IMPROVEMENT_THRESHOLD = 0.15


def _optimize_k_on_split(
    ticker_code: str,
    df: pd.DataFrame,
    k_range: list[float] | None = None,
) -> dict:
    """단일 구간에서 K값 그리드 서치. Sharpe 기준 최적화.

    Returns:
        {best_k, best_sharpe, all_results: [{k, sharpe, return, trades, ...}]}
    """
    if k_range is None:
        k_range = K_RANGE

    best_k = k_range[0]
    best_sharpe = -999.0
    all_results = []

    for k in k_range:
        config = TradingConfig.from_dict({"vb_k": k, "vb_k_individual": k})
        bt = BacktesterV2(config)
        stats = bt.run_vb(ticker_code, df)

        sharpe = stats.get("sharpe_ratio", 0.0)
        trades = stats.get("total_trades", 0)

        # 최소 거래 수 필터: 5회 미만은 통계적 의미 없음
        effective_sharpe = sharpe if trades >= 5 else -999.0

        all_results.append({
            "k": k,
            "sharpe": round(sharpe, 2),
            "total_return_pct": stats.get("total_return_pct", 0),
            "profit_factor": stats.get("profit_factor", 0),
            "max_drawdown_pct": stats.get("max_drawdown_pct", 0),
            "win_rate_pct": stats.get("win_rate_pct", 0),
            "total_trades": trades,
        })

        if effective_sharpe > best_sharpe:
            best_sharpe = effective_sharpe
            best_k = k

    return {
        "best_k": best_k,
        "best_sharpe": round(best_sharpe, 2),
        "all_results": all_results,
    }


def walk_forward_auto(
    ticker_code: str,
    yf_ticker: str,
    train_months: int = 18,
    test_months: int = 6,
    k_range: list[float] | None = None,
) -> dict:
    """Walk-Forward 자동 재최적화.

    1) 최근 2년(train_months + test_months) 데이터 다운로드
    2) train(앞 18개월) / test(뒤 6개월) 분할
    3) train에서 최적 K값 탐색
    4) test에서 검증
    5) train vs test Sharpe 비교 → 과적합 판정

    Args:
        ticker_code: 종목코드 (e.g. "069500")
        yf_ticker: yfinance 티커 (e.g. "069500.KS")
        train_months: 훈련 기간 (월)
        test_months: 검증 기간 (월)
        k_range: K값 탐색 범위

    Returns:
        {
            ticker_code, name, is_etf,
            train_best_k, train_sharpe, test_sharpe,
            sharpe_drop_pct, overfitted, recommended_k,
            current_k, improvement_pct, should_update,
            train_details, test_details,
        }
    """
    if k_range is None:
        k_range = K_RANGE

    total_months = train_months + test_months
    end_date = datetime.now()
    start_date = end_date - timedelta(days=total_months * 30)

    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    df = download(yf_ticker, start=start_str, end=end_str)
    if df.empty or len(df) < 200:
        return {"error": f"데이터 부족: {len(df)}행", "ticker_code": ticker_code}

    # train / test 분할 (거래일 기준)
    total_days = len(df)
    train_ratio = train_months / total_months
    train_end_idx = int(total_days * train_ratio)

    # 지표 워밍업용 패딩 (60거래일)
    warmup = 60

    train_df = df.iloc[:train_end_idx].copy().reset_index(drop=True)
    # test: 워밍업 포함하여 지표 계산 정확도 확보
    test_start = max(0, train_end_idx - warmup)
    test_df = df.iloc[test_start:].copy().reset_index(drop=True)

    if len(train_df) < 120 or len(test_df) < warmup + 30:
        return {"error": f"분할 후 데이터 부족 (train={len(train_df)}, test={len(test_df)})", "ticker_code": ticker_code}

    # train에서 최적 K값 탐색
    train_opt = _optimize_k_on_split(ticker_code, train_df, k_range)
    best_k = train_opt["best_k"]
    train_sharpe = train_opt["best_sharpe"]

    # test에서 최적 K로 검증
    config_test = TradingConfig.from_dict({"vb_k": best_k, "vb_k_individual": best_k})
    bt_test = BacktesterV2(config_test)
    test_stats = bt_test.run_vb(ticker_code, test_df)
    test_sharpe = test_stats.get("sharpe_ratio", 0.0)

    # 과적합 판정: test Sharpe가 train 대비 50% 이상 하락
    if train_sharpe > 0:
        sharpe_drop_pct = (train_sharpe - test_sharpe) / train_sharpe
    elif train_sharpe == 0:
        sharpe_drop_pct = 0.0
    else:
        # train Sharpe가 음수면 test도 비교 의미 없음
        sharpe_drop_pct = 0.0

    overfitted = sharpe_drop_pct > OVERFIT_DROP_THRESHOLD

    # 현재 설정값과 비교
    config = TradingConfig.from_env()
    is_etf = ticker_code in ETF_CODES
    current_k = config.vb_k if is_etf else config.vb_k_individual

    # 현재 K로 test 구간 Sharpe 계산 (비교용)
    config_current = TradingConfig.from_dict({"vb_k": current_k, "vb_k_individual": current_k})
    bt_current = BacktesterV2(config_current)
    current_test_stats = bt_current.run_vb(ticker_code, test_df)
    current_test_sharpe = current_test_stats.get("sharpe_ratio", 0.0)

    # 개선율 계산
    if current_test_sharpe > 0:
        improvement_pct = (test_sharpe - current_test_sharpe) / current_test_sharpe
    elif test_sharpe > 0:
        improvement_pct = 1.0  # 현재 0 이하 → 양수로 개선
    else:
        improvement_pct = 0.0

    should_update = (
        not overfitted
        and improvement_pct > IMPROVEMENT_THRESHOLD
        and test_sharpe > 0
    )

    name = TICKERS.get(ticker_code, (yf_ticker, ticker_code))[1]

    return {
        "ticker_code": ticker_code,
        "name": name,
        "is_etf": is_etf,
        "train_best_k": best_k,
        "train_sharpe": round(train_sharpe, 2),
        "test_sharpe": round(test_sharpe, 2),
        "current_k": current_k,
        "current_test_sharpe": round(current_test_sharpe, 2),
        "sharpe_drop_pct": round(sharpe_drop_pct * 100, 1),
        "overfitted": overfitted,
        "recommended_k": best_k,
        "improvement_pct": round(improvement_pct * 100, 1),
        "should_update": should_update,
        "test_return_pct": test_stats.get("total_return_pct", 0),
        "test_trades": test_stats.get("total_trades", 0),
        "test_mdd": test_stats.get("max_drawdown_pct", 0),
        "train_details": train_opt["all_results"],
        "test_details": {
            "total_return_pct": test_stats.get("total_return_pct", 0),
            "sharpe_ratio": test_stats.get("sharpe_ratio", 0),
            "profit_factor": test_stats.get("profit_factor", 0),
            "max_drawdown_pct": test_stats.get("max_drawdown_pct", 0),
            "win_rate_pct": test_stats.get("win_rate_pct", 0),
            "total_trades": test_stats.get("total_trades", 0),
        },
    }


def run_all_tickers(
    train_months: int = 18,
    test_months: int = 6,
) -> list[dict]:
    """전 종목 walk-forward 분석 실행.

    Returns:
        list of per-ticker results from walk_forward_auto()
    """
    results = []
    for code, (yf_ticker, name) in TICKERS.items():
        print(f"  [{name}] ({code}) 분석 중...")
        result = walk_forward_auto(
            ticker_code=code,
            yf_ticker=yf_ticker,
            train_months=train_months,
            test_months=test_months,
        )
        results.append(result)
    return results


def _build_telegram_report(results: list[dict]) -> str:
    """텔레그램 발송용 리포트 생성."""
    config = TradingConfig.from_env()
    lines = [
        "--- Walk-Forward 분기 재최적화 리포트 ---",
        f"분석일: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"현재 설정: ETF K={config.vb_k}, 개별주 K={config.vb_k_individual}",
        "",
    ]

    # 과적합 경고
    overfitted = [r for r in results if not r.get("error") and r.get("overfitted")]
    if overfitted:
        lines.append("[과적합 경고]")
        for r in overfitted:
            lines.append(
                f"  {r['name']}: train Sharpe={r['train_sharpe']}, "
                f"test Sharpe={r['test_sharpe']} "
                f"(하락 {r['sharpe_drop_pct']:.0f}%)"
            )
        lines.append("")

    # 업데이트 추천
    updates = [r for r in results if not r.get("error") and r.get("should_update")]
    if updates:
        lines.append("[K값 변경 추천] (Sharpe 15%+ 개선)")
        for r in updates:
            lines.append(
                f"  {r['name']}: K {r['current_k']} -> {r['recommended_k']} "
                f"(Sharpe {r['current_test_sharpe']:.2f} -> {r['test_sharpe']:.2f}, "
                f"+{r['improvement_pct']:.0f}%)"
            )
        lines.append("")
        lines.append("** 자동 반영 안 됨 — 수동 승인 필요 **")
    else:
        lines.append("[결과] 현재 K값 유지 권장 (유의미한 개선 없음)")

    # 에러
    errors = [r for r in results if r.get("error")]
    if errors:
        lines.append("")
        lines.append(f"[스킵] 데이터 부족 {len(errors)}종목")

    # ETF / 개별주 평균 추천 K
    etf_results = [r for r in results if not r.get("error") and r.get("is_etf") and not r.get("overfitted")]
    stock_results = [r for r in results if not r.get("error") and not r.get("is_etf") and not r.get("overfitted")]

    if etf_results:
        avg_etf_k = sum(r["train_best_k"] for r in etf_results) / len(etf_results)
        lines.append(f"\nETF 평균 추천 K: {avg_etf_k:.2f} (현재: {config.vb_k})")
    if stock_results:
        avg_stock_k = sum(r["train_best_k"] for r in stock_results) / len(stock_results)
        lines.append(f"개별주 평균 추천 K: {avg_stock_k:.2f} (현재: {config.vb_k_individual})")

    return "\n".join(lines)


def run_quarterly_reoptimization() -> dict:
    """분기별 재최적화 실행 — 수동 호출 또는 스케줄러에서 사용.

    1) 전 종목 walk-forward 분석
    2) 현재 K값 대비 개선 여부 판단
    3) 15% 이상 Sharpe 개선 시 텔레그램으로 추천 발송
    4) 자동 설정 변경 없음 (사람 승인 필요)

    Returns:
        {
            total_tickers, analyzed, skipped, overfitted_count,
            update_recommended, report_text, results,
        }
    """
    print("=" * 60)
    print("  Walk-Forward 분기 재최적화")
    print(f"  분석일: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    results = run_all_tickers()

    valid = [r for r in results if not r.get("error")]
    errors = [r for r in results if r.get("error")]
    overfitted = [r for r in valid if r.get("overfitted")]
    updates = [r for r in valid if r.get("should_update")]

    report_text = _build_telegram_report(results)

    # 텔레그램 발송 (업데이트 추천이 있거나 과적합 경고가 있을 때)
    if updates or overfitted:
        try:
            from alerts.telegram_notifier import TelegramNotifier
            notifier = TelegramNotifier()
            admin_id = os.getenv("TELEGRAM_ADMIN_ID", "")
            if admin_id:
                notifier.send_message(report_text, chat_id=admin_id)
                print("\n  [텔레그램] 리포트 발송 완료")
            else:
                print("\n  [텔레그램] TELEGRAM_ADMIN_ID 미설정, 발송 스킵")
        except Exception as e:
            logger.warning("[텔레그램] 발송 실패: %s", e)
            print(f"\n  [텔레그램] 발송 실패: {e}")

    return {
        "total_tickers": len(results),
        "analyzed": len(valid),
        "skipped": len(errors),
        "overfitted_count": len(overfitted),
        "update_recommended": len(updates),
        "report_text": report_text,
        "results": results,
    }


def main() -> None:
    """Walk-Forward 자동 재최적화 실행 + 결과 출력."""
    logging.disable(logging.CRITICAL)

    config = TradingConfig.from_env()

    summary = run_quarterly_reoptimization()
    results = summary["results"]

    # 상세 결과 테이블 출력
    print("\n")
    print("=" * 110)
    print("  종목별 Walk-Forward 분석 결과 (Train=18m, Test=6m)")
    print("=" * 110)
    print(
        f"  {'종목':>16} | {'유형':>4} | {'현재K':>5} | {'추천K':>5} | "
        f"{'Train Sharpe':>12} | {'Test Sharpe':>11} | {'하락률':>6} | "
        f"{'현재K Test':>10} | {'개선':>6} | {'판정':>8}"
    )
    print(
        f"  {'-'*16}-+-{'-'*4}-+-{'-'*5}-+-{'-'*5}-+-"
        f"{'-'*12}-+-{'-'*11}-+-{'-'*6}-+-{'-'*10}-+-{'-'*6}-+-{'-'*8}"
    )

    for r in results:
        if r.get("error"):
            print(f"  {r.get('ticker_code', '?'):>16} | {'SKIP':>4} | {r['error']}")
            continue

        ticker_type = "ETF" if r["is_etf"] else "개별주"

        if r["overfitted"]:
            verdict = "OVERFIT"
        elif r["should_update"]:
            verdict = "UPDATE"
        else:
            verdict = "OK"

        print(
            f"  {r['name']:>16} | {ticker_type:>4} | {r['current_k']:>5.1f} | {r['train_best_k']:>5.1f} | "
            f"{r['train_sharpe']:>12.2f} | {r['test_sharpe']:>11.2f} | "
            f"{r['sharpe_drop_pct']:>5.1f}% | "
            f"{r['current_test_sharpe']:>10.2f} | "
            f"{r['improvement_pct']:>+5.1f}% | {verdict:>8}"
        )

    # 요약
    valid = [r for r in results if not r.get("error")]
    overfitted = [r for r in valid if r.get("overfitted")]
    updates = [r for r in valid if r.get("should_update")]

    print(f"\n  분석: {len(valid)}/{len(results)}종목 | "
          f"과적합 경고: {len(overfitted)}종목 | "
          f"K값 변경 추천: {len(updates)}종목")

    if overfitted:
        print("\n  [과적합 경고 종목]")
        for r in overfitted:
            print(f"    - {r['name']}: Train={r['train_sharpe']:.2f}, "
                  f"Test={r['test_sharpe']:.2f} (하락 {r['sharpe_drop_pct']:.0f}%)")

    if updates:
        print("\n  [K값 변경 추천]")
        for r in updates:
            print(f"    - {r['name']}: K {r['current_k']} -> {r['recommended_k']} "
                  f"(Sharpe +{r['improvement_pct']:.0f}%)")
    else:
        print("\n  현재 K값 유지 권장")

    # ETF / 개별주 평균
    etf_valid = [r for r in valid if r["is_etf"] and not r["overfitted"]]
    stock_valid = [r for r in valid if not r["is_etf"] and not r["overfitted"]]

    if etf_valid:
        avg_k = sum(r["train_best_k"] for r in etf_valid) / len(etf_valid)
        avg_test_sharpe = sum(r["test_sharpe"] for r in etf_valid) / len(etf_valid)
        print(f"\n  ETF 평균 추천 K: {avg_k:.2f} (현재: {config.vb_k}), "
              f"평균 Test Sharpe: {avg_test_sharpe:.2f}")

    if stock_valid:
        avg_k = sum(r["train_best_k"] for r in stock_valid) / len(stock_valid)
        avg_test_sharpe = sum(r["test_sharpe"] for r in stock_valid) / len(stock_valid)
        print(f"  개별주 평균 추천 K: {avg_k:.2f} (현재: {config.vb_k_individual}), "
              f"평균 Test Sharpe: {avg_test_sharpe:.2f}")


if __name__ == "__main__":
    main()
