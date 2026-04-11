"""매크로 레짐 판별 — 지정학적 위기 + 유가 + 환율 반영.

현재 상황 (2026-04):
- 미국-이란 전쟁 → 호르무즈 해협 폐쇄 → 유가 $106 (전쟁 전 $70)
- 원달러 1,520원 돌파 → 외국인 자금 이탈
- 코스피 트럼프 발언 한마디에 ±200pt 변동

이런 시장에서 기술적 지표 기반 단타는 자살행위.
매크로 레짐을 먼저 판단하고, 위기 시 현금 비중을 높인다.

학술 근거:
- CBOE VIX > 30: "공포 구간" → 현금 비중 확대 (역사적 승률 낮음)
- 유가 급등 > 30%/3개월: 경기침체 선행 (Hamilton, 2003)
- 환율 급등 > 10%/3개월: 외국인 매도 압력 (한국 시장 특수)
- 전쟁/지정학 이벤트: S&P 평균 -6%, 6개월 후 +3.4% (CNBC 2026 분석)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("stock_analysis")


class MacroRegime(Enum):
    """매크로 레짐 상태."""
    CRISIS = "crisis"           # 전쟁/패닉 → 현금 100%, 거래 금지
    CAUTION = "caution"         # 불안정 → 포지션 축소, ETF만
    NORMAL = "normal"           # 정상 → 전 전략 가동


@dataclass
class MacroStatus:
    """매크로 상태 판단 결과."""
    regime: MacroRegime
    reasons: list[str]
    equity_ratio: float         # 주식 비중 (0.0~1.0)
    allowed_strategies: list[str]  # 허용 전략 목록
    oil_change_pct: float = 0.0
    fx_rate: float = 0.0
    vkospi: float = 0.0


def assess_macro(
    oil_price: float = 0.0,
    oil_price_3m_ago: float = 0.0,
    fx_rate: float = 0.0,
    fx_rate_3m_ago: float = 0.0,
    vkospi: float = 0.0,
    kospi_change_1d: float = 0.0,
    war_active: bool = False,
) -> MacroStatus:
    """매크로 레짐 판별.

    Args:
        oil_price: 현재 유가 (브렌트)
        oil_price_3m_ago: 3개월 전 유가
        fx_rate: 현재 원달러 환율
        fx_rate_3m_ago: 3개월 전 환율
        vkospi: 한국 VIX (VKOSPI)
        kospi_change_1d: 코스피 전일 대비 등락률 (%)
        war_active: 전쟁/지정학 위기 진행 중 여부

    Returns:
        MacroStatus
    """
    reasons = []
    crisis_score = 0

    # 1) 전쟁/지정학 위기
    if war_active:
        crisis_score += 3
        reasons.append("지정학적 위기 진행 중 (전쟁)")

    # 2) 유가 급등 (3개월 +30% 이상 = 경기침체 선행 신호)
    oil_change = 0.0
    if oil_price > 0 and oil_price_3m_ago > 0:
        oil_change = (oil_price / oil_price_3m_ago - 1) * 100
        if oil_change >= 40:
            crisis_score += 3
            reasons.append(f"유가 3개월 +{oil_change:.0f}% 급등 (공급 충격)")
        elif oil_change >= 20:
            crisis_score += 2
            reasons.append(f"유가 3개월 +{oil_change:.0f}% 상승")
        elif oil_change >= 10:
            crisis_score += 1
            reasons.append(f"유가 3개월 +{oil_change:.0f}% 상승 (주의)")

    # 3) 환율 급등 (1,450원 이상 = 외국인 이탈)
    if fx_rate >= 1500:
        crisis_score += 2
        reasons.append(f"환율 {fx_rate:.0f}원 (1,500원 초과, 외국인 이탈)")
    elif fx_rate >= 1400:
        crisis_score += 1
        reasons.append(f"환율 {fx_rate:.0f}원 (1,400원 초과)")

    fx_change = 0.0
    if fx_rate > 0 and fx_rate_3m_ago > 0:
        fx_change = (fx_rate / fx_rate_3m_ago - 1) * 100
        if fx_change >= 10:
            crisis_score += 1
            reasons.append(f"환율 3개월 +{fx_change:.0f}% 급등")

    # 4) VKOSPI (한국 VIX)
    if vkospi >= 35:
        crisis_score += 2
        reasons.append(f"VKOSPI {vkospi:.1f} (극도의 공포)")
    elif vkospi >= 25:
        crisis_score += 1
        reasons.append(f"VKOSPI {vkospi:.1f} (불안)")

    # 5) 코스피 일일 급변 (±3% 이상)
    if abs(kospi_change_1d) >= 3:
        crisis_score += 1
        reasons.append(f"코스피 전일 대비 {kospi_change_1d:+.1f}% (급변)")

    # ── 레짐 판정 ──
    # 위기에도 돈 버는 섹터는 있다. 100% 현금은 틀린 판단.
    # 위기 시 → 위기 수혜 ETF로 로테이션 (방산/에너지/인버스)
    if crisis_score >= 5:
        regime = MacroRegime.CRISIS
        equity_ratio = 0.5          # 50%는 위기 수혜 ETF, 50% 현금
        allowed = ["crisis_rotation"]  # 위기 전용 전략만
    elif crisis_score >= 3:
        regime = MacroRegime.CAUTION
        equity_ratio = 0.5
        allowed = ["crisis_rotation", "momentum_rotation"]
    elif crisis_score >= 1:
        regime = MacroRegime.NORMAL
        equity_ratio = 0.7
        allowed = ["momentum_rotation", "vb", "connors_rsi", "combo"]
    else:
        regime = MacroRegime.NORMAL
        equity_ratio = 1.0
        allowed = ["momentum_rotation", "vb", "connors_rsi", "combo"]

    if not reasons:
        reasons.append("매크로 정상")

    logger.info(
        "[매크로 레짐] %s (점수=%d, 주식비중=%.0f%%): %s",
        regime.value, crisis_score, equity_ratio * 100, " / ".join(reasons),
    )

    return MacroStatus(
        regime=regime,
        reasons=reasons,
        equity_ratio=equity_ratio,
        allowed_strategies=allowed,
        oil_change_pct=oil_change,
        fx_rate=fx_rate,
        vkospi=vkospi,
    )


# ── 매크로 오버라이드 파일 (텔레그램 명령으로 전환) ──

import json
from pathlib import Path as _Path

_MACRO_OVERRIDE_PATH = _Path(__file__).parent.parent / "data" / "macro_override.json"


def _load_override() -> dict:
    """data/macro_override.json 로드. 없으면 빈 dict."""
    if not _MACRO_OVERRIDE_PATH.exists():
        return {}
    try:
        with open(_MACRO_OVERRIDE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_macro_override(overrides: dict) -> None:
    """매크로 오버라이드 저장. 텔레그램 /레짐 명령에서 호출.

    예: save_macro_override({"war_active": False, "oil_price": 80})
    """
    _MACRO_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_MACRO_OVERRIDE_PATH, "w", encoding="utf-8") as f:
        json.dump(overrides, f, ensure_ascii=False, indent=2)
    logger.info("[매크로] 오버라이드 저장: %s", overrides)


def _fetch_macro_data() -> dict:
    """yfinance에서 매크로 데이터 자동 수집. 실패 시 빈 dict."""
    import yfinance as yf
    result = {}
    try:
        # 유가 (Brent Crude)
        bz = yf.Ticker("BZ=F")
        bz_hist = bz.history(period="3mo")
        if len(bz_hist) >= 2:
            result["oil_price"] = float(bz_hist["Close"].iloc[-1])
            result["oil_price_3m_ago"] = float(bz_hist["Close"].iloc[0])

        # 환율 (USD/KRW)
        fx = yf.Ticker("KRW=X")
        fx_hist = fx.history(period="3mo")
        if len(fx_hist) >= 2:
            result["fx_rate"] = float(fx_hist["Close"].iloc[-1])
            result["fx_rate_3m_ago"] = float(fx_hist["Close"].iloc[0])

        # VKOSPI (한국 VIX) - yfinance에서 직접 불가, KOSPI 변동성으로 대체
        kospi = yf.Ticker("^KS11")
        kospi_hist = kospi.history(period="5d")
        if len(kospi_hist) >= 2:
            result["kospi_change_1d"] = float(
                (kospi_hist["Close"].iloc[-1] / kospi_hist["Close"].iloc[-2] - 1) * 100
            )
    except Exception as e:
        logger.warning("[매크로] 자동 수집 실패 (무시): %s", e)
    return result


def assess_current() -> MacroStatus:
    """현재 매크로 상태 판별.

    1. yfinance에서 유가/환율/KOSPI 자동 수집
    2. 기본값 (자동 수집 실패 시 보수적)
    3. data/macro_override.json 오버라이드 (텔레그램 /레짐 명령)
    우선순위: override > auto_data > defaults
    """
    # 1. 자동 수집
    auto_data = _fetch_macro_data()

    # 2. 기본값: 보수적 (CAUTION). 자동 수집 실패 시 신중 모드.
    defaults = {
        "oil_price": 90,
        "oil_price_3m_ago": 72,
        "fx_rate": 1450,
        "fx_rate_3m_ago": 1350,
        "vkospi": 22,
        "kospi_change_1d": 0.0,
        "war_active": False,
    }

    # 3. 오버라이드 (텔레그램 명령)
    overrides = _load_override()
    if not overrides:
        logger.warning("[매크로] 오버라이드 파일 없음 — 보수적 기본값 사용")

    # 우선순위: override > auto_data > defaults
    merged = {**defaults, **auto_data, **overrides}

    return assess_macro(**merged)


if __name__ == "__main__":
    status = assess_current()
    print(f"레짐: {status.regime.value}")
    print(f"주식 비중: {status.equity_ratio * 100:.0f}%")
    print(f"허용 전략: {status.allowed_strategies}")
    print(f"사유:")
    for r in status.reasons:
        print(f"  - {r}")
