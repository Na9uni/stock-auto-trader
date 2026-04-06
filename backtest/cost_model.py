"""백테스트 비용 모델 — 현실적 슬리피지 반영.

보호지정가 마진 ≠ 실제 슬리피지.
보호지정가는 "체결 보장"용으로 넓게 잡지만,
실제 체결가는 호가창 유동성에 따라 시장가 근처에서 체결된다.

유동성별 실제 슬리피지:
- ETF(KODEX200 등): 1~2틱 = 약 0.03~0.05%
- 대형주(삼성전자 등): 2~3틱 = 약 0.05~0.1%
- 중형주(KB금융 등): 3~5틱 = 약 0.1~0.2%
- 소형주(보성파워텍 등): 5~10틱 = 약 0.2~0.5%
"""

from __future__ import annotations

from config.trading_config import TradingConfig


class CostModel:
    """현실적 비용 모델."""

    def __init__(self, config: TradingConfig):
        self._config = config

    def slippage(self, price: int, ticker: str = "") -> float:
        """실제 슬리피지 (보호마진이 아닌 체결 기대치).

        보호마진(0.3~1.0%)은 체결 보장용 한도이며, 실제 체결가는
        호가창 유동성에 따라 시장가 근처에서 결정된다.
        이 값은 실전 체결 데이터를 기반으로 한 보수적 추정치.
        """
        if self._config.is_etf(ticker):
            return 0.0005   # 0.05% (ETF: 유동성 풍부)
        if price >= 50_000:
            return 0.001    # 0.1% (대형주)
        if price >= 20_000:
            return 0.0015   # 0.15% (중형주)
        if price >= 5_000:
            return 0.003    # 0.3% (소형주)
        return 0.005        # 0.5% (초소형주)

    def buy_execution_price(self, signal_price: int, next_open: int,
                            ticker: str = "") -> int:
        """매수 체결가 = 시장가 + 슬리피지."""
        slip = self.slippage(next_open, ticker)
        return int(next_open * (1 + slip))

    def sell_execution_price(self, signal_price: int, next_open: int,
                             ticker: str = "") -> int:
        """매도 체결가 = 시장가 - 슬리피지."""
        slip = self.slippage(next_open, ticker)
        return int(next_open * (1 - slip))

    def buy_cost(self, price: int, qty: int) -> int:
        """매수 수수료."""
        return self._config.buy_cost(price, qty)

    def sell_cost(self, price: int, qty: int, ticker: str = "") -> tuple[int, int]:
        """매도 수수료 + 세금. ETF는 세금 면제."""
        commission = int(price * qty * self._config.commission_rate)
        if self._config.is_etf(ticker):
            tax = 0
        else:
            tax = int(price * qty * self._config.tax_rate)
        return commission, tax

    def roundtrip_cost_pct(self, price: int, ticker: str = "") -> float:
        """왕복 비용률 (%)."""
        slip = self.slippage(price, ticker)
        comm = self._config.commission_rate * 2
        tax = 0.0 if self._config.is_etf(ticker) else self._config.tax_rate
        return (slip * 2 + comm + tax) * 100
