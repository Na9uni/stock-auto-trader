"""트레이딩 설정 통합 모듈 — 백테스터와 실전이 동일한 설정을 공유한다."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class TradingConfig:
    """불변 트레이딩 설정. .env 또는 dict에서 생성."""

    # 운영 모드
    operation_mode: str = "OBSERVE"
    mock_mode: bool = True
    auto_trade_enabled: bool = False

    # 포지션
    auto_trade_amount: int = 150_000
    max_order_amount: int = 300_000
    max_slots: int = 2

    # 손절/익절 (%)
    stoploss_pct: float = 2.0
    trailing_activate_pct: float = 2.5
    trailing_stop_pct: float = 1.0

    # 손실 방어
    max_daily_loss: int = 30_000
    max_monthly_loss: int = 100_000
    max_consec_stoploss: int = 2

    # 비용 모델
    commission_rate: float = 0.00015   # 편도 0.015%
    tax_rate: float = 0.0018           # 매도 세금 0.18%

    # 신호 판정 (합산 전략용)
    strong_threshold_5m: int = 8
    strong_threshold_daily: int = 7

    # 변동성 돌파 전략
    vb_k: float = 0.5
    vb_k_individual: float = 0.6       # 개별주 K값 (노이즈 필터링 강화)

    # 전략 선택: "vb" | "score" | "combo" | "trend" | "auto"
    strategy: str = "auto"

    # 시간 제한
    buy_start_minute: int = 10         # 장 시작 후 N분 뒤부터 매수 허용
    buy_end_hour: int = 15             # 이 시간 이후 신규 매수 차단 (_auto_trade 공통)
    eod_liquidation: bool = True       # 15:20 당일 강제 청산 (False=스윙 모드)

    # 레짐 엔진 파라미터
    regime_defense_trigger_pct: float = -2.0     # DEFENSE 전환 KOSPI 등락률 (%)
    regime_cash_trigger_pct: float = -3.0        # CASH 전환 추가 하락률 (%)
    regime_swing_volatility_pct: float = 3.0     # SWING 전환 변동성 기준 (%)
    regime_deescalation_cooldown_min: int = 30   # 디에스컬레이션 쿨다운 (분)

    @classmethod
    def from_env(cls) -> TradingConfig:
        """실전: .env에서 로드."""
        return cls(
            operation_mode=os.getenv("OPERATION_MODE", "OBSERVE").upper(),
            mock_mode=os.getenv("KIWOOM_MOCK_MODE", "True").lower() == "true",
            auto_trade_enabled=os.getenv("AUTO_TRADE_ENABLED", "false").lower() == "true",
            auto_trade_amount=int(os.getenv("AUTO_TRADE_AMOUNT", "150000")),
            max_order_amount=int(os.getenv("MAX_ORDER_AMOUNT", "300000")),
            max_slots=int(os.getenv("MAX_SLOTS", "2")),
            stoploss_pct=float(os.getenv("STOPLOSS_PCT", "2.0")),
            trailing_activate_pct=float(os.getenv("TRAILING_ACTIVATE_PCT", "2.5")),
            trailing_stop_pct=float(os.getenv("TRAILING_STOP_PCT", "1.0")),
            max_daily_loss=int(os.getenv("MAX_DAILY_LOSS", "30000")),
            max_monthly_loss=int(os.getenv("MAX_MONTHLY_LOSS", "100000")),
            max_consec_stoploss=int(os.getenv("MAX_CONSEC_STOPLOSS", "2")),
            commission_rate=float(os.getenv("COMMISSION_RATE", "0.00015")),
            tax_rate=float(os.getenv("TAX_RATE", "0.0018")),
            strong_threshold_5m=int(os.getenv("STRONG_THRESHOLD_5M", "8")),
            strong_threshold_daily=int(os.getenv("STRONG_THRESHOLD_DAILY", "7")),
            vb_k=float(os.getenv("VB_K", "0.5")),
            vb_k_individual=float(os.getenv("VB_K_INDIVIDUAL", "0.6")),
            strategy=os.getenv("STRATEGY", "auto").lower(),
            buy_start_minute=int(os.getenv("BUY_START_MINUTE", "10")),
            buy_end_hour=int(os.getenv("BUY_END_HOUR", "15")),
            eod_liquidation=os.getenv("EOD_LIQUIDATION", "true").lower() == "true",
            regime_defense_trigger_pct=float(os.getenv("REGIME_DEFENSE_TRIGGER_PCT", "-2.0")),
            regime_cash_trigger_pct=float(os.getenv("REGIME_CASH_TRIGGER_PCT", "-3.0")),
            regime_swing_volatility_pct=float(os.getenv("REGIME_SWING_VOLATILITY_PCT", "3.0")),
            regime_deescalation_cooldown_min=int(os.getenv("REGIME_DEESCALATION_COOLDOWN_MIN", "30")),
        )

    @classmethod
    def from_dict(cls, overrides: dict) -> TradingConfig:
        """백테스트: 기본값 + 오버라이드."""
        base = cls.from_env()
        fields = {f.name for f in base.__dataclass_fields__.values()}
        merged = {}
        for f_name in fields:
            merged[f_name] = overrides.get(f_name, getattr(base, f_name))
        return cls(**merged)

    def protection_margin(self, price: int) -> float:
        """가격대별 보호 지정가 마진 (실전 + 백테스트 공용)."""
        if price < 5_000:
            return 0.01
        elif price < 20_000:
            return 0.005
        elif price < 50_000:
            return 0.004
        else:
            return 0.003

    def buy_cost(self, price: int, qty: int) -> int:
        """매수 수수료."""
        return int(price * qty * self.commission_rate)

    def sell_cost(self, price: int, qty: int) -> tuple[int, int]:
        """매도 수수료 + 세금. Returns (commission, tax)."""
        revenue = price * qty
        return int(revenue * self.commission_rate), int(revenue * self.tax_rate)

    def is_etf(self, ticker: str) -> bool:
        """ETF 여부 (세금 면제 판단용)."""
        return ticker in _ETF_TICKERS


# ETF 종목 코드 (세금 면제)
_ETF_TICKERS = frozenset({
    "069500",  # KODEX 200
    "229200",  # KODEX 코스닥150
    "133690",  # TIGER 미국나스닥100
    "131890",  # ACE 삼성그룹동일가중
    "108450",  # ACE 삼성그룹섹터가중
    "395160",  # KODEX AI반도체
    # 위기 로테이션 ETF
    "261220",  # KODEX WTI원유선물(H)
    "130730",  # KODEX 인버스
    "132030",  # KODEX 골드선물(H)
    # 모멘텀 로테이션 ETF
    "360750",  # TIGER 미국S&P500
    "371460",  # TIGER 차이나전기차SOLACTIVE
    "161510",  # TIGER 단기채권액티브 (현금 대용)
    "364690",  # TIGER 방산
    "091170",  # KODEX 은행
    # 2026-04-17 확장판 추가 ETF (세금 면제 대상)
    "102780",  # KODEX 삼성그룹
    "114800",  # KODEX 인버스 (위 130730과 다른 인덱스 추적)
    "252670",  # KODEX 200선물인버스2X
    "122630",  # KODEX 레버리지
    "091160",  # KODEX 반도체
    "117460",  # KODEX 철강
    "305720",  # KODEX 2차전지산업
    "176950",  # KODEX 미국달러선물
    "251340",  # KODEX 코스닥150선물인버스
    "465580",  # ACE 미국배당다우존스
    "294400",  # KODEX 리츠
})
