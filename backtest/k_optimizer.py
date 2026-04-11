"""K-value Grid Search + Ticker Performance Ranking.

화이트리스트 전 종목에 대해 K=0.3~0.8 범위를 그리드 서치하여
Sharpe Ratio 기준 최적 K값을 탐색한다.

VB 전략 (EOD 청산) 기반, backtester_auto.py 의 VB 로직과 동일.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import yfinance as yf

from analysis.indicators import TechnicalIndicators
from backtest.cost_model import CostModel
from backtest.exit_manager import ExitManager, ExitReason
from config.trading_config import TradingConfig


# ── 전체 화이트리스트 (yfinance ticker 매핑) ──
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

ETF_CODES = {"069500", "229200", "133690", "131890", "108450", "395160",
             "261220", "130730", "132030"}

K_VALUES = [round(0.3 + 0.1 * i, 1) for i in range(6)]  # 0.3 ~ 0.8


def download(yf_ticker: str, start: str, end: str) -> pd.DataFrame:
    """yfinance 일봉 다운로드."""
    raw = yf.download(yf_ticker, start=start, end=end, auto_adjust=True, progress=False)
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


def run_vb_backtest(ticker_code: str, df: pd.DataFrame, k: float,
                    config: TradingConfig) -> dict:
    """VB 전략 백테스트 (EOD 청산). backtester_auto.py VB 로직 동일."""
    is_etf = ticker_code in ETF_CODES
    # config override for this K
    overrides = {"vb_k": k, "vb_k_individual": k}
    cfg = TradingConfig.from_dict(overrides)
    cost = CostModel(cfg)
    exit_mgr = ExitManager(cfg)
    indicators = TechnicalIndicators()

    df = df.copy().reset_index(drop=True)
    df["range"] = df["high"] - df["low"]
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()

    df_ind = indicators.get_all_indicators(df)

    capital = 1_000_000
    cash = capital
    position = None
    trades: list[dict] = []
    equity_curve: list[tuple] = []

    start_idx = 61

    for i in range(start_idx, len(df)):
        prev = df.iloc[i - 1]
        today = df.iloc[i]
        today_date = str(today.get("datetime", i))
        today_open = int(today["open"])
        today_high = int(today["high"])
        today_low = int(today["low"])
        today_close = int(today["close"])
        prev_range = int(prev["range"])
        prev_ma10 = float(prev["ma10"]) if not pd.isna(prev["ma10"]) else 0
        prev_ma20 = float(prev["ma20"]) if not pd.isna(prev["ma20"]) else 0
        prev_ma60 = float(prev["ma60"]) if not pd.isna(prev["ma60"]) else 0

        target = today_open + int(prev_range * k)

        # RSI + ATR
        rsi = 50.0
        atr = 0.0
        if i < len(df_ind):
            r = df_ind.iloc[i].get("rsi", 50.0)
            if not pd.isna(r):
                rsi = float(r)
            a = df_ind.iloc[i].get("atr", 0.0)
            if not pd.isna(a):
                atr = float(a)

        # ── 보유 중: 장중 손절/트레일링 → EOD 청산 ──
        if position is not None:
            exit_actions, new_trail, new_partial = exit_mgr.check(
                buy_price=position["buy_price"],
                qty=position["qty"],
                high_price=position["high_price"],
                current_low=today_low,
                current_high=today_high,
                current_close=today_close,
                rsi=rsi,
                trailing_activated=position["trailing_activated"],
                partial_sold=position["partial_sold"],
                atr=atr,
            )
            position["trailing_activated"] = new_trail
            position["partial_sold"] = new_partial
            if today_high > position["high_price"]:
                position["high_price"] = today_high

            sold_all = False
            for action in exit_actions:
                if action.reason == ExitReason.PARTIAL_TAKE_PROFIT:
                    sell_price = cost.sell_execution_price(action.price, today_close, ticker_code)
                    comm, tax = cost.sell_cost(sell_price, action.qty, ticker_code)
                    revenue = sell_price * action.qty - comm - tax
                    buy_comm_portion = int(position.get("buy_comm", 0) * action.qty / position["qty"])
                    pnl = (sell_price - position["buy_price"]) * action.qty - comm - tax - buy_comm_portion
                    position["buy_comm"] = position.get("buy_comm", 0) - buy_comm_portion
                    cash += revenue
                    position["qty"] -= action.qty
                    trades.append({"side": "sell", "pnl": pnl})
                else:
                    sell_qty = position["qty"]
                    sell_price = cost.sell_execution_price(action.price, action.price, ticker_code)
                    comm, tax = cost.sell_cost(sell_price, sell_qty, ticker_code)
                    revenue = sell_price * sell_qty - comm - tax
                    pnl = (sell_price - position["buy_price"]) * sell_qty - comm - tax - position.get("buy_comm", 0)
                    cash += revenue
                    trades.append({"side": "sell", "pnl": pnl})
                    position = None
                    sold_all = True
                    break

            # EOD 강제 청산
            if not sold_all and position is not None:
                sell_qty = position["qty"]
                sell_price = cost.sell_execution_price(today_close, today_close, ticker_code)
                comm, tax = cost.sell_cost(sell_price, sell_qty, ticker_code)
                revenue = sell_price * sell_qty - comm - tax
                pnl = (sell_price - position["buy_price"]) * sell_qty - comm - tax - position.get("buy_comm", 0)
                cash += revenue
                trades.append({"side": "sell", "pnl": pnl})
                position = None

        # ── 매수 판단 ──
        if position is None:
            # 레짐 필터: MA20 > MA60 (상승장만)
            if prev_ma20 > 0 and prev_ma60 > 0 and prev_ma20 < prev_ma60:
                equity_curve.append((today_date, cash))
                continue
            # 마켓 필터: 시가 > MA10
            if prev_ma10 <= 0 or today_open <= prev_ma10:
                equity_curve.append((today_date, cash))
                continue
            # 변동성 필터
            if prev_range < today_open * 0.005:
                equity_curve.append((today_date, cash))
                continue
            # 목표가 돌파
            if today_high >= target:
                fill_base = max(target, today_open)
                buy_price = cost.buy_execution_price(fill_base, fill_base, ticker_code)
                cost_per_share = buy_price * (1 + cfg.commission_rate)
                qty = int(cash // cost_per_share)
                if qty > 0:
                    buy_comm = cost.buy_cost(buy_price, qty)
                    cash -= (buy_price * qty + buy_comm)
                    position = {
                        "qty": qty,
                        "buy_price": buy_price,
                        "buy_comm": buy_comm,
                        "high_price": today_high,
                        "trailing_activated": False,
                        "partial_sold": False,
                    }
                    trades.append({"side": "buy", "pnl": 0})

        # 에쿼티 기록
        equity = cash
        if position is not None:
            equity += position["qty"] * today_close
        equity_curve.append((today_date, equity))

    # 마지막 보유분 청산
    if position is not None:
        last_close = int(df.iloc[-1]["close"])
        sell_price = cost.sell_execution_price(last_close, last_close, ticker_code)
        sell_qty = position["qty"]
        comm, tax = cost.sell_cost(sell_price, sell_qty, ticker_code)
        revenue = sell_price * sell_qty - comm - tax
        pnl = (sell_price - position["buy_price"]) * sell_qty - comm - tax - position.get("buy_comm", 0)
        cash += revenue
        trades.append({"side": "sell", "pnl": pnl})
        equity_curve.append((str(df.iloc[-1].get("datetime", "last")), cash))

    # ── 통계 계산 ──
    sell_trades = [t for t in trades if t["side"] == "sell"]
    total_trades = len(sell_trades)
    wins = [t for t in sell_trades if t["pnl"] > 0]
    losses = [t for t in sell_trades if t["pnl"] <= 0]

    total_return = (cash - capital) / capital * 100
    win_rate = len(wins) / total_trades * 100 if total_trades > 0 else 0

    gross_profit = sum(t["pnl"] for t in wins) if wins else 0
    gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0)

    # MDD
    peak = capital
    max_dd = 0.0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Sharpe
    daily_returns: list[float] = []
    for j in range(1, len(equity_curve)):
        prev_eq = equity_curve[j - 1][1]
        curr_eq = equity_curve[j][1]
        if prev_eq > 0:
            daily_returns.append((curr_eq - prev_eq) / prev_eq)
    sharpe = 0.0
    if daily_returns:
        arr = np.array(daily_returns)
        if arr.std() > 0:
            sharpe = (arr.mean() / arr.std()) * np.sqrt(252)

    return {
        "total_return_pct": round(total_return, 2),
        "sharpe_ratio": round(sharpe, 2),
        "profit_factor": round(pf, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "win_rate_pct": round(win_rate, 1),
        "total_trades": total_trades,
        "final_capital": int(cash),
    }


def main() -> None:
    """K-value 그리드 서치 + 종목 랭킹."""
    import logging
    logging.disable(logging.CRITICAL)

    config = TradingConfig.from_env()
    start_date = "2021-01-01"
    end_date = "2026-04-11"

    print("=" * 100)
    print(f"  K-value Grid Search (VB + EOD Liquidation)")
    print(f"  기간: {start_date} ~ {end_date}")
    print(f"  K 범위: {K_VALUES}")
    print(f"  현재 설정: ETF K={config.vb_k}, 개별주 K={config.vb_k_individual}")
    print("=" * 100)

    all_results: list[dict] = []

    for code, (yf_ticker, name) in TICKERS.items():
        print(f"\n>>> {name} ({code}) 다운로드 중...")
        df = download(yf_ticker, start_date, end_date)
        if len(df) < 100:
            print(f"  데이터 부족 ({len(df)}행). 스킵.")
            continue

        is_etf = code in ETF_CODES
        current_k = config.vb_k if is_etf else config.vb_k_individual
        ticker_type = "ETF" if is_etf else "개별주"

        best_sharpe = -999.0
        best_k = current_k
        best_stats = {}
        current_stats = {}

        k_results = {}

        for k_val in K_VALUES:
            stats = run_vb_backtest(code, df, k_val, config)
            k_results[k_val] = stats

            if k_val == current_k:
                current_stats = stats

            if stats["sharpe_ratio"] > best_sharpe:
                best_sharpe = stats["sharpe_ratio"]
                best_k = k_val
                best_stats = stats

        # 현재 K에 대한 결과가 없으면 (0.5나 0.6이 아닌 경우) 직접 계산
        if not current_stats:
            current_stats = run_vb_backtest(code, df, current_k, config)

        # 모든 K에서의 최대/최소 통계
        all_sharpes = [k_results[kv]["sharpe_ratio"] for kv in K_VALUES]
        all_pfs = [k_results[kv]["profit_factor"] for kv in K_VALUES]
        all_mdds = [k_results[kv]["max_drawdown_pct"] for kv in K_VALUES]
        max_sharpe_any_k = max(all_sharpes)
        max_pf_any_k = max(all_pfs)
        min_mdd_any_k = min(all_mdds)
        max_mdd_any_k = max(all_mdds)

        all_results.append({
            "code": code,
            "name": name,
            "type": ticker_type,
            "current_k": current_k,
            "best_k": best_k,
            "current_return": current_stats.get("total_return_pct", 0),
            "best_return": best_stats.get("total_return_pct", 0),
            "current_sharpe": current_stats.get("sharpe_ratio", 0),
            "best_sharpe": best_stats.get("sharpe_ratio", 0),
            "current_pf": current_stats.get("profit_factor", 0),
            "best_pf": best_stats.get("profit_factor", 0),
            "current_mdd": current_stats.get("max_drawdown_pct", 0),
            "best_mdd": best_stats.get("max_drawdown_pct", 0),
            "current_winrate": current_stats.get("win_rate_pct", 0),
            "best_winrate": best_stats.get("win_rate_pct", 0),
            "current_trades": current_stats.get("total_trades", 0),
            "best_trades": best_stats.get("total_trades", 0),
            "max_sharpe_any_k": max_sharpe_any_k,
            "max_pf_any_k": max_pf_any_k,
            "max_mdd_any_k": max_mdd_any_k,
            "k_details": k_results,
        })

        # K값별 결과 출력
        print(f"\n  [{ticker_type}] {name} ({code}) — K-value 그리드 결과:")
        print(f"  {'K':>4} | {'수익률':>8} | {'Sharpe':>7} | {'PF':>5} | {'MDD':>6} | {'승률':>5} | {'거래수':>5}")
        print(f"  {'-'*4}-+-{'-'*8}-+-{'-'*7}-+-{'-'*5}-+-{'-'*6}-+-{'-'*5}-+-{'-'*5}")
        for kv in K_VALUES:
            s = k_results[kv]
            marker = " *" if kv == best_k else ("  " if kv != current_k else " @")
            print(
                f"  {kv:>4}{marker}| {s['total_return_pct']:>+7.1f}% | {s['sharpe_ratio']:>7.2f} | "
                f"{s['profit_factor']:>5.2f} | {s['max_drawdown_pct']:>5.1f}% | "
                f"{s['win_rate_pct']:>4.0f}% | {s['total_trades']:>5}"
            )
        print(f"  (* = 최적K, @ = 현재K)")

    # ============================================================
    # Task 1: 종합 테이블
    # ============================================================
    print("\n")
    print("=" * 120)
    print("  TASK 1: K-value 최적화 결과 종합")
    print("=" * 120)
    print(
        f"  {'종목':>16} | {'유형':>4} | {'현재K':>5} | {'최적K':>5} | "
        f"{'현재수익':>8} | {'최적수익':>8} | {'Sharpe(현재)':>12} | {'Sharpe(최적)':>12} | "
        f"{'PF(최적)':>8} | {'MDD(최적)':>9}"
    )
    print(f"  {'-'*16}-+-{'-'*4}-+-{'-'*5}-+-{'-'*5}-+-{'-'*8}-+-{'-'*8}-+-{'-'*12}-+-{'-'*12}-+-{'-'*8}-+-{'-'*9}")
    for r in all_results:
        improvement = ""
        if r["best_k"] != r["current_k"]:
            diff = r["best_sharpe"] - r["current_sharpe"]
            if r["current_sharpe"] != 0:
                pct_change = diff / abs(r["current_sharpe"]) * 100
                improvement = f" ({pct_change:+.0f}%)"
            else:
                improvement = f" (+NEW)"
        print(
            f"  {r['name']:>16} | {r['type']:>4} | {r['current_k']:>5.1f} | {r['best_k']:>5.1f} | "
            f"{r['current_return']:>+7.1f}% | {r['best_return']:>+7.1f}% | "
            f"{r['current_sharpe']:>12.2f} | {r['best_sharpe']:>12.2f}{improvement:>0} | "
            f"{r['best_pf']:>8.2f} | {r['best_mdd']:>8.1f}%"
        )

    # ============================================================
    # Task 2: 종목 랭킹 + 제거 판정
    # ============================================================
    print("\n")
    print("=" * 120)
    print("  TASK 2: 종목 성과 랭킹 (Sharpe 기준, 최적K 적용)")
    print("=" * 120)

    # Sharpe 내림차순 정렬
    ranked = sorted(all_results, key=lambda x: x["best_sharpe"], reverse=True)

    removal_list = []
    keep_list = []

    print(
        f"  {'순위':>4} | {'종목':>16} | {'유형':>4} | {'최적K':>5} | "
        f"{'Sharpe':>7} | {'PF':>5} | {'MDD':>6} | {'수익률':>8} | {'판정':>8} | {'사유':>20}"
    )
    print(f"  {'-'*4}-+-{'-'*16}-+-{'-'*4}-+-{'-'*5}-+-{'-'*7}-+-{'-'*5}-+-{'-'*6}-+-{'-'*8}-+-{'-'*8}-+-{'-'*20}")

    for rank_idx, r in enumerate(ranked, 1):
        reasons = []
        remove = False

        # 제거 기준 체크: 모든 K에서 Sharpe < 0.3
        if r["max_sharpe_any_k"] < 0.3:
            reasons.append("Sharpe<0.3(전K)")
            remove = True

        # 제거 기준: 모든 K에서 PF < 1.0
        if r["max_pf_any_k"] < 1.0:
            reasons.append("PF<1.0(전K)")
            remove = True

        # 제거 기준: MDD > 25% (최적K 기준)
        if r["best_mdd"] > 25.0:
            reasons.append(f"MDD>{r['best_mdd']:.1f}%")
            remove = True

        verdict = "REMOVE" if remove else "KEEP"
        reason_str = ", ".join(reasons) if reasons else "-"

        if remove:
            removal_list.append(r)
        else:
            keep_list.append(r)

        print(
            f"  {rank_idx:>4} | {r['name']:>16} | {r['type']:>4} | {r['best_k']:>5.1f} | "
            f"{r['best_sharpe']:>7.2f} | {r['best_pf']:>5.2f} | {r['best_mdd']:>5.1f}% | "
            f"{r['best_return']:>+7.1f}% | {verdict:>8} | {reason_str:>20}"
        )

    # ============================================================
    # 요약
    # ============================================================
    print("\n")
    print("=" * 80)
    print("  요약")
    print("=" * 80)

    if removal_list:
        print(f"\n  제거 대상 ({len(removal_list)}종목):")
        for r in removal_list:
            reasons = []
            if r["max_sharpe_any_k"] < 0.3:
                reasons.append(f"Sharpe<0.3(최고={r['max_sharpe_any_k']:.2f})")
            if r["max_pf_any_k"] < 1.0:
                reasons.append(f"PF<1.0(최고={r['max_pf_any_k']:.2f})")
            if r["best_mdd"] > 25.0:
                reasons.append(f"MDD={r['best_mdd']:.1f}%>25%")
            print(f"    - {r['name']} ({r['code']}): {', '.join(reasons)}")

    if keep_list:
        print(f"\n  유지 종목 ({len(keep_list)}종목):")
        for r in keep_list:
            k_change = ""
            if r["best_k"] != r["current_k"]:
                k_change = f" (현재 {r['current_k']} -> 추천 {r['best_k']})"
            print(f"    - {r['name']} ({r['code']}): 최적K={r['best_k']}{k_change}, Sharpe={r['best_sharpe']:.2f}")

    # ETF 평균 최적K vs 현재 / 개별주 평균 최적K vs 현재
    etf_results = [r for r in keep_list if r["type"] == "ETF"]
    stock_results = [r for r in keep_list if r["type"] == "개별주"]

    if etf_results:
        avg_etf_k = sum(r["best_k"] for r in etf_results) / len(etf_results)
        avg_etf_sharpe_current = sum(r["current_sharpe"] for r in etf_results) / len(etf_results)
        avg_etf_sharpe_best = sum(r["best_sharpe"] for r in etf_results) / len(etf_results)
        print(f"\n  ETF 평균 최적K: {avg_etf_k:.2f} (현재: {config.vb_k})")
        print(f"  ETF 평균 Sharpe: 현재 {avg_etf_sharpe_current:.2f} -> 최적 {avg_etf_sharpe_best:.2f}")
        if avg_etf_sharpe_current > 0:
            improvement = (avg_etf_sharpe_best - avg_etf_sharpe_current) / avg_etf_sharpe_current * 100
            print(f"  ETF Sharpe 개선율: {improvement:+.1f}%")

    if stock_results:
        avg_stock_k = sum(r["best_k"] for r in stock_results) / len(stock_results)
        avg_stock_sharpe_current = sum(r["current_sharpe"] for r in stock_results) / len(stock_results)
        avg_stock_sharpe_best = sum(r["best_sharpe"] for r in stock_results) / len(stock_results)
        print(f"\n  개별주 평균 최적K: {avg_stock_k:.2f} (현재: {config.vb_k_individual})")
        print(f"  개별주 평균 Sharpe: 현재 {avg_stock_sharpe_current:.2f} -> 최적 {avg_stock_sharpe_best:.2f}")
        if avg_stock_sharpe_current > 0:
            improvement = (avg_stock_sharpe_best - avg_stock_sharpe_current) / avg_stock_sharpe_current * 100
            print(f"  개별주 Sharpe 개선율: {improvement:+.1f}%")

    # .env 변경 제안
    print("\n")
    print("=" * 80)
    print("  .env 변경 제안")
    print("=" * 80)
    if etf_results:
        avg_etf_k = sum(r["best_k"] for r in etf_results) / len(etf_results)
        rounded_etf_k = round(avg_etf_k, 1)
        if avg_etf_sharpe_current > 0:
            improvement = (avg_etf_sharpe_best - avg_etf_sharpe_current) / avg_etf_sharpe_current * 100
            if abs(improvement) > 10:
                print(f"  VB_K: {config.vb_k} -> {rounded_etf_k} (Sharpe 개선 {improvement:+.1f}% > 10%)")
            else:
                print(f"  VB_K: {config.vb_k} 유지 (Sharpe 개선 {improvement:+.1f}% <= 10%)")
        else:
            print(f"  VB_K: {config.vb_k} -> {rounded_etf_k} (현재 Sharpe=0, 최적으로 변경)")

    if stock_results:
        avg_stock_k = sum(r["best_k"] for r in stock_results) / len(stock_results)
        rounded_stock_k = round(avg_stock_k, 1)
        if avg_stock_sharpe_current > 0:
            improvement = (avg_stock_sharpe_best - avg_stock_sharpe_current) / avg_stock_sharpe_current * 100
            if abs(improvement) > 10:
                print(f"  VB_K_INDIVIDUAL: {config.vb_k_individual} -> {rounded_stock_k} (Sharpe 개선 {improvement:+.1f}% > 10%)")
            else:
                print(f"  VB_K_INDIVIDUAL: {config.vb_k_individual} 유지 (Sharpe 개선 {improvement:+.1f}% <= 10%)")
        else:
            print(f"  VB_K_INDIVIDUAL: {config.vb_k_individual} -> {rounded_stock_k} (현재 Sharpe=0, 최적으로 변경)")


if __name__ == "__main__":
    main()
