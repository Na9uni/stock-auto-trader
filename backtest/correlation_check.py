"""화이트리스트 종목 간 상관관계 분석 (TODO 9-B).

보유 종목 수익률이 같은 방향으로 움직이면 분산 효과 X.
Core-Satellite 구조(TODO ⑧) 전 종목 조합 검토용.

출력:
- 상관계수 행렬
- 고상관 쌍 (>0.7) 경고 리스트
- 섹터 분산 점수

사용:
    py backtest/correlation_check.py
    py backtest/correlation_check.py --period 60d --threshold 0.7
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


TICKERS = {
    "069500.KS": "KODEX200",
    "229200.KS": "KODEX코스닥150",
    "133690.KS": "TIGER나스닥100",
    "131890.KS": "ACE삼성그룹동일",
    "108450.KS": "ACE삼성그룹섹터",
    "395160.KS": "KODEXAI반도체",
    "005930.KS": "삼성전자",
    "034020.KS": "두산에너빌",
    "105560.KS": "KB금융",
    "055550.KS": "신한지주",
    "103590.KS": "일진전기",
}

# 섹터 분류 (대략)
SECTORS = {
    "069500.KS": "ETF-대형",
    "229200.KS": "ETF-코스닥",
    "133690.KS": "ETF-미국",
    "131890.KS": "ETF-삼성그룹",
    "108450.KS": "ETF-삼성그룹",
    "395160.KS": "ETF-반도체테마",
    "005930.KS": "반도체",
    "034020.KS": "원전/중공업",
    "105560.KS": "금융",
    "055550.KS": "금융",
    "103590.KS": "전력/에너지",
}


def download_close(ticker: str, period: str = "90d") -> pd.Series | None:
    raw = yf.download(ticker, period=period, auto_adjust=True, progress=False)
    if raw.empty:
        return None
    df = raw.copy()
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    return df["Close"]


def compute_correlation(period: str = "90d") -> pd.DataFrame:
    """종목 간 일별 수익률 상관계수."""
    print(f"\n📥 데이터 다운로드 중 ({period})...")
    prices = {}
    for ticker, name in TICKERS.items():
        close = download_close(ticker, period)
        if close is None or len(close) < 20:
            print(f"  ⚠️ {name}: 데이터 부족")
            continue
        prices[name] = close
        print(f"  ✅ {name}: {len(close)}일")

    if len(prices) < 2:
        print("❌ 데이터 부족으로 상관계수 계산 불가")
        return pd.DataFrame()

    df = pd.DataFrame(prices).dropna()
    returns = df.pct_change().dropna()
    corr = returns.corr()
    return corr


def find_high_correlation(corr: pd.DataFrame, threshold: float = 0.7) -> list[tuple]:
    """threshold 이상 고상관 쌍 찾기."""
    pairs = []
    cols = corr.columns.tolist()
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            r = corr.iloc[i, j]
            if abs(r) >= threshold:
                pairs.append((cols[i], cols[j], float(r)))
    return sorted(pairs, key=lambda x: abs(x[2]), reverse=True)


def sector_diversification_score(corr: pd.DataFrame) -> dict:
    """섹터 분산 점수: 낮을수록 분산 ↑."""
    names_to_ticker = {v: k for k, v in TICKERS.items()}
    sector_groups: dict[str, list[str]] = {}
    for name in corr.columns:
        ticker = names_to_ticker.get(name, "?")
        sector = SECTORS.get(ticker, "Unknown")
        sector_groups.setdefault(sector, []).append(name)

    # 섹터 내 평균 상관계수 vs 섹터 간 평균 상관계수
    intra_corrs = []
    inter_corrs = []
    for sector, names in sector_groups.items():
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if a in corr.columns and b in corr.columns:
                    intra_corrs.append(corr.loc[a, b])

    for s1, names1 in sector_groups.items():
        for s2, names2 in sector_groups.items():
            if s1 >= s2:
                continue
            for a in names1:
                for b in names2:
                    if a in corr.columns and b in corr.columns:
                        inter_corrs.append(corr.loc[a, b])

    return {
        "sectors": sector_groups,
        "intra_sector_avg_corr": float(np.mean(intra_corrs)) if intra_corrs else 0.0,
        "inter_sector_avg_corr": float(np.mean(inter_corrs)) if inter_corrs else 0.0,
        "diversification_ratio": (
            float(np.mean(inter_corrs)) / float(np.mean(intra_corrs))
            if intra_corrs and np.mean(intra_corrs) != 0
            else 0.0
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", default="90d", help="기간 (yfinance period, 기본 90d)")
    parser.add_argument("--threshold", type=float, default=0.7, help="고상관 임계값")
    args = parser.parse_args()

    print("=" * 85)
    print(f"  📊 화이트리스트 상관관계 분석 — {args.period}")
    print("=" * 85)

    corr = compute_correlation(args.period)
    if corr.empty:
        return

    # 1. 상관계수 행렬 (간결 출력)
    print(f"\n## 상관계수 행렬 ({len(corr)}종목)")
    print(corr.round(2).to_string())

    # 2. 고상관 쌍 (>=threshold)
    high_pairs = find_high_correlation(corr, args.threshold)
    print(f"\n## 고상관 쌍 (|r| >= {args.threshold})")
    if not high_pairs:
        print(f"  ✅ 없음 — 현재 화이트리스트 분산 양호")
    else:
        for a, b, r in high_pairs[:10]:
            marker = "🔴" if abs(r) >= 0.85 else "🟡"
            print(f"  {marker} {a:<14} ↔ {b:<14}  r = {r:+.3f}")
        if len(high_pairs) > 10:
            print(f"  ... 외 {len(high_pairs)-10}쌍")

    # 3. 섹터 분산 점수
    div = sector_diversification_score(corr)
    print(f"\n## 섹터 분산")
    for sector, names in div["sectors"].items():
        print(f"  [{sector}] {', '.join(names)}")
    print(f"\n  섹터 내 평균 상관: {div['intra_sector_avg_corr']:+.3f}")
    print(f"  섹터 간 평균 상관: {div['inter_sector_avg_corr']:+.3f}")
    ratio = div["diversification_ratio"]
    judgment = "양호 ✅" if ratio < 0.8 else ("보통 🟡" if ratio < 1.0 else "취약 🔴")
    print(f"  분산 비율 (섹터간/섹터내): {ratio:.2f}  → {judgment}")

    print("\n" + "=" * 85)
    print("  💡 활용:")
    print("  - 고상관 쌍 동시 보유 시 → 한 종목 제외 권장")
    print("  - 섹터 분산 취약 시 → 다른 섹터 ETF 추가")
    print("  - Core-Satellite 구축 시 (TODO ⑧) 필수 참고")
    print("=" * 85)


if __name__ == "__main__":
    main()
