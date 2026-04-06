"""위기 로테이션 전략 — 전쟁/지정학 위기 시 수혜 섹터로 회전.

2026-04 이란 전쟁 상황:
- 코스피 전체 -20% (3월), 반도체 -20%
- 방산주 랠리, 에너지 +40%, 인버스 ETF +20%
- 종전 기대 시 하루만에 코스피 +8.4%

전략:
1. 전쟁 진행 중 → 방산/에너지/인버스 ETF 보유
2. 종전 신호 시 → 즉시 매도하고 낙폭과대 성장주로 전환
3. 듀얼 모멘텀 원리 적용: 수혜 ETF 중 가장 강한 것에 집중

사용 가능한 한국 ETF:
- 261220: KODEX WTI원유선물(H) — 유가 수혜
- 130730: KODEX 인버스 — 코스피 하락 수혜
- 117700: KODEX 건설 — 전후 재건 수혜 (미래 포지셔닝)
- 132030: KODEX 골드선물(H) — 안전자산
- 364690: TIGER 방산 — 방산 수혜 (유동성 확인 필요)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("stock_analysis")


# 위기 시 매수 후보 ETF
CRISIS_ETF_UNIVERSE: dict[str, dict] = {
    "261220": {
        "name": "KODEX WTI원유선물(H)",
        "category": "에너지",
        "crisis_type": ["war", "oil_shock"],
        "description": "유가 상승 시 직접 수혜",
    },
    "130730": {
        "name": "KODEX 인버스",
        "category": "인버스",
        "crisis_type": ["war", "recession", "panic"],
        "description": "코스피 하락 시 수익",
    },
    "132030": {
        "name": "KODEX 골드선물(H)",
        "category": "안전자산",
        "crisis_type": ["war", "inflation", "panic"],
        "description": "안전자산 수요 + 인플레이션 헤지",
    },
    "364690": {
        "name": "TIGER 방산",
        "category": "방산",
        "crisis_type": ["war"],
        "description": "전쟁 수혜 직접 테마",
    },
}

# 종전/회복 시 매수 후보 ETF (낙폭과대 반등)
RECOVERY_ETF_UNIVERSE: dict[str, dict] = {
    "069500": {
        "name": "KODEX 200",
        "category": "시장대표",
        "description": "코스피 전체 반등",
    },
    "229200": {
        "name": "KODEX 코스닥150",
        "category": "성장주",
        "description": "낙폭과대 성장주 반등",
    },
    "091170": {
        "name": "KODEX 은행",
        "category": "금융",
        "description": "환율 안정 시 금융주 반등",
    },
}


@dataclass
class CrisisSignal:
    """위기 로테이션 신호."""
    action: str               # "buy_crisis" | "hold" | "switch_recovery" | "none"
    tickers: list[str]         # 매수 대상 종목 코드
    names: list[str]           # 종목명
    reasons: list[str]
    allocation: dict[str, float]  # {ticker: 비중 0~1}


def evaluate_crisis_rotation(
    war_active: bool = True,
    oil_trend: str = "rising",      # "rising" | "stable" | "falling"
    peace_signal: bool = False,      # 종전 협상/기대감 여부
    kospi_change_5d: float = 0.0,   # 코스피 5일 등락률
) -> CrisisSignal:
    """위기 상황에서 어떤 ETF를 사야 하는지 판단.

    Args:
        war_active: 전쟁 진행 중
        oil_trend: 유가 추세
        peace_signal: 종전/평화 신호 있음
        kospi_change_5d: 코스피 최근 5일 등락률
    """
    # 종전 신호 → 위기 ETF 매도, 회복 ETF로 전환
    if peace_signal:
        tickers = list(RECOVERY_ETF_UNIVERSE.keys())
        names = [v["name"] for v in RECOVERY_ETF_UNIVERSE.values()]
        n = len(tickers)
        allocation = {t: 1.0 / n for t in tickers}
        return CrisisSignal(
            action="switch_recovery",
            tickers=tickers,
            names=names,
            reasons=[
                "종전/평화 신호 감지",
                "위기 수혜 ETF 매도 → 낙폭과대 반등 ETF 매수",
                "역사적 평균: 종전 후 6개월 +3.4% 반등",
            ],
            allocation=allocation,
        )

    # 전쟁 진행 중
    if war_active:
        selected = {}
        reasons = []

        # 유가 상승 중 → 원유 ETF
        if oil_trend == "rising":
            selected["261220"] = 0.35
            reasons.append("유가 상승 추세 → WTI원유 ETF 35%")

        # 방산 항상 포함
        selected["364690"] = 0.25
        reasons.append("전쟁 진행 → 방산 ETF 25%")

        # 코스피 하락 추세 → 인버스
        if kospi_change_5d < -2:
            selected["130730"] = 0.25
            reasons.append(f"코스피 5일 {kospi_change_5d:+.1f}% → 인버스 25%")
        else:
            # 코스피 반등 중이면 인버스 대신 금
            selected["132030"] = 0.15
            reasons.append("금 ETF 15% (안전자산)")

        # 비중 정규화
        total = sum(selected.values())
        if total > 0:
            selected = {k: v / total for k, v in selected.items()}

        tickers = list(selected.keys())
        names = [CRISIS_ETF_UNIVERSE[t]["name"] for t in tickers]

        return CrisisSignal(
            action="buy_crisis",
            tickers=tickers,
            names=names,
            reasons=reasons,
            allocation=selected,
        )

    return CrisisSignal(
        action="none",
        tickers=[], names=[],
        reasons=["위기 상황 아님"],
        allocation={},
    )


if __name__ == "__main__":
    print("=== 현재 상황: 전쟁 진행, 유가 상승, 코스피 급락 ===")
    signal = evaluate_crisis_rotation(
        war_active=True,
        oil_trend="rising",
        peace_signal=False,
        kospi_change_5d=-5.0,
    )
    print(f"행동: {signal.action}")
    for t, name in zip(signal.tickers, signal.names):
        pct = signal.allocation.get(t, 0) * 100
        print(f"  {name} ({t}): {pct:.0f}%")
    for r in signal.reasons:
        print(f"  - {r}")

    print("\n=== 종전 신호 발생 시 ===")
    signal2 = evaluate_crisis_rotation(
        war_active=True,
        oil_trend="falling",
        peace_signal=True,
        kospi_change_5d=+8.0,
    )
    print(f"행동: {signal2.action}")
    for t, name in zip(signal2.tickers, signal2.names):
        pct = signal2.allocation.get(t, 0) * 100
        print(f"  {name} ({t}): {pct:.0f}%")
    for r in signal2.reasons:
        print(f"  - {r}")
