"""VB 전략 파라미터 그리드 서치 — 여러 조합 자동 스윕.

목적:
    K값·손절폭·트레일링 등 핵심 파라미터 조합을 자동으로 백테스트하여
    수익률·안정성·승률·비용 효율의 종합 점수 상위 조합을 선별.

사용:
    py backtest/grid_search.py
    py backtest/grid_search.py --tickers 229200.KS --period 1y   # 특정 종목
    py backtest/grid_search.py --small                            # 빠른 테스트 (소규모)

결과:
    backtest/results/grid_search_YYYYMMDD.csv  (전체 조합 순위)
    backtest/results/grid_search_YYYYMMDD.txt  (상위 10개 리포트)
"""

from __future__ import annotations

import argparse
import itertools
import sys
from dataclasses import replace
from datetime import datetime
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


# ─────────────────────────────────────────────────────────────
# 1. 파라미터 그리드 정의
# ─────────────────────────────────────────────────────────────

# 전체 탐색 범위 (기본)
PARAM_GRID_FULL = {
    "vb_k": [0.3, 0.5, 0.7, 1.0],
    "stoploss_pct": [1.5, 2.0, 2.5, 3.0],
    "trailing_activate_pct": [2.0, 2.5, 3.0],
    "trailing_stop_pct": [0.5, 1.0, 1.5],
}

# 소규모 (빠른 테스트용)
PARAM_GRID_SMALL = {
    "vb_k": [0.3, 0.5, 0.7],
    "stoploss_pct": [1.5, 2.0, 2.5],
    "trailing_activate_pct": [2.0, 2.5],
    "trailing_stop_pct": [0.5, 1.0],
}

TICKERS_DEFAULT = {
    "069500.KS": "KODEX200",
    "229200.KS": "KODEX코스닥150",
    "005930.KS": "삼성전자",
}


# ─────────────────────────────────────────────────────────────
# 2. 종합 점수 계산
# ─────────────────────────────────────────────────────────────

def compute_score(stats: dict) -> float:
    """종합 점수 = 수익률 40% + 안정성 30% + 승률 20% + 비용 효율 10%.

    각 지표를 0~100 스케일로 정규화 후 가중 합산. 0 미만 방지.
    """
    ret = stats.get("total_return_pct", 0.0)
    mdd = max(stats.get("max_drawdown_pct", 100.0), 0.01)
    win = stats.get("win_rate_pct", 0.0)
    trades = stats.get("total_trades", 0)

    # 수익률: -50~+200을 0~100으로
    ret_score = max(0, min(100, (ret + 50) / 250 * 100))

    # 안정성: MDD 0~40을 100~0으로 (낮을수록 좋음)
    stab_score = max(0, 100 - mdd / 40 * 100)

    # 승률: 0~100 그대로
    win_score = win

    # 비용 효율: 거래 수 적을수록 좋음 (0~100거래 → 100~0)
    cost_score = max(0, 100 - trades / 100 * 100)

    total = (ret_score * 0.4 + stab_score * 0.3 + win_score * 0.2 + cost_score * 0.1)
    return round(total, 2)


# ─────────────────────────────────────────────────────────────
# 3. 데이터 다운로드
# ─────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────
# 4. 그리드 서치 실행
# ─────────────────────────────────────────────────────────────

def run_grid_search(
    param_grid: dict,
    tickers: dict,
    period: str = "3y",
) -> pd.DataFrame:
    """모든 파라미터 조합 × 종목 백테스트 후 결과 DataFrame 반환."""
    base_config = TradingConfig.from_env()

    # 종목별 데이터 미리 로드
    print(f"\n📥 데이터 다운로드 중 ({period})...")
    data = {}
    for yf_ticker, name in tickers.items():
        df = download(yf_ticker, period=period)
        if len(df) < 70:
            print(f"  ⚠️ {name}: 데이터 부족 ({len(df)}일)")
            continue
        data[yf_ticker] = (df, name)
        print(f"  ✅ {name}: {len(df)}일")

    # 조합 생성
    keys = list(param_grid.keys())
    combos = list(itertools.product(*[param_grid[k] for k in keys]))
    total_runs = len(combos) * len(data)

    print(f"\n🔄 그리드 서치 시작: {len(combos)}개 조합 × {len(data)}종목 = {total_runs}회")
    print()

    results = []
    run_idx = 0

    for combo in combos:
        params = dict(zip(keys, combo))
        config = replace(base_config, **params)
        bt = BacktesterV2(config)

        for yf_ticker, (df, name) in data.items():
            run_idx += 1
            code = yf_ticker.split(".")[0]
            try:
                stats = bt.run_vb(code, df)
            except Exception as e:
                print(f"  ❌ {run_idx}/{total_runs} {params} / {name}: {e}")
                continue

            score = compute_score(stats)
            row = {
                "ticker": name,
                **params,
                "return_pct": round(stats["total_return_pct"], 2),
                "win_rate": round(stats["win_rate_pct"], 1),
                "trades": stats["total_trades"],
                "mdd_pct": round(stats["max_drawdown_pct"], 2),
                "profit_factor": round(stats.get("profit_factor", 0), 2),
                "sharpe": round(stats.get("sharpe_ratio", 0), 2),
                "score": score,
            }
            results.append(row)

            if run_idx % 20 == 0 or run_idx == total_runs:
                print(f"  진행: {run_idx}/{total_runs} ({run_idx / total_runs * 100:.0f}%)")

    return pd.DataFrame(results)


# ─────────────────────────────────────────────────────────────
# 5. 리포트 생성
# ─────────────────────────────────────────────────────────────

def generate_report(df: pd.DataFrame, output_path: Path) -> None:
    """상위 10개 + 종목별 Top 3 리포트."""
    lines = []
    lines.append("=" * 85)
    lines.append(f"  VB 파라미터 그리드 서치 결과 — {datetime.now():%Y-%m-%d %H:%M}")
    lines.append("=" * 85)
    lines.append("")

    # 종합 평균 (종목 3개 평균으로 조합 성과 판정)
    param_cols = [c for c in df.columns if c in ["vb_k", "stoploss_pct",
                                                  "trailing_activate_pct", "trailing_stop_pct"]]
    agg = df.groupby(param_cols).agg(
        avg_return=("return_pct", "mean"),
        avg_win_rate=("win_rate", "mean"),
        avg_trades=("trades", "mean"),
        avg_mdd=("mdd_pct", "mean"),
        avg_score=("score", "mean"),
    ).reset_index().sort_values("avg_score", ascending=False)

    lines.append("## 🏆 종합 순위 (3종목 평균)")
    lines.append("")
    lines.append(f"  {'순위':<4} {'K':>4} {'손절':>5} {'트레일활성':>8} {'트레일폭':>7} "
                 f"{'수익률':>8} {'승률':>6} {'거래':>5} {'MDD':>6} {'점수':>6}")
    lines.append("  " + "-" * 80)

    for i, (_, row) in enumerate(agg.head(10).iterrows(), 1):
        marker = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else f"{i}위"))
        lines.append(
            f"  {marker:<4} {row['vb_k']:>4} {row['stoploss_pct']:>4.1f}% "
            f"{row['trailing_activate_pct']:>7.1f}% {row['trailing_stop_pct']:>6.1f}% "
            f"{row['avg_return']:>+7.1f}% {row['avg_win_rate']:>5.0f}% "
            f"{row['avg_trades']:>5.0f} {row['avg_mdd']:>5.1f}% {row['avg_score']:>6.1f}"
        )

    lines.append("")

    # 종목별 Top 3
    lines.append("## 📊 종목별 Top 3")
    for ticker_name in df["ticker"].unique():
        sub = df[df["ticker"] == ticker_name].sort_values("score", ascending=False).head(3)
        lines.append("")
        lines.append(f"### {ticker_name}")
        for i, (_, row) in enumerate(sub.iterrows(), 1):
            lines.append(
                f"  {i}. K={row['vb_k']} 손절={row['stoploss_pct']}% "
                f"트레일={row['trailing_activate_pct']}%/{row['trailing_stop_pct']}% "
                f"→ 수익 {row['return_pct']:+.1f}%, 승률 {row['win_rate']:.0f}%, "
                f"MDD {row['mdd_pct']:.1f}%, 점수 {row['score']:.1f}"
            )

    lines.append("")
    lines.append("=" * 85)
    lines.append("  종합 점수 = 수익률 40% + 안정성 30% + 승률 20% + 비용 효율 10%")
    lines.append("  현재 .env: vb_k=0.5, stoploss_pct=2.0, trailing=2.5%/1.0%")
    lines.append("=" * 85)

    report = "\n".join(lines)
    output_path.write_text(report, encoding="utf-8")
    print("\n" + report)


# ─────────────────────────────────────────────────────────────
# 6. 메인
# ─────────────────────────────────────────────────────────────

def sensitivity_analysis(df: pd.DataFrame, base_params: dict) -> None:
    """파라미터 민감도 분석: 기준값 ±20%에서 수익률 변화 기울기 측정.

    변화량이 크면 그 파라미터는 "불안정" (과적합 위험 ↑).
    변화량이 작으면 그 파라미터는 "안정" (신뢰 가능 ↑).
    """
    print("\n" + "=" * 85)
    print("  🎯 파라미터 민감도 분석 (기준값 ±20%)")
    print(f"  기준: K={base_params['vb_k']} / 손절={base_params['stoploss_pct']}% / "
          f"트레일={base_params['trailing_activate_pct']}%→{base_params['trailing_stop_pct']}%")
    print("=" * 85)

    param_cols = list(base_params.keys())
    print(f"\n  {'파라미터':<24} {'기준값':>8} {'-20%':>8} {'-10%':>8} {'+10%':>8} {'+20%':>8} {'민감도':>8} {'판정':<8}")
    print("  " + "-" * 90)

    base_stats = df[df[param_cols].eq(pd.Series(base_params)).all(axis=1)]
    if base_stats.empty:
        print("  ⚠️ 기준값 조합이 grid_search 결과에 없음")
        return
    base_score = float(base_stats["score"].mean())

    for col in param_cols:
        base_val = base_params[col]
        # 각 -20/-10/+10/+20% 대응 값 찾기 (이웃 값)
        unique_vals = sorted(df[col].unique())
        if base_val not in unique_vals:
            continue
        idx = unique_vals.index(base_val)

        # 평균 점수로 민감도 계산
        def get_score_at(val):
            sub = df[df[col] == val]
            if sub.empty:
                return None
            return float(sub["score"].mean())

        # 이웃 값들 (최대 ±2 슬롯)
        vals_around = []
        for off in [-2, -1, 0, 1, 2]:
            target_idx = idx + off
            if 0 <= target_idx < len(unique_vals):
                val = unique_vals[target_idx]
                score = get_score_at(val)
                vals_around.append((val, score))

        if len(vals_around) < 3:
            continue

        # 민감도 = (최고점수 - 최저점수) / 기준점수
        scores = [s for _, s in vals_around if s is not None]
        if not scores:
            continue
        sensitivity = (max(scores) - min(scores)) / base_score * 100 if base_score > 0 else 0

        # 판정
        if sensitivity < 5:
            verdict = "안정 ✅"
        elif sensitivity < 15:
            verdict = "보통 🟡"
        else:
            verdict = "민감 🔴"

        # 출력
        line = f"  {col:<24} {base_val:>8}"
        for val, score in vals_around:
            if val == base_val:
                continue
            if score is None:
                line += f" {'-':>8}"
            else:
                line += f" {score:>7.1f}"
        line += f" {sensitivity:>7.1f}% {verdict}"
        print(line)

    print("\n  💡 판정 기준:")
    print("     안정 (<5%)   — 기준값에서 살짝 바꿔도 결과 비슷 → 신뢰 가능")
    print("     보통 (5~15%) — 조정 여지 있음, 튜닝 시 확인")
    print("     민감 (>15%)  — 파라미터 조금만 바꿔도 결과 크게 달라짐 → 과적합 의심")
    print("=" * 85)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="종목 지정 (기본: KODEX200 + 코스닥150 + 삼성전자)")
    parser.add_argument("--period", default="3y",
                        help="기간 (yfinance period, 기본 3y)")
    parser.add_argument("--small", action="store_true",
                        help="소규모 그리드 (빠른 테스트)")
    parser.add_argument("--sensitivity", action="store_true",
                        help="민감도 분석 실행 (기존 결과 + ±20% 주변 비교)")
    args = parser.parse_args()

    grid = PARAM_GRID_SMALL if args.small else PARAM_GRID_FULL
    tickers = (
        {t: t.split(".")[0] for t in args.tickers}
        if args.tickers
        else TICKERS_DEFAULT
    )

    df = run_grid_search(grid, tickers, period=args.period)

    # 결과 저장
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y%m%d")
    csv_path = results_dir / f"grid_search_{today}.csv"
    txt_path = results_dir / f"grid_search_{today}.txt"

    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    generate_report(df, txt_path)

    print(f"\n📁 저장: {csv_path.name} / {txt_path.name}")

    # 9-C 감도 분석 (옵션)
    if args.sensitivity:
        base_config = TradingConfig.from_env()
        base_params = {
            "vb_k": base_config.vb_k,
            "stoploss_pct": base_config.stoploss_pct,
            "trailing_activate_pct": base_config.trailing_activate_pct,
            "trailing_stop_pct": base_config.trailing_stop_pct,
        }
        sensitivity_analysis(df, base_params)


if __name__ == "__main__":
    main()
