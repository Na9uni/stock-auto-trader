"""몬테카를로 시뮬레이션 — 전략의 운 vs 실력 통계 검증 (TODO 9-A).

백테스트 결과(trades 시계열)를 N회 무작위 재배열하여 다양한 손실 시나리오 분포 계산.
실제 운영 순서가 아닌 다른 순서로 체결됐어도 비슷한 결과인지 통계 검증.

목적:
- "이 전략이 운 좋아 번 건지, 진짜 실력인지" 통계적 신뢰도
- 95%/99% VaR (Value at Risk) 측정
- 최악 시나리오 MDD 분포

사용:
    py backtest/monte_carlo.py
    py backtest/monte_carlo.py --sims 10000 --ticker 229200.KS
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import pandas as pd
import yfinance as yf

from backtest.backtester_v2 import BacktesterV2
from config.trading_config import TradingConfig


def download(ticker: str, period: str = "3y") -> pd.DataFrame:
    raw = yf.download(ticker, period=period, auto_adjust=True, progress=False)
    if raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                            "Close": "close", "Volume": "volume"})
    df = df.reset_index()
    date_col = [c for c in df.columns if "date" in str(c).lower()]
    if date_col:
        df = df.rename(columns={date_col[0]: "datetime"})
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y%m%d")
    return df[["datetime", "open", "high", "low", "close", "volume"]]


def extract_returns_from_backtest(ticker_code: str, df: pd.DataFrame, config: TradingConfig) -> np.ndarray:
    """백테스트 실행 후 각 거래의 수익률(%)만 추출."""
    bt = BacktesterV2(config)
    stats = bt.run_vb(ticker_code, df)
    trades_all = stats.get("trades_detail", [])
    # sell 거래만 (pnl 포함)
    sell_trades = [t for t in trades_all if t.get("side") == "sell"]
    if not sell_trades:
        return np.array([])
    initial = 1_000_000
    returns_pct = []
    for t in sell_trades:
        pnl = t.get("pnl", 0)
        # 각 거래의 수익률 (초기 자본 대비)
        returns_pct.append(pnl / initial * 100)
    return np.array(returns_pct)


def monte_carlo(returns: np.ndarray, n_sims: int = 5000) -> dict:
    """수천 번 순서 재배열하여 MDD/총수익률 분포 계산.

    Args:
        returns: 각 거래의 수익률 배열 (%)
        n_sims: 시뮬레이션 횟수

    Returns:
        dict: mdd 분포, 총수익률 분포, VaR 등
    """
    if len(returns) < 5:
        return {"error": f"거래 수 부족 ({len(returns)}건) — 최소 5건 필요"}

    mdds = []
    total_returns = []
    worst_streaks = []

    for _ in range(n_sims):
        shuffled = np.random.permutation(returns)
        # 복리 누적 equity curve
        equity = np.cumprod(1 + shuffled / 100)
        # MDD
        peak = np.maximum.accumulate(equity)
        dd = (peak - equity) / peak * 100
        mdds.append(float(np.max(dd)))
        # 총 수익률
        total_returns.append(float((equity[-1] - 1) * 100))
        # 연속 손실 최대
        losses = shuffled < 0
        max_streak = 0
        current = 0
        for l in losses:
            current = current + 1 if l else 0
            max_streak = max(max_streak, current)
        worst_streaks.append(max_streak)

    mdds = np.array(mdds)
    total_returns = np.array(total_returns)
    worst_streaks = np.array(worst_streaks)

    return {
        "n_sims": n_sims,
        "n_trades": len(returns),
        "mdd": {
            "mean": float(np.mean(mdds)),
            "median": float(np.median(mdds)),
            "p95": float(np.percentile(mdds, 95)),
            "p99": float(np.percentile(mdds, 99)),
            "max": float(np.max(mdds)),
        },
        "total_return": {
            "mean": float(np.mean(total_returns)),
            "median": float(np.median(total_returns)),
            "p5": float(np.percentile(total_returns, 5)),
            "p95": float(np.percentile(total_returns, 95)),
            "prob_positive": float(np.sum(total_returns > 0) / n_sims * 100),
        },
        "worst_loss_streak": {
            "mean": float(np.mean(worst_streaks)),
            "p95": float(np.percentile(worst_streaks, 95)),
            "max": int(np.max(worst_streaks)),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sims", type=int, default=5000, help="시뮬레이션 횟수 (기본 5000)")
    parser.add_argument("--tickers", nargs="+", default=None, help="특정 종목 지정")
    parser.add_argument("--period", default="3y")
    args = parser.parse_args()

    config = TradingConfig.from_env()

    tickers = args.tickers or [
        ("069500.KS", "KODEX200"),
        ("229200.KS", "KODEX코스닥150"),
        ("005930.KS", "삼성전자"),
    ]
    if args.tickers:
        tickers = [(t, t.split(".")[0]) for t in args.tickers]

    print("=" * 85)
    print(f"  🎲 몬테카를로 시뮬레이션 — {args.sims}회 재배열")
    print(f"  설정: K={config.vb_k}, 손절={config.stoploss_pct}%, 트레일={config.trailing_activate_pct}%→{config.trailing_stop_pct}%")
    print("=" * 85)

    for yf_ticker, name in tickers:
        print(f"\n## {name} ({yf_ticker})")
        df = download(yf_ticker, args.period)
        if len(df) < 30:
            print(f"  ⚠️ 데이터 부족")
            continue

        code = yf_ticker.split(".")[0]
        returns = extract_returns_from_backtest(code, df, config)
        if len(returns) < 5:
            print(f"  ⚠️ 거래 수 부족: {len(returns)}건")
            continue

        result = monte_carlo(returns, args.sims)
        if "error" in result:
            print(f"  ❌ {result['error']}")
            continue

        mdd = result["mdd"]
        tr = result["total_return"]
        ws = result["worst_loss_streak"]

        print(f"  거래 수: {result['n_trades']} | 시뮬: {result['n_sims']:,}회")
        print(f"\n  📉 MDD 분포 (최대 낙폭)")
        print(f"     평균 {mdd['mean']:.1f}% | 중앙 {mdd['median']:.1f}% | 95% {mdd['p95']:.1f}% | 99% {mdd['p99']:.1f}% | 최악 {mdd['max']:.1f}%")
        print(f"\n  📈 총수익률 분포")
        print(f"     평균 {tr['mean']:+.1f}% | 중앙 {tr['median']:+.1f}% | 5% 하위 {tr['p5']:+.1f}% | 95% 상위 {tr['p95']:+.1f}%")
        print(f"     승률 (수익 > 0 확률): {tr['prob_positive']:.1f}%")
        print(f"\n  🔥 최대 연속 손실")
        print(f"     평균 {ws['mean']:.1f}회 | 95% {ws['p95']:.1f}회 | 최악 {ws['max']}회")

        # 판정
        worst_case = mdd["p99"]
        prob_profit = tr["prob_positive"]
        if worst_case < 20 and prob_profit > 80:
            verdict = "✅ 실력 우세 — 운의 영향 적음"
        elif worst_case < 30 and prob_profit > 65:
            verdict = "🟡 보통 — 어느 정도 운의 영향"
        else:
            verdict = "🔴 운 의존도 높음 — 파라미터 재검토 필요"
        print(f"\n  판정: {verdict}")

    print("\n" + "=" * 85)
    print("  💡 해석:")
    print("  - 99% MDD < 20% + 수익확률 > 80% → LIVE 투입 안전")
    print("  - 99% MDD < 30% + 수익확률 > 65% → 조건부 (파라미터 조정 고려)")
    print("  - 그 외 → 전략 재설계 또는 종목 재선정 필요")
    print()
    print("  ※ 백테스트 성공이 우연인지 실력인지 통계적으로 가늠 가능.")
    print("    MOCK 2주+ 후엔 실거래 데이터로 같은 분석 재수행 권장.")
    print("=" * 85)


if __name__ == "__main__":
    main()
