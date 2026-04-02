"""
그리드 서치 백테스트 최적화
화이트리스트 18종목 × 파라미터 조합 → 최적 설정 도출
"""
import sys
from pathlib import Path
from itertools import product

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import yfinance as yf
from backtest.backtester import Backtester

# 화이트리스트 (yfinance 티커)
TICKERS = {
    "005930.KS": "삼성전자",
    "069500.KS": "KODEX 200",
    "105560.KS": "KB금융",
    "055550.KS": "신한지주",
    "006910.KS": "보성파워텍",
    "016610.KS": "DB증권",
    "133690.KS": "TIGER 미국나스닥100",
    "229200.KS": "KODEX 코스닥150",
    "019180.KS": "티에이치엔",
    "000500.KS": "가온전선",
    "014790.KS": "HL D&I",
    "103590.KS": "일진전기",
    "009420.KS": "한올바이오파마",
    "034020.KS": "두산에너빌리티",
    "078600.KS": "대주전자재료",
}

# 그리드 서치 파라미터
STOPLOSS_RANGE = [2.5, 3.0, 3.5, 4.0, 5.0]
TRAILING_ACT_RANGE = [3.0, 4.0, 5.0, 7.0]
TRAILING_STOP_RANGE = [1.5, 2.0, 2.5, 3.0]
STRONG_RANGE = [3, 4, 5, 6]


def download_data(ticker: str, period: str = "1y") -> pd.DataFrame:
    """yfinance로 데이터 다운로드."""
    try:
        raw = yf.download(ticker, period=period, interval="1d", auto_adjust=True, progress=False)
        if raw.empty:
            return pd.DataFrame()
        df = raw.copy()
        df.columns = [c.lower() if isinstance(c, str) else c[0].lower() for c in df.columns]
        df = df.reset_index()
        date_col = [c for c in df.columns if 'date' in str(c).lower()]
        if date_col:
            df = df.rename(columns={date_col[0]: "datetime"})
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y%m%d")
        keep = ["datetime", "open", "high", "low", "close", "volume"]
        df = df[[c for c in keep if c in df.columns]]
        return df
    except Exception as e:
        print(f"  [{ticker}] 다운로드 실패: {e}")
        return pd.DataFrame()


def run_grid_search():
    """18종목 × 파라미터 그리드 서치."""
    print("=" * 70)
    print("  그리드 서치 백테스트 최적화")
    print("=" * 70)

    # 1) 데이터 다운로드
    print("\n[1/3] 데이터 다운로드...")
    all_data = {}
    for yf_ticker, name in TICKERS.items():
        df = download_data(yf_ticker, period="1y")
        if len(df) >= 120:
            all_data[yf_ticker] = (name, df)
            print(f"  {name}: {len(df)}일")
        else:
            print(f"  {name}: 데이터 부족 ({len(df)}일) - 스킵")

    if not all_data:
        print("데이터 없음. 종료.")
        return

    # 2) 그리드 서치
    print(f"\n[2/3] 그리드 서치 시작...")
    combos = list(product(STOPLOSS_RANGE, TRAILING_ACT_RANGE, TRAILING_STOP_RANGE, STRONG_RANGE))
    valid = [(sl, ta, ts, st) for sl, ta, ts, st in combos if ts < ta]
    print(f"  파라미터 조합: {len(valid)}개 × {len(all_data)}종목 = {len(valid) * len(all_data)}회")

    results = []
    total = len(valid)
    for idx, (sl, ta, ts, strong) in enumerate(valid):
        combo_pnl = 0
        combo_wins = 0
        combo_losses = 0

        for yf_ticker, (name, df) in all_data.items():
            bt = Backtester(
                initial_capital=1_000_000,
                stoploss_pct=sl,
                trailing_activate_pct=ta,
                trailing_stop_pct=ts,
                max_slots=1,
                strong_threshold=strong,
                use_daily=True,
            )
            stats = bt.run(yf_ticker.replace(".KS", ""), df)
            combo_pnl += stats.get("gross_profit", 0) + stats.get("gross_loss", 0)
            combo_wins += stats.get("wins", 0)
            combo_losses += stats.get("losses", 0)

        total_trades = combo_wins + combo_losses
        win_rate = (combo_wins / total_trades * 100) if total_trades > 0 else 0
        gross_profit = max(combo_pnl, 0)
        gross_loss = abs(min(combo_pnl, 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else 0

        results.append({
            "sl": sl, "ta": ta, "ts": ts, "strong": strong,
            "trades": total_trades,
            "pnl": combo_pnl,
            "wins": combo_wins,
            "losses": combo_losses,
            "win_rate": win_rate,
            "pf": pf,
        })

        if (idx + 1) % 20 == 0:
            print(f"  진행: {idx + 1}/{total}")

    # 3) 결과 정렬 (PnL 순)
    results.sort(key=lambda x: x["pnl"], reverse=True)

    # 거래가 있는 결과만 필터 + PnL 순 정렬
    traded = [r for r in results if r["trades"] > 0]
    traded.sort(key=lambda x: x["pnl"], reverse=True)

    print(f"\n[3/3] 결과 — 거래 발생 {len(traded)}개 / 전체 {len(results)}개")
    if not traded:
        print("\n※ 모든 조합에서 거래 0건. 신호 로직이 일봉에서 작동하지 않습니다.")
        return

    print(f"\n상위 15개:")
    print(f"{'SL%':>5} {'TA%':>5} {'TS%':>5} {'STR':>4} {'거래':>5} {'승':>4} {'패':>4} {'승률':>6} {'PF':>5} {'총손익':>12}")
    print("-" * 65)
    for r in traded[:15]:
        print(f"{r['sl']:>5.1f} {r['ta']:>5.1f} {r['ts']:>5.1f} {r['strong']:>4} "
              f"{r['trades']:>5} {r['wins']:>4} {r['losses']:>4} "
              f"{r['win_rate']:>5.1f}% {r['pf']:>5.1f} {r['pnl']:>+11,}원")

    print(f"\n하위 5개:")
    print(f"{'SL%':>5} {'TA%':>5} {'TS%':>5} {'STR':>4} {'거래':>5} {'승':>4} {'패':>4} {'승률':>6} {'PF':>5} {'총손익':>12}")
    print("-" * 65)
    for r in traded[-5:]:
        print(f"{r['sl']:>5.1f} {r['ta']:>5.1f} {r['ts']:>5.1f} {r['strong']:>4} "
              f"{r['trades']:>5} {r['wins']:>4} {r['losses']:>4} "
              f"{r['win_rate']:>5.1f}% {r['pf']:>5.1f} {r['pnl']:>+11,}원")

    # 최적 설정
    best = traded[0]
    print(f"\n{'=' * 65}")
    if best["pnl"] > 0:
        print(f"★ 최적 설정: SL={best['sl']}% TA={best['ta']}% TS={best['ts']}% STRONG>={best['strong']}")
        print(f"  성적: {best['trades']}거래, 승률 {best['win_rate']:.1f}%, PF {best['pf']:.1f}, 총손익 {best['pnl']:+,}원")
    else:
        print(f"※ 양의 수익 조합 없음. 최선: SL={best['sl']}% TA={best['ta']}% TS={best['ts']}% STRONG>={best['strong']}")
        print(f"  성적: {best['trades']}거래, 손익 {best['pnl']:+,}원")
    print(f"{'=' * 65}")


if __name__ == "__main__":
    run_grid_search()
