"""듀얼 모멘텀 ETF 로테이션 전략.

학술 근거:
- Jegadeesh & Titman (1993): 모멘텀 효과 최초 문서화
- Gary Antonacci (2014): Dual Momentum — 상대 모멘텀 + 절대 모멘텀
- Meb Faber (2007): 10개월 MA 필터 = 하락장 회피
- AQR Capital: 모멘텀은 모든 자산군에서 작동하는 유일한 팩터

핵심 원리:
1. 상대 모멘텀: 여러 ETF 중 가장 강한 것을 산다
2. 절대 모멘텀: 그 종목조차 하락 추세면 현금 보유
3. 복합 스코어: 1/3/6/12개월 수익률 가중 평균 (최근 1개월 제외 = 단기 반전 효과 회피)
4. 월 1회 리밸런싱 = 연 12회 거래 = 비용 최소화

100만원에 최적인 이유:
- 거래 빈도 극도로 낮음 (월 1~2회)
- ETF 전용 = 세금 면제 + 슬리피지 최소
- 하락장에서 현금 전환 = 원금 보존
- 30년 이상 학술적으로 검증된 전략
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger("stock_analysis")


# ── 유니버스 ────────────────────────────────────────────────────────

# 한국 시장 ETF 유니버스 (yfinance 티커 → 키움 코드)
ETF_UNIVERSE = {
    "069500.KS": ("069500", "KODEX 200"),
    "229200.KS": ("229200", "KODEX 코스닥150"),
    "133690.KS": ("133690", "TIGER 미국나스닥100"),
    "360750.KS": ("360750", "TIGER 미국S&P500"),
    "371460.KS": ("371460", "TIGER 차이나전기차SOLACTIVE"),
    "395160.KS": ("395160", "KODEX AI반도체핵심장비"),
    "161510.KS": ("161510", "TIGER 단기채권액티브"),  # 현금 대용
}

# 현금 대용 ETF (절대 모멘텀 실패 시)
CASH_ETF = "161510.KS"


@dataclass
class MomentumScore:
    """모멘텀 점수 결과."""
    ticker: str
    name: str
    score: float           # 복합 모멘텀 점수
    ret_1m: float          # 1개월 수익률
    ret_3m: float          # 3개월 수익률
    ret_6m: float          # 6개월 수익률
    ret_12m: float         # 12개월 수익률
    above_ma: bool         # 10개월 MA 위에 있는지 (절대 모멘텀)
    volatility: float      # 연환산 변동성


def calc_momentum_score(prices: pd.Series) -> MomentumScore | None:
    """가격 시리즈에서 복합 모멘텀 점수 계산.

    Args:
        prices: 일별 종가 시리즈 (최소 252일)

    Returns:
        MomentumScore 또는 데이터 부족 시 None
    """
    if len(prices) < 252:
        return None

    current = prices.iloc[-1]

    # 수익률 계산 (최근 1개월은 skip — 단기 반전 효과 회피)
    # "skip month" = Jegadeesh & Titman의 발견
    skip = 21  # 1개월 ≈ 21 거래일

    price_1m_ago = prices.iloc[-skip - 21] if len(prices) > skip + 21 else prices.iloc[0]
    price_3m_ago = prices.iloc[-skip - 63] if len(prices) > skip + 63 else prices.iloc[0]
    price_6m_ago = prices.iloc[-skip - 126] if len(prices) > skip + 126 else prices.iloc[0]
    price_12m_ago = prices.iloc[-skip - 252] if len(prices) > skip + 252 else prices.iloc[0]
    price_skip = prices.iloc[-skip]  # skip 시점 가격

    ret_1m = (price_skip / price_1m_ago - 1) * 100 if price_1m_ago > 0 else 0
    ret_3m = (price_skip / price_3m_ago - 1) * 100 if price_3m_ago > 0 else 0
    ret_6m = (price_skip / price_6m_ago - 1) * 100 if price_6m_ago > 0 else 0
    ret_12m = (price_skip / price_12m_ago - 1) * 100 if price_12m_ago > 0 else 0

    # 복합 모멘텀 점수: 가중 평균 (단기에 약간 더 가중)
    # AQR 스타일: 12-1 모멘텀이 표준이지만, 복합이 더 안정적
    score = (ret_1m * 0.3 + ret_3m * 0.3 + ret_6m * 0.2 + ret_12m * 0.2)

    # 절대 모멘텀: 10개월(≈210거래일) 이동평균 위에 있는지
    ma_200 = prices.tail(210).mean()
    above_ma = current > ma_200

    # 연환산 변동성 (포지션 사이징용)
    daily_returns = prices.pct_change().dropna().tail(63)  # 최근 3개월
    volatility = daily_returns.std() * np.sqrt(252) * 100 if len(daily_returns) > 20 else 20.0

    return MomentumScore(
        ticker="", name="",
        score=round(score, 2),
        ret_1m=round(ret_1m, 2),
        ret_3m=round(ret_3m, 2),
        ret_6m=round(ret_6m, 2),
        ret_12m=round(ret_12m, 2),
        above_ma=above_ma,
        volatility=round(volatility, 2),
    )


def rank_universe(price_dict: dict[str, pd.Series]) -> list[MomentumScore]:
    """유니버스 전체의 모멘텀 순위를 매긴다.

    Args:
        price_dict: {ticker: prices_series}

    Returns:
        모멘텀 점수 내림차순 정렬된 리스트
    """
    scores = []
    for ticker, prices in price_dict.items():
        ms = calc_momentum_score(prices)
        if ms is None:
            continue
        ms.ticker = ticker
        info = ETF_UNIVERSE.get(ticker, ("", ticker))
        ms.name = info[1] if isinstance(info, tuple) else ticker
        scores.append(ms)

    return sorted(scores, key=lambda x: x.score, reverse=True)


def select_holdings(
    rankings: list[MomentumScore],
    max_holdings: int = 2,
    min_score: float = 0.0,
) -> list[MomentumScore]:
    """매수할 ETF 선정.

    규칙:
    1. 상대 모멘텀: 상위 max_holdings개 선정
    2. 절대 모멘텀: 10개월 MA 아래면 제외
    3. 최소 점수: score > min_score (0 이하면 하락 추세)
    4. 빈 슬롯은 현금(채권 ETF)으로 채움

    Returns:
        선정된 ETF 목록
    """
    selected = []
    for ms in rankings:
        if len(selected) >= max_holdings:
            break
        # 현금 대용 ETF는 랭킹에서 제외
        if ms.ticker == CASH_ETF:
            continue
        # 절대 모멘텀 필터
        if not ms.above_ma:
            logger.info("[모멘텀] %s: MA 아래 → 스킵 (score=%.1f)", ms.name, ms.score)
            continue
        # 최소 점수 필터
        if ms.score <= min_score:
            logger.info("[모멘텀] %s: 점수 %.1f ≤ %.1f → 스킵", ms.name, ms.score, min_score)
            continue
        selected.append(ms)

    # 빈 슬롯은 CASH_ETF(채권)로 채움 (데이터 존재 시에만)
    # backtest에서 CASH_ETF 데이터가 없으면 현금 보유 (매수 안 함)
    while len(selected) < max_holdings:
        cash_ms = MomentumScore(
            ticker=CASH_ETF, name="단기채권(현금)",
            score=0, ret_1m=0, ret_3m=0, ret_6m=0, ret_12m=0,
            above_ma=True, volatility=2.0,
        )
        selected.append(cash_ms)

    return selected


def filter_available(selected: list[MomentumScore], available_tickers: set[str]) -> list[MomentumScore]:
    """데이터가 없는 종목 제거."""
    return [ms for ms in selected if ms.ticker in available_tickers]


# ── 백테스트 ────────────────────────────────────────────────────────

def backtest_momentum_rotation(
    price_dict: dict[str, pd.DataFrame],
    capital: int = 1_000_000,
    max_holdings: int = 2,
    rebalance_days: int = 21,         # 리밸런스 간격 (거래일)
    commission_rate: float = 0.00015,  # 편도 0.015%
    slippage: float = 0.0005,          # 0.05% (ETF)
) -> dict:
    """듀얼 모멘텀 ETF 로테이션 백테스트.

    Args:
        price_dict: {ticker: DataFrame with 'close' column}
        capital: 초기 자본
        max_holdings: 최대 보유 종목 수
        rebalance_days: 리밸런스 주기 (거래일)
    """
    # 모든 종목의 공통 날짜 인덱스 맞추기
    all_dates = None
    close_dict: dict[str, pd.Series] = {}
    for ticker, df in price_dict.items():
        if "close" not in df.columns:
            continue
        series = df.set_index("datetime")["close"] if "datetime" in df.columns else df["close"]
        series = series.dropna()
        close_dict[ticker] = series
        if all_dates is None:
            all_dates = set(series.index)
        else:
            all_dates &= set(series.index)

    if all_dates is None or len(all_dates) < 252:
        return {"error": "데이터 부족"}

    common_dates = sorted(all_dates)

    # 공통 날짜로 정렬
    aligned: dict[str, pd.Series] = {}
    for ticker, series in close_dict.items():
        aligned[ticker] = series.reindex(common_dates).ffill().dropna()

    cash = capital
    positions: dict[str, dict] = {}  # {ticker: {qty, buy_price}}
    equity_curve = []
    trades = []
    last_rebalance = 0

    for day_idx in range(252, len(common_dates)):
        date = common_dates[day_idx]

        # 에쿼티 계산
        equity = cash
        for ticker, pos in positions.items():
            current_price = float(aligned[ticker].iloc[day_idx])
            equity += pos["qty"] * current_price
        equity_curve.append((str(date), equity))

        # 리밸런스 시점인지 확인
        if day_idx - last_rebalance < rebalance_days:
            continue
        last_rebalance = day_idx

        # 모멘텀 점수 계산 (전일까지 데이터로 — 당일 종가 look-ahead 방지)
        price_series = {t: s.iloc[:day_idx] for t, s in aligned.items()}
        rankings = rank_universe(price_series)
        targets = select_holdings(rankings, max_holdings)
        target_tickers = {ms.ticker for ms in targets}

        # ── 매도: 현재 보유 중이지만 타겟 아닌 종목 ──
        for ticker in list(positions.keys()):
            if ticker not in target_tickers:
                pos = positions[ticker]
                sell_price = float(aligned[ticker].iloc[day_idx])
                actual_sell = int(sell_price * (1 - slippage))
                revenue = actual_sell * pos["qty"]
                comm = int(revenue * commission_rate)
                pnl = (actual_sell - pos["buy_price"]) * pos["qty"] - comm - pos.get("buy_comm", 0)
                cash += revenue - comm
                trades.append({
                    "date": str(date), "ticker": ticker, "side": "sell",
                    "price": actual_sell, "qty": pos["qty"], "pnl": pnl,
                })
                del positions[ticker]

        # ── 매수: 타겟이지만 미보유 종목 ──
        free_slots = max_holdings - len(positions)
        if free_slots > 0 and targets:
            # 변동성 기반 포지션 사이징 (Risk Parity 간소화)
            # 변동성이 낮을수록 더 많이 투자
            buy_targets = [ms for ms in targets if ms.ticker not in positions and ms.ticker in aligned]
            if buy_targets:
                inv_vols = [1 / max(ms.volatility, 5) for ms in buy_targets]
                vol_sum = sum(inv_vols)
                # 매수 전 현금 스냅샷으로 배분 (루프 중 cash 감소 방지)
                cash_snapshot = cash * 0.95  # 5% 버퍼
                for ms, inv_vol in zip(buy_targets, inv_vols):
                    if len(positions) >= max_holdings:
                        break
                    weight = inv_vol / vol_sum
                    alloc = int(cash_snapshot * weight)
                    buy_price = float(aligned[ms.ticker].iloc[day_idx])
                    actual_buy = int(buy_price * (1 + slippage))
                    qty = alloc // actual_buy if actual_buy > 0 else 0
                    if qty <= 0:
                        continue
                    cost = actual_buy * qty
                    comm = int(cost * commission_rate)
                    cash -= (cost + comm)
                    positions[ms.ticker] = {"qty": qty, "buy_price": actual_buy, "buy_comm": comm}
                    trades.append({
                        "date": str(date), "ticker": ms.ticker, "side": "buy",
                        "price": actual_buy, "qty": qty, "pnl": 0,
                        "reason": f"모멘텀 score={ms.score:.1f}",
                    })

    # 최종 청산
    for ticker, pos in list(positions.items()):
        sell_price = float(aligned[ticker].iloc[-1])
        actual_sell = int(sell_price * (1 - slippage))
        revenue = actual_sell * pos["qty"]
        comm = int(revenue * commission_rate)
        pnl = (actual_sell - pos["buy_price"]) * pos["qty"] - comm - pos.get("buy_comm", 0)
        cash += revenue - comm
        trades.append({
            "date": str(common_dates[-1]), "ticker": ticker, "side": "sell",
            "price": actual_sell, "qty": pos["qty"], "pnl": pnl,
        })

    # 통계
    sell_trades = [t for t in trades if t["side"] == "sell"]
    wins = [t for t in sell_trades if t["pnl"] > 0]
    losses = [t for t in sell_trades if t["pnl"] <= 0]

    total_return = (cash - capital) / capital * 100

    # MDD
    peak = capital
    max_dd = 0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Sharpe
    daily_returns = []
    for i in range(1, len(equity_curve)):
        prev_eq = equity_curve[i - 1][1]
        curr_eq = equity_curve[i][1]
        if prev_eq > 0:
            daily_returns.append((curr_eq - prev_eq) / prev_eq)
    sharpe = 0.0
    if daily_returns:
        arr = np.array(daily_returns)
        if arr.std() > 0:
            sharpe = (arr.mean() / arr.std()) * np.sqrt(252)

    # CAGR
    years = len(equity_curve) / 252
    cagr = ((cash / capital) ** (1 / years) - 1) * 100 if years > 0 else 0

    gross_profit = sum(t["pnl"] for t in wins) if wins else 0
    gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 1
    pf = gross_profit / gross_loss if gross_loss > 0 else 0

    return {
        "total_return_pct": round(total_return, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 2),
        "profit_factor": round(pf, 2),
        "total_trades": len(sell_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(sell_trades) * 100 if sell_trades else 0, 1),
        "avg_trades_per_year": round(len(sell_trades) / years if years > 0 else 0, 1),
        "final_capital": int(cash),
        "equity_curve": equity_curve,
        "trades": trades,
    }


def main():
    """듀얼 모멘텀 로테이션 백테스트 실행."""
    import yfinance as yf

    print("=" * 60)
    print("  듀얼 모멘텀 ETF 로테이션 백테스트")
    print("  (Antonacci + Faber + Risk Parity)")
    print("=" * 60)

    for label, period in [("최근 2년", "2y"), ("최근 5년", "5y")]:
        print(f"\n{'─'*55}")
        print(f"  {label}")
        print(f"{'─'*55}")

        price_dict = {}
        for yf_ticker, (code, name) in ETF_UNIVERSE.items():
            try:
                raw = yf.download(yf_ticker, period=period, auto_adjust=True, progress=False)
                if raw.empty:
                    continue
                df = raw.copy()
                df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
                df = df.reset_index()
                dc = [c for c in df.columns if "date" in str(c).lower()]
                if dc:
                    df = df.rename(columns={dc[0]: "datetime"})
                df["datetime"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y%m%d")
                price_dict[yf_ticker] = df[["datetime", "close"]].copy()
            except Exception as e:
                print(f"  {name}: 다운로드 실패 ({e})")

        if len(price_dict) < 3:
            print("  데이터 부족")
            continue

        for max_h in [1, 2]:
            stats = backtest_momentum_rotation(
                price_dict, capital=1_000_000, max_holdings=max_h,
            )

            if "error" in stats:
                print(f"  {max_h}종목: {stats['error']}")
                continue

            print(
                f"  {max_h}종목 보유: "
                f"수익{stats['total_return_pct']:>+7.1f}% "
                f"CAGR{stats['cagr_pct']:>+5.1f}% "
                f"MDD{stats['max_drawdown_pct']:>5.1f}% "
                f"Sharpe{stats['sharpe_ratio']:>5.2f} "
                f"PF{stats['profit_factor']:>4.1f} "
                f"승률{stats['win_rate_pct']:>4.0f}% "
                f"{stats['total_trades']}거래 "
                f"({stats['avg_trades_per_year']:.0f}회/년) "
                f"최종{stats['final_capital']:>+,}원"
            )


if __name__ == "__main__":
    main()
