"""
run_backtest.py
삼성전자 6개월 일봉 데이터로 백테스트 실행 예시
"""
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import yfinance as yf

from backtest.backtester import Backtester


def download_samsung(period: str = "6mo") -> pd.DataFrame:
    """yfinance로 삼성전자 일봉 다운로드 후 OHLCV 형식으로 반환."""
    # 삼성전자 KRX 티커: 005930.KS
    ticker = "005930.KS"
    raw = yf.download(ticker, period=period, interval="1d", auto_adjust=True, progress=False)

    if raw.empty:
        raise RuntimeError(f"yfinance 데이터 수신 실패: {ticker}")

    # 컬럼명 소문자 통일 & datetime 컬럼 추가
    df = raw.copy()
    df.columns = [c.lower() for c in df.columns]
    df = df.rename(columns={"date": "datetime"})
    df = df.reset_index()

    # index가 DatetimeIndex인 경우 datetime 컬럼으로 이동
    if "datetime" not in df.columns and df.index.name == "Date":
        df = df.reset_index().rename(columns={"Date": "datetime"})

    df["datetime"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d")

    # 필요 컬럼만 유지
    keep = ["datetime", "open", "high", "low", "close", "volume"]
    df = df[[c for c in keep if c in df.columns]]

    print(f"[데이터] {ticker} {len(df)}일치 로드 완료 "
          f"({df['datetime'].iloc[0]} ~ {df['datetime'].iloc[-1]})")
    return df


def main():
    print("삼성전자(005930) 6개월 백테스트\n")

    # 1) 데이터 수집
    df = download_samsung(period="6mo")

    if len(df) < 65:
        print(f"데이터 부족 ({len(df)}일). 기간을 늘려주세요.")
        sys.exit(1)

    # 2) 백테스터 초기화
    bt = Backtester(
        initial_capital=1_000_000,
        stoploss_pct=2.0,
        trailing_activate_pct=2.0,
        trailing_stop_pct=1.0,
        commission_rate=0.00015,
        tax_rate=0.0018,
        max_slots=1,  # 단일 종목이므로 슬롯 1개
    )

    # 3) 백테스트 실행
    stats = bt.run("005930", df)

    # 4) 결과 출력
    bt.print_report(stats)

    # 5) 거래 내역 상세 출력
    sell_trades = [t for t in bt.trades if t["side"] == "sell"]
    if sell_trades:
        print("\n거래 내역:")
        print(f"{'날짜':<12} {'구분':<8} {'가격':>8} {'수량':>6} {'손익':>10} 사유")
        print("-" * 60)
        for t in sell_trades:
            pnl_str = f"{t['pnl']:+,.0f}원"
            print(f"{t['date']:<12} {t['side']:<8} {t['price']:>8,} "
                  f"{t['qty']:>6} {pnl_str:>10} {t['reason']}")
    else:
        print("\n체결된 거래 없음 (신호 미발생 또는 데이터 부족)")


if __name__ == "__main__":
    main()
