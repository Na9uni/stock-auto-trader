"""2020 코로나 급락장 스트레스 테스트 (LIVE 전환 체크리스트 항목).

2020-02 ~ 2020-06 급락·반등 구간에서 시스템 생존·수익 검증.
2022 하락장(완만)보다 훨씬 급격 → 최악의 시나리오 시뮬.

사용:
    py backtest/stress_covid.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pandas as pd
import yfinance as yf

from backtest.backtester_v2 import BacktesterV2
from config.trading_config import TradingConfig


TICKERS = {
    "069500.KS": "KODEX200",
    "229200.KS": "KODEX코스닥150",
    "005930.KS": "삼성전자",
    "133690.KS": "TIGER나스닥100",
}

# 코로나 주요 구간
PERIODS = [
    ("2020-02-01", "2020-03-31", "급락 구간 (-35%)"),
    ("2020-03-20", "2020-06-30", "반등 구간 (+40%)"),
    ("2020-02-01", "2020-06-30", "급락+반등 전체"),
    ("2020-01-01", "2020-12-31", "2020년 전체 (V자 회복)"),
]


def download(ticker: str, start: str, end: str) -> pd.DataFrame:
    raw = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
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


def main() -> None:
    config = TradingConfig.from_env()
    bt = BacktesterV2(config)

    print("=" * 85)
    print("  🦠 2020 코로나 급락장 스트레스 테스트")
    print(f"  설정: K={config.vb_k}(ETF)/{config.vb_k_individual}(개별주), "
          f"손절={config.stoploss_pct}%, 트레일={config.trailing_activate_pct}%→{config.trailing_stop_pct}%")
    print("=" * 85)

    for start, end, label in PERIODS:
        print(f"\n## {label} ({start} ~ {end})")
        print(f"  {'종목':<16} {'수익률':>8} {'BnH':>8} {'승률':>6} {'거래':>5} {'MDD':>7} {'Sharpe':>7}")
        print("  " + "-" * 75)

        total_pnl = 0
        survived = 0
        for yf_ticker, name in TICKERS.items():
            df = download(yf_ticker, start, end)
            if len(df) < 15:
                print(f"  {name:<16} 데이터 부족 ({len(df)}일)")
                continue

            code = yf_ticker.split(".")[0]
            try:
                stats = bt.run_vb(code, df)
            except Exception as e:
                print(f"  {name:<16} 오류: {e}")
                continue

            first_p = int(df.iloc[0]["close"])
            last_p = int(df.iloc[-1]["close"])
            bnh = (last_p - first_p) / first_p * 100

            total_pnl += stats["final_capital"] - 1_000_000
            if stats["total_return_pct"] > -10:
                survived += 1

            marker = "✅" if stats["total_return_pct"] > -10 else ("⚠️" if stats["total_return_pct"] > -20 else "❌")
            print(
                f"  {marker} {name:<14} "
                f"{stats['total_return_pct']:>+7.1f}% "
                f"{bnh:>+7.1f}% "
                f"{stats['win_rate_pct']:>5.0f}% "
                f"{stats['total_trades']:>5} "
                f"{stats['max_drawdown_pct']:>6.1f}% "
                f"{stats['sharpe_ratio']:>6.2f}"
            )

        print(f"\n  🏁 합산 손익: {total_pnl:+,}원 | 생존 종목(>-10%): {survived}/{len(TICKERS)}")

    print("\n" + "=" * 85)
    print("  판정 기준:")
    print("  ✅ 전략 > -10%   : LIVE 투입 가능 수준")
    print("  ⚠️  전략 > -20%   : 조건부 허용 (파라미터 재검토)")
    print("  ❌ 전략 < -20%   : LIVE 전환 금지 (시스템 재설계)")
    print()
    print("  ※ 2020 코로나는 V자 회복이라 BnH가 유리. 추세추종은 하락장 방어 중심 평가.")
    print("=" * 85)


if __name__ == "__main__":
    main()
