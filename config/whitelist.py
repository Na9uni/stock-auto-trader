"""자동매매 화이트리스트 + 필터 관리.

K-value 최적화 (2021-01~2026-04, Sharpe 기준) 결과 반영:
  제거 기준 — Sharpe<0.3(전K) / PF<1.0(전K) / MDD>25%
"""

from __future__ import annotations

# ETF 종목 (변동성 돌파 주력)
# 최적K 주석: 그리드서치(0.3~0.8) Sharpe 최대화 기준
ETF_WHITELIST: dict[str, str] = {
    # "069500": "KODEX 200",           # 제외: MDD 26.7%(K=0.3) — K=0.5 유지 시 MDD 22.0%/Sharpe 0.56 이지만 상위 ETF 대비 열세
    "229200": "KODEX 코스닥150",        # 최적K=0.6, Sharpe=0.55, PF=1.39, MDD=17.1%
    # "133690": "TIGER 미국나스닥100",   # 제외: Sharpe<0(전K), PF<1.0(전K) — VB 전략 부적합
    "131890": "ACE 삼성그룹동일가중",     # 최적K=0.3, Sharpe=1.35, PF=2.29, MDD=12.6%
    "108450": "ACE 삼성그룹섹터가중",     # 최적K=0.3, Sharpe=0.89, PF=1.66, MDD=13.3%
    "395160": "KODEX AI반도체",          # 최적K=0.5, Sharpe=1.53, PF=2.29, MDD=9.4%  (1위)
    # 위기 로테이션 ETF (VB 전략 부적합하나 레짐 엔진 헤지용 유지)
    "261220": "KODEX WTI원유선물(H)",    # 최적K=0.5, Sharpe=0.47, PF=1.31, MDD=17.3%
    # "130730": "KODEX 인버스",          # 제외: Sharpe<0(전K), PF=0(전K) — VB 전략에서 거래 1~2회만 발생
    "132030": "KODEX 골드선물(H)",       # 최적K=0.7, Sharpe=0.75, PF=1.71, MDD=6.6%
}

# 개별주 (감시 + 제한적 자동매매)
# K-value 최적화 결과 — 개별주는 MDD가 전반적으로 높아 삼성전자만 통과
STOCK_WHITELIST: dict[str, str] = {
    "005930": "삼성전자",               # 최적K=0.6, Sharpe=0.89, PF=1.96, MDD=15.0%  (4위)
    # "105560": "KB금융",              # 제외: Sharpe<0(전K), PF<1.0(전K), MDD=31.2%
    # "055550": "신한지주",             # 제외: Sharpe<0.3(전K), MDD=28.8%
    # "006910": "보성파워텍",            # 제외: PF 1.0, 승률 29%, MDD 27.6% (기존)
    # "016610": "DB증권",              # 제외: Sharpe<0(전K), PF<1.0(전K), MDD=50.5%
    # "019180": "티에이치엔",            # 제외: Sharpe<0(전K), PF<1.0(전K), MDD=50.3%
    # "000500": "가온전선",             # 제외: MDD=44.5% — 수익률 +164.8%이나 낙폭 과대
    # "014790": "HL D&I",             # 제외: Sharpe<0(전K), PF<1.0(전K), MDD=47.4%
    # "103590": "일진전기",             # 제외: MDD=38.6% — 수익률 +133.8%이나 낙폭 과대
    # "009420": "한올바이오파마",         # 제외: Sharpe<0.3(전K), MDD=29.9%
    # "034020": "두산에너빌리티",         # 제외: MDD=61.8% — 수익률 +67.3%이나 낙폭 과대
    # "078600": "대주전자재료",           # 제외: MDD=39.7% — 수익률 +84.6%이나 낙폭 과대
}

# 전체 화이트리스트
AUTO_TRADE_WHITELIST: dict[str, str] = {**ETF_WHITELIST, **STOCK_WHITELIST}

# 종목별 최적 K값 (walk-forward 그리드서치 2021~2026 Sharpe 기준)
# 여기에 없는 종목은 기본값 사용 (ETF: VB_K, 개별주: VB_K_INDIVIDUAL)
TICKER_K_MAP: dict[str, float] = {
    "229200": 0.6,   # KODEX 코스닥150
    "131890": 0.3,   # ACE 삼성그룹동일가중 (Sharpe 1.35)
    "108450": 0.3,   # ACE 삼성그룹섹터가중 (Sharpe 0.89)
    "395160": 0.5,   # KODEX AI반도체 (Sharpe 1.53)
    "005930": 0.6,   # 삼성전자 (Sharpe 0.89)
    "261220": 0.5,   # KODEX WTI원유선물
    "132030": 0.7,   # KODEX 골드선물 (Sharpe 0.75)
}


def get_ticker_k(ticker: str) -> float | None:
    """종목별 최적 K값 반환. 없으면 None (기본값 사용)."""
    return TICKER_K_MAP.get(ticker)

# 대형주 일봉 임계값 완화 대상
LARGECAP_DAILY_THRESHOLD: dict[str, str] = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "005380": "현대차",
}
LARGECAP_DAILY_MIN_SCORE = 4


def is_whitelisted(ticker: str) -> bool:
    """화이트리스트 포함 여부."""
    return ticker in AUTO_TRADE_WHITELIST


def is_etf(ticker: str) -> bool:
    """ETF 종목 여부."""
    return ticker in ETF_WHITELIST
