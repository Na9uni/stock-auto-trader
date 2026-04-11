"""화이트리스트 + K값 테스트."""
from __future__ import annotations

import pytest

from config.whitelist import (
    is_whitelisted,
    is_etf,
    get_ticker_k,
    AUTO_TRADE_WHITELIST,
    ETF_WHITELIST,
    STOCK_WHITELIST,
    TICKER_K_MAP,
)


class TestWhitelist:
    """화이트리스트 관리 테스트."""

    def test_samsung_is_whitelisted(self) -> None:
        assert is_whitelisted("005930")

    def test_removed_ticker_not_whitelisted(self) -> None:
        """제거된 종목(보성파워텍 006910)은 화이트리스트에 없어야 한다."""
        assert not is_whitelisted("006910")

    def test_samsung_is_not_etf(self) -> None:
        assert not is_etf("005930")

    def test_kodex_kosdaq150_is_etf(self) -> None:
        """KODEX 코스닥150 (229200)은 ETF여야 한다."""
        assert is_etf("229200")

    def test_kodex200_removed_from_whitelist(self) -> None:
        """KODEX 200 (069500)은 화이트리스트에서 제거됨."""
        assert not is_whitelisted("069500")

    def test_per_ticker_k(self) -> None:
        assert get_ticker_k("131890") == 0.3   # ACE 삼성그룹동일가중
        assert get_ticker_k("132030") == 0.7   # 골드선물
        assert get_ticker_k("999999") is None  # 없는 종목

    def test_all_whitelist_tickers_have_k(self) -> None:
        """모든 화이트리스트 종목에 K값이 정의되어 있어야 한다."""
        for ticker in AUTO_TRADE_WHITELIST:
            k = get_ticker_k(ticker)
            assert k is not None, f"{ticker} ({AUTO_TRADE_WHITELIST[ticker]}) has no K value in TICKER_K_MAP"

    def test_whitelist_is_union_of_etf_and_stock(self) -> None:
        """AUTO_TRADE_WHITELIST = ETF_WHITELIST + STOCK_WHITELIST."""
        expected = {**ETF_WHITELIST, **STOCK_WHITELIST}
        assert AUTO_TRADE_WHITELIST == expected

    def test_no_overlap_between_etf_and_stock(self) -> None:
        """ETF와 개별주 리스트에 중복이 없어야 한다."""
        overlap = set(ETF_WHITELIST) & set(STOCK_WHITELIST)
        assert overlap == set(), f"ETF/개별주 중복: {overlap}"

    def test_k_values_in_valid_range(self) -> None:
        """모든 K값은 0.1~0.9 범위여야 한다."""
        for ticker, k in TICKER_K_MAP.items():
            assert 0.1 <= k <= 0.9, f"{ticker}: K={k} out of range [0.1, 0.9]"

    def test_unknown_ticker_not_whitelisted(self) -> None:
        assert not is_whitelisted("000000")
        assert not is_etf("000000")

    def test_ai_semiconductor_etf(self) -> None:
        """KODEX AI반도체(395160) 정상 등록 확인."""
        assert is_whitelisted("395160")
        assert is_etf("395160")
        assert get_ticker_k("395160") == 0.5
