"""추세추종 MA 조합 비교 백테스트 (옵션 A, 2026-04-17 작성).

목적:
    현재 TrendStrategy는 MA5/MA20/MA60 고정. 다른 조합과 수익/승률/회전/MDD 비교.
    MOCK 운영 데이터(옵션 B)와 대조해 최종 선택 근거 마련.

방식:
    - 골든크로스(매수): MA_fast 가 MA_mid 를 상향 돌파 + 2일 확인 + 종가 > MA_slow
    - 데드크로스(매도): MA_fast 가 MA_mid 를 하향 돌파 + 2일 확인
    - 손절 -2% (ATR 생략, 비교 단순화)
    - 왕복 비용 (CostModel) 반영

실행:
    py backtest/compare_ma_combos.py > backtest/results/ma_combos_20260417.txt
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Windows 한글 콘솔 대응
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pandas as pd
import yfinance as yf

from backtest.cost_model import CostModel
from config.trading_config import TradingConfig


# ─────────────────────────────────────────────────────────────
# 1. 백테스트 엔진 (MA 파라미터화)
# ─────────────────────────────────────────────────────────────

def run_trend(
    code: str,
    df: pd.DataFrame,
    ma_fast: int,
    ma_mid: int,
    ma_slow: int,
    cost: CostModel,
    stoploss_pct: float = 2.0,
    initial_capital: int = 1_000_000,
) -> dict:
    """순수 추세추종 백테스트.

    Returns:
        {"total_return_pct", "win_rate_pct", "trades", "max_dd_pct", "turnover"}
    """
    df = df.copy().reset_index(drop=True)
    df[f"ma{ma_fast}"] = df["close"].rolling(ma_fast).mean()
    df[f"ma{ma_mid}"] = df["close"].rolling(ma_mid).mean()
    df[f"ma{ma_slow}"] = df["close"].rolling(ma_slow).mean()

    cash = initial_capital
    shares = 0
    buy_price = 0
    buy_cash = 0  # 매수 시점 투입 현금
    trades = []
    equity_curve = [initial_capital]
    turnover_sum = 0  # 회전율 = 총 거래대금 / 초기 자본

    warmup = max(ma_slow, 3) + 2  # MA 웜업 + 2일 확인용

    for i in range(warmup, len(df)):
        row_today = df.iloc[i]
        row_yesterday = df.iloc[i - 1]
        row_day_before = df.iloc[i - 2]

        close_today = float(row_today["close"])
        fast_t = row_today[f"ma{ma_fast}"]
        mid_t = row_today[f"ma{ma_mid}"]
        slow_t = row_today[f"ma{ma_slow}"]
        fast_y = row_yesterday[f"ma{ma_fast}"]
        mid_y = row_yesterday[f"ma{ma_mid}"]
        fast_db = row_day_before[f"ma{ma_fast}"]
        mid_db = row_day_before[f"ma{ma_mid}"]

        if pd.isna(fast_t) or pd.isna(mid_t) or pd.isna(slow_t):
            equity_curve.append(cash + shares * close_today)
            continue

        # 현재 평가액
        current_equity = cash + shares * close_today

        # 매도 판단 (보유 중일 때만)
        if shares > 0:
            loss_pct = (close_today - buy_price) / buy_price * 100
            # 손절
            if loss_pct <= -stoploss_pct:
                gross = shares * close_today
                net = gross * (1 - cost.roundtrip_cost_pct(buy_price, code) / 100)
                pnl = net - buy_cash
                trades.append({"pnl": pnl, "reason": "stop"})
                cash += gross  # 실제 체결액
                turnover_sum += gross
                shares = 0
                buy_price = 0
                buy_cash = 0
            # 데드크로스 (2일 확인)
            elif (fast_db >= mid_db and fast_y < mid_y and fast_t < mid_t):
                gross = shares * close_today
                net = gross * (1 - cost.roundtrip_cost_pct(buy_price, code) / 100)
                pnl = net - buy_cash
                trades.append({"pnl": pnl, "reason": "dead_cross"})
                cash += gross
                turnover_sum += gross
                shares = 0
                buy_price = 0
                buy_cash = 0

        # 매수 판단 (공백 슬롯일 때만)
        elif shares == 0:
            # 골든크로스 (2일 확인) + 종가 > MA_slow
            if (fast_db <= mid_db and fast_y > mid_y and fast_t > mid_t
                    and close_today > slow_t):
                qty = cash // close_today
                if qty > 0:
                    gross = qty * close_today
                    shares = qty
                    buy_price = close_today
                    buy_cash = gross  # 매수 비용 (수수료 제외 원가)
                    cash -= gross
                    turnover_sum += gross

        equity_curve.append(cash + shares * close_today)

    # 마지막 보유분 청산 (평가만)
    if shares > 0:
        last_close = float(df.iloc[-1]["close"])
        gross = shares * last_close
        net = gross * (1 - cost.roundtrip_cost_pct(buy_price, code) / 100)
        pnl = net - buy_cash
        trades.append({"pnl": pnl, "reason": "final"})
        cash += gross
        turnover_sum += gross

    # 통계
    final_equity = cash
    total_return_pct = (final_equity - initial_capital) / initial_capital * 100

    wins = sum(1 for t in trades if t["pnl"] > 0)
    win_rate_pct = wins / len(trades) * 100 if trades else 0.0

    # MDD
    peak = equity_curve[0]
    mdd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        dd = (peak - v) / peak * 100
        mdd = max(mdd, dd)

    turnover = turnover_sum / initial_capital  # 회전율 (초기자본 대비 거래대금 배수)

    return {
        "total_return_pct": total_return_pct,
        "win_rate_pct": win_rate_pct,
        "trades": len(trades),
        "max_dd_pct": mdd,
        "turnover": turnover,
    }


# ─────────────────────────────────────────────────────────────
# 2. 데이터 다운로드
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
    return df[["datetime", "open", "high", "low", "close", "volume"]]


# ─────────────────────────────────────────────────────────────
# 3. 실행 & 리포트
# ─────────────────────────────────────────────────────────────

MA_COMBOS = [
    (5, 10, 30),     # 초단기 (스캘핑형)
    (5, 20, 60),     # 현재 설정 (교과서)
    (10, 20, 60),    # 중단기 (느린 신호)
    (10, 30, 90),    # 스윙형
    (20, 60, 120),   # 중장기
]

TICKERS = {
    "069500.KS": "KODEX200",
    "229200.KS": "KODEX코스닥150",
    "005930.KS": "삼성전자",
}


def main() -> None:
    config = TradingConfig.from_env()
    cost = CostModel(config)

    print("=" * 75)
    print("  추세추종 MA 조합 비교 백테스트 — 최근 3년")
    print("  (골든/데드크로스 + 2일확인 + MA_slow 필터 + 손절 -2%)")
    print("=" * 75)
    print()

    # 종목별 데이터 먼저 다운로드
    data: dict[str, pd.DataFrame] = {}
    for yf_ticker, name in TICKERS.items():
        df = download(yf_ticker, period="3y")
        if len(df) < 150:
            print(f"  ⚠️ {name}: 데이터 부족 ({len(df)}개)")
            continue
        data[yf_ticker] = df

    # 결과 누적
    summary = {combo: [] for combo in MA_COMBOS}

    for yf_ticker, df in data.items():
        name = TICKERS[yf_ticker]
        code = yf_ticker.split(".")[0]
        print(f"\n### {name} ({code}) — {len(df)}일")
        print(f"  {'MA 조합':<14} {'수익률':>8} {'승률':>6} {'거래':>4} {'MDD':>7} {'회전':>6}")
        print("  " + "-" * 55)

        # Buy & Hold 비교치
        first_p = float(df.iloc[0]["close"])
        last_p = float(df.iloc[-1]["close"])
        bnh = (last_p - first_p) / first_p * 100

        for combo in MA_COMBOS:
            ma_fast, ma_mid, ma_slow = combo
            stats = run_trend(code, df, ma_fast, ma_mid, ma_slow, cost)
            marker = "★" if combo == (5, 20, 60) else " "
            print(
                f"  {marker} ({ma_fast:>2},{ma_mid:>2},{ma_slow:>3}) "
                f"{stats['total_return_pct']:>+7.1f}% "
                f"{stats['win_rate_pct']:>5.0f}% "
                f"{stats['trades']:>4} "
                f"{stats['max_dd_pct']:>6.1f}% "
                f"{stats['turnover']:>5.1f}x"
            )
            summary[combo].append(stats)

        print(f"    [B&H 참고치] {bnh:+.1f}%")

    # 종합 평균
    print()
    print("=" * 75)
    print("  종합 (3종목 평균)")
    print("=" * 75)
    print(f"  {'MA 조합':<14} {'평균수익률':>10} {'평균승률':>8} {'평균거래':>8} {'평균MDD':>8} {'회전율':>7}")
    print("  " + "-" * 60)

    ranked = []
    for combo in MA_COMBOS:
        stats_list = summary[combo]
        if not stats_list:
            continue
        avg_ret = sum(s["total_return_pct"] for s in stats_list) / len(stats_list)
        avg_win = sum(s["win_rate_pct"] for s in stats_list) / len(stats_list)
        avg_trd = sum(s["trades"] for s in stats_list) / len(stats_list)
        avg_mdd = sum(s["max_dd_pct"] for s in stats_list) / len(stats_list)
        avg_tov = sum(s["turnover"] for s in stats_list) / len(stats_list)
        ranked.append((combo, avg_ret, avg_win, avg_trd, avg_mdd, avg_tov))

    ranked.sort(key=lambda x: x[1], reverse=True)  # 수익률 내림차순

    for i, (combo, avg_ret, avg_win, avg_trd, avg_mdd, avg_tov) in enumerate(ranked, 1):
        ma_fast, ma_mid, ma_slow = combo
        marker = "★" if combo == (5, 20, 60) else " "
        print(
            f"  {i}위{marker} ({ma_fast:>2},{ma_mid:>2},{ma_slow:>3}) "
            f"{avg_ret:>+9.1f}% "
            f"{avg_win:>7.0f}% "
            f"{avg_trd:>7.1f} "
            f"{avg_mdd:>7.1f}% "
            f"{avg_tov:>6.1f}x"
        )

    print()
    print("  ★ = 현재 운영 설정 (MA5/MA20/MA60)")
    print()


if __name__ == "__main__":
    main()
