"""VB 전략 고점 매수 회피 필터 비교 백테스트 (3 버전).

기존 VB (필터 없음) vs 엄격 필터 vs 완화 필터
동일 기간/종목/비용 모델로 직접 비교.

필터 버전:
- STRICT : 이격도 3% / 거래량 1.2x / RSI < 70
- LENIENT: 이격도 5% / 거래량 1.0x / RSI < 75
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import yfinance as yf

from backtest.backtester_v2 import BacktesterV2
from config.trading_config import TradingConfig


def download(ticker: str, period: str = "1y",
             start: str | None = None, end: str | None = None) -> pd.DataFrame:
    if start and end:
        raw = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
    else:
        raw = yf.download(ticker, period=period, auto_adjust=True, progress=False)
    if raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.reset_index()
    dc = [c for c in df.columns if "date" in str(c).lower()]
    if dc:
        df = df.rename(columns={dc[0]: "datetime"})
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y%m%d")
    return df[["datetime", "open", "high", "low", "close", "volume"]]


def main() -> None:
    config = TradingConfig.from_env()
    bt = BacktesterV2(config)

    tickers = {
        "069500.KS": "KODEX200",
        "229200.KS": "KODEX코스닥150",
        "131890.KS": "ACE삼성그룹동일",
        "108450.KS": "ACE삼성그룹섹터",
        "395160.KS": "KODEX AI반도체",
        "005930.KS": "삼성전자",
    }

    periods = [
        ("최근 2년", {"period": "2y"}),
        ("2022 하락장", {"start": "2022-01-01", "end": "2022-12-31"}),
        ("2023 회복장", {"start": "2023-01-01", "end": "2023-12-31"}),
    ]

    for label, period_args in periods:
        print(f"\n{'=' * 90}")
        print(f"  {label} - 3 버전 비교 (기존 / 엄격 / 완화)")
        print(f"{'=' * 90}")
        print(f"  {'종목':18s} {'수익%':>32s} {'거래수':>16s} {'승률%':>16s}")
        print(f"  {'':18s} {'기존 / 엄격 / 완화':>32s} {'기존/엄격/완화':>16s} {'기존/엄격/완화':>16s}")
        print(f"  {'-' * 82}")

        sum_basic = 0
        sum_strict = 0
        sum_lenient = 0
        tr_basic = 0
        tr_strict = 0
        tr_lenient = 0

        for yf_ticker, name in tickers.items():
            df = download(yf_ticker, **period_args)
            if len(df) < 70:
                print(f"  {name}: 데이터 부족")
                continue

            code = yf_ticker.split(".")[0]
            basic = bt.run_vb(code, df, use_high_point_filters=False)
            strict = bt.run_vb(
                code, df, use_high_point_filters=True,
                filter_ma10_deviation_max=0.03,
                filter_volume_ratio_min=1.2,
                filter_rsi_max=70.0,
            )
            lenient = bt.run_vb(
                code, df, use_high_point_filters=True,
                filter_ma10_deviation_max=0.05,
                filter_volume_ratio_min=1.0,
                filter_rsi_max=75.0,
            )

            sum_basic += basic["final_capital"] - 1_000_000
            sum_strict += strict["final_capital"] - 1_000_000
            sum_lenient += lenient["final_capital"] - 1_000_000
            tr_basic += basic["total_trades"]
            tr_strict += strict["total_trades"]
            tr_lenient += lenient["total_trades"]

            # 완화 vs 기존 차이로 marker
            delta = lenient["total_return_pct"] - basic["total_return_pct"]
            mark = "+" if delta > 0 else ("-" if delta < -2 else "=")

            print(
                f"  {mark} {name:16s} "
                f"{basic['total_return_pct']:>+7.1f} / {strict['total_return_pct']:>+7.1f} / {lenient['total_return_pct']:>+7.1f}   "
                f"{basic['total_trades']:>3d}/{strict['total_trades']:>3d}/{lenient['total_trades']:>3d}   "
                f"{basic['win_rate_pct']:>3.0f}/{strict['win_rate_pct']:>3.0f}/{lenient['win_rate_pct']:>3.0f}"
            )

        basic_pct = sum_basic / (6 * 1_000_000) * 100
        strict_pct = sum_strict / (6 * 1_000_000) * 100
        lenient_pct = sum_lenient / (6 * 1_000_000) * 100
        print(f"  {'-' * 82}")
        print(
            f"  {'합산':18s} "
            f"{basic_pct:>+7.1f} / {strict_pct:>+7.1f} / {lenient_pct:>+7.1f}   "
            f"{tr_basic:>3d}/{tr_strict:>3d}/{tr_lenient:>3d}"
        )
        print(f"  완화 vs 기존: {lenient_pct - basic_pct:+.1f}%p")


if __name__ == "__main__":
    main()
