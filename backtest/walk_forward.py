"""Walk-Forward 검증 + K값 최적화.

IS(In-Sample) 구간에서 최적 K값을 찾고,
OOS(Out-of-Sample) 구간에서 검증한다.
과적합 방지의 핵심.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd

from backtest.backtester_v2 import BacktesterV2, download
from config.trading_config import TradingConfig


def optimize_k(
    bt: BacktesterV2,
    ticker: str,
    df: pd.DataFrame,
    k_range: list[float] | None = None,
) -> dict:
    """단일 구간에서 K값 그리드 서치.

    목표 함수: PF * sqrt(거래수) / (1 + MDD%)
    """
    if k_range is None:
        k_range = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]

    best_k = k_range[0]  # 검색 범위 내 첫 값으로 초기화
    best_score = -999
    results = []

    for k in k_range:
        config = TradingConfig.from_dict({"vb_k": k, "vb_k_individual": k})
        bt_k = BacktesterV2(config)
        stats = bt_k.run_vb(ticker, df)

        trades = stats.get("total_trades", 0)
        pf = stats.get("profit_factor", 0)
        mdd = stats.get("max_drawdown_pct", 0)

        # 최소 5거래 필요
        if trades < 5:
            score = -999
        else:
            score = pf * np.sqrt(trades) / (1 + mdd / 100)

        results.append({
            "k": k,
            "return": stats.get("total_return_pct", 0),
            "trades": trades,
            "win_rate": stats.get("win_rate_pct", 0),
            "pf": pf,
            "mdd": mdd,
            "score": round(score, 2),
        })

        if score > best_score:
            best_score = score
            best_k = k

    return {"best_k": best_k, "best_score": round(best_score, 2), "results": results}


def walk_forward(
    ticker_yf: str,
    ticker_code: str,
    is_days: int = 120,
    oos_days: int = 40,
    k_range: list[float] | None = None,
    total_period: str = "2y",
) -> dict:
    """Walk-Forward 검증.

    Args:
        ticker_yf: yfinance 티커 (e.g. "069500.KS")
        ticker_code: 종목코드 (e.g. "069500")
        is_days: In-Sample 거래일 수
        oos_days: Out-of-Sample 거래일 수
        k_range: 테스트할 K값 목록
        total_period: 전체 데이터 기간

    Returns:
        {windows, oos_returns, wfe, best_ks, avg_k}
    """
    df = download(ticker_yf, period=total_period)
    if len(df) < is_days + oos_days + 60:
        return {"error": f"데이터 부족: {len(df)}행 < {is_days + oos_days + 60}"}

    config = TradingConfig.from_env()
    bt = BacktesterV2(config)

    windows = []
    oos_returns = []
    is_returns = []
    best_ks = []

    start = 0
    window_num = 0

    while start + is_days + oos_days <= len(df):
        window_num += 1
        is_df = df.iloc[start:start + is_days].copy().reset_index(drop=True)
        # OOS: 지표 워밍업(60일)을 위해 IS 끝 60일 + OOS 구간을 합침
        warmup = 60
        oos_start = max(0, start + is_days - warmup)
        oos_df = df.iloc[oos_start:start + is_days + oos_days].copy().reset_index(drop=True)

        # IS: K값 최적화
        opt = optimize_k(bt, ticker_code, is_df, k_range)
        best_k = opt["best_k"]
        best_ks.append(best_k)

        # IS 수익률 (최적 K)
        config_is = TradingConfig.from_dict({"vb_k": best_k, "vb_k_individual": best_k})
        bt_is = BacktesterV2(config_is)
        is_stats = bt_is.run_vb(ticker_code, is_df)
        is_ret = is_stats.get("total_return_pct", 0)
        is_returns.append(is_ret)

        # OOS: 독립 실행. MA60 워밍업용 61일을 앞에 붙임.
        # 주의: 워밍업 구간에서 발생한 거래가 OOS 수익에 포함될 수 있음 (근사치).
        # run_vb가 range(61,...)부터 시작하므로 워밍업 구간 거래는 최소화됨.
        oos_pad = 61
        oos_start_raw = max(0, start + is_days - oos_pad)
        oos_df = df.iloc[oos_start_raw:start + is_days + oos_days].copy().reset_index(drop=True)

        if len(oos_df) < oos_pad + 10:
            oos_returns.append(0.0)
            windows.append({
                "window": window_num, "best_k": best_k,
                "is_return": round(is_ret, 2), "oos_return": 0.0,
                "oos_trades": 0, "oos_win_rate": 0,
            })
            start += oos_days
            continue

        bt_oos = BacktesterV2(config_is)
        oos_full_stats = bt_oos.run_vb(ticker_code, oos_df)
        oos_ret = oos_full_stats.get("total_return_pct", 0)
        oos_returns.append(oos_ret)

        windows.append({
            "window": window_num,
            "best_k": best_k,
            "is_return": round(is_ret, 2),
            "oos_return": round(oos_ret, 2),
            "oos_trades": oos_full_stats.get("total_trades", 0),  # 워밍업 포함이지만 근사치
            "oos_win_rate": oos_full_stats.get("win_rate_pct", 0),  # 근사치
        })

        start += oos_days

    # Walk-Forward Efficiency
    avg_is = np.mean(is_returns) if is_returns else 0
    avg_oos = np.mean(oos_returns) if oos_returns else 0
    wfe = avg_oos / avg_is if avg_is != 0 else 0
    avg_k = np.mean(best_ks) if best_ks else 0.5

    # OOS 수익 복리 계산 (단순 합산이 아닌 체이닝)
    compounded = 1.0
    for r in oos_returns:
        compounded *= (1 + r / 100)
    total_oos_compounded = (compounded - 1) * 100

    return {
        "windows": windows,
        "total_oos_return": round(total_oos_compounded, 2),
        "avg_oos_return": round(avg_oos, 2),
        "avg_is_return": round(avg_is, 2),
        "wfe": round(wfe, 2),
        "best_ks": best_ks,
        "avg_k": round(avg_k, 2),
        "recommended_k": round(avg_k, 1),
    }


def main():
    """주요 종목 Walk-Forward 검증."""
    tickers = {
        "069500.KS": ("069500", "KODEX200"),
        "229200.KS": ("229200", "KODEX코스닥150"),
        "005930.KS": ("005930", "삼성전자"),
        "034020.KS": ("034020", "두산에너빌"),
        "103590.KS": ("103590", "일진전기"),
    }

    for yf_ticker, (code, name) in tickers.items():
        print(f"\n{'='*55}")
        print(f"  {name} Walk-Forward (IS=120, OOS=40)")
        print(f"{'='*55}")

        result = walk_forward(yf_ticker, code, total_period="2y")

        if "error" in result:
            print(f"  {result['error']}")
            continue

        for w in result["windows"]:
            print(
                f"  W{w['window']}: K={w['best_k']:.1f} "
                f"IS={w['is_return']:+.1f}% "
                f"OOS={w['oos_return']:+.1f}% "
                f"({w['oos_trades']}거래, 승률{w['oos_win_rate']:.0f}%)"
            )

        print(f"\n  WFE: {result['wfe']:.2f} ", end="")
        if result["wfe"] >= 0.5:
            print("(안정적)")
        elif result["wfe"] >= 0.3:
            print("(주의)")
        else:
            print("(과적합 의심)")

        print(f"  K값 범위: {min(result['best_ks']):.1f} ~ {max(result['best_ks']):.1f}")
        print(f"  권장 K: {result['recommended_k']}")
        print(f"  OOS 누적 수익: {result['total_oos_return']:+.1f}%")


if __name__ == "__main__":
    main()
