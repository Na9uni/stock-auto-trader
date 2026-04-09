"""자동매매 화이트리스트 + 필터 관리."""

from __future__ import annotations

# ETF 종목 (변동성 돌파 주력)
ETF_WHITELIST: dict[str, str] = {
    "069500": "KODEX 200",
    "229200": "KODEX 코스닥150",
    "133690": "TIGER 미국나스닥100",
    "131890": "ACE 삼성그룹동일가중",
    "108450": "ACE 삼성그룹섹터가중",
    "395160": "KODEX AI반도체",
    # 위기 로테이션 ETF
    "261220": "KODEX WTI원유선물(H)",
    "130730": "KODEX 인버스",
    "132030": "KODEX 골드선물(H)",
}

# 개별주 (감시 + 제한적 자동매매)
# 백테스트 PF < 1.0 종목 제외: 보성파워텍(MDD 27.6%, 승률 29%)
STOCK_WHITELIST: dict[str, str] = {
    "005930": "삼성전자",
    "105560": "KB금융",
    "055550": "신한지주",
    # "006910": "보성파워텍",  # 제외: PF 1.0, 승률 29%, MDD 27.6%
    "016610": "DB증권",
    "019180": "티에이치엔",
    "000500": "가온전선",
    "014790": "HL D&I",
    "103590": "일진전기",
    "009420": "한올바이오파마",
    "034020": "두산에너빌리티",
    "078600": "대주전자재료",
}

# 전체 화이트리스트
AUTO_TRADE_WHITELIST: dict[str, str] = {**ETF_WHITELIST, **STOCK_WHITELIST}

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
