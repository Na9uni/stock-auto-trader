"""
signal_detector.py
5분봉/일봉 기반 매수·매도 신호 감지 모듈
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

logger = logging.getLogger("stock_analysis")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SignalType(Enum):
    BUY = "buy"
    SELL = "sell"
    NEUTRAL = "neutral"


class SignalStrength(Enum):
    STRONG = "strong"
    MEDIUM = "medium"
    WEAK = "weak"


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class SignalResult:
    signal_type: SignalType
    strength: SignalStrength
    score: int
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rsi: float = float("nan")
    macd_cross: str | None = None   # "golden" / "dead" / None
    vol_ratio: float = float("nan")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_float(value, default: float | None = None) -> float | None:
    """값이 NaN이거나 None이면 default 반환, 그 외 float 반환."""
    if value is None:
        return default
    try:
        v = float(value)
        return default if np.isnan(v) else v
    except (TypeError, ValueError):
        return default


def _get_row(df: pd.DataFrame, iloc: int) -> dict:
    """iloc 인덱스의 행을 dict로 반환. 존재하지 않으면 빈 dict."""
    if (len(df) <= abs(iloc)) if iloc < 0 else (len(df) <= iloc):
        return {}
    return df.iloc[iloc].to_dict()


def _determine_strength(score: int, strong_threshold: int) -> tuple[SignalType, SignalStrength]:
    """점수로 SignalType·SignalStrength 결정."""
    abs_score = abs(score)

    if score == 0:
        return SignalType.NEUTRAL, SignalStrength.WEAK

    signal_type = SignalType.BUY if score > 0 else SignalType.SELL

    if abs_score >= strong_threshold:
        strength = SignalStrength.STRONG
    elif abs_score >= 3:
        strength = SignalStrength.MEDIUM
    else:
        strength = SignalStrength.WEAK

    return signal_type, strength


def _compute_vol_ratio(df: pd.DataFrame) -> float | None:
    """
    현재 거래량 / 최근 20봉 평균 거래량.
    컬럼명: 'volume' 또는 '거래량'
    """
    vol_col = None
    for candidate in ("volume", "거래량", "Volume"):
        if candidate in df.columns:
            vol_col = candidate
            break
    if vol_col is None:
        return None

    series = df[vol_col]
    if len(series) < 2:
        return None

    current_vol = _safe_float(series.iloc[-1])
    if current_vol is None:
        return None

    window = series.iloc[max(0, len(series) - 21):-1]
    if len(window) == 0:
        return None

    avg_vol = window.mean()
    if pd.isna(avg_vol) or avg_vol == 0:
        return None

    return current_vol / avg_vol


# ---------------------------------------------------------------------------
# Score calculators
# ---------------------------------------------------------------------------

def _buy_scores(row: dict, prev_row: dict, df: pd.DataFrame) -> tuple[int, list[str]]:
    """매수 점수 합산. (점수, reasons) 반환."""
    score = 0
    reasons: list[str] = []

    # 1. RSI 과매도 < 30 : +2
    rsi = _safe_float(row.get("rsi"))
    if rsi is not None and rsi < 30:
        score += 2
        reasons.append(f"RSI 과매도({rsi:.1f})")

    # 2. RSI 30~45 반등 중 : +1
    elif rsi is not None and 30 <= rsi <= 45:
        prev_rsi = _safe_float(prev_row.get("rsi"))
        if prev_rsi is not None and rsi > prev_rsi:
            score += 1
            reasons.append(f"RSI 반등 중({prev_rsi:.1f}→{rsi:.1f})")

    # 3. MACD 골든크로스 (macd_hist 음→양 전환) : +2
    macd_hist = _safe_float(row.get("macd_hist"))
    prev_macd_hist = _safe_float(prev_row.get("macd_hist"))
    macd_cross: str | None = None

    if (macd_hist is not None and prev_macd_hist is not None
            and prev_macd_hist < 0 and macd_hist >= 0):
        score += 2
        reasons.append("MACD 골든크로스")
        macd_cross = "golden"
    elif (macd_hist is not None and prev_macd_hist is not None
          and prev_macd_hist > 0 and macd_hist <= 0):
        macd_cross = "dead"

    # 4. MACD 히스토그램 증가 중 : +1 (골든크로스 봉에서는 중복 가산 방지)
    if (macd_cross != "golden"
            and macd_hist is not None and prev_macd_hist is not None
            and macd_hist > prev_macd_hist):
        score += 1
        reasons.append("MACD 히스토그램 증가")

    # 5. 볼린저 하단 터치/이탈 후 복귀 : +2
    close = _safe_float(row.get("close") or row.get("종가"))
    bb_lower = _safe_float(row.get("bb_lower") or row.get("볼린저하단"))
    prev_close = _safe_float(prev_row.get("close") or prev_row.get("종가"))
    prev_bb_lower = _safe_float(prev_row.get("bb_lower") or prev_row.get("볼린저하단"))

    if (close is not None and bb_lower is not None
            and prev_close is not None and prev_bb_lower is not None):
        if prev_close <= prev_bb_lower and close > bb_lower:
            score += 2
            reasons.append("볼린저 하단 복귀")

    # 6~10. 이평선 관련 점수 (ADX < 20 횡보장이면 50% 감점)
    ma5 = _safe_float(row.get("ma5") or row.get("MA5"))
    ma20 = _safe_float(row.get("ma20") or row.get("MA20"))
    ma60 = _safe_float(row.get("ma60") or row.get("MA60"))
    adx_val = _safe_float(row.get("adx"))

    ma_score = 0
    ma_reasons: list[str] = []

    # 6. MA5 > MA20 정배열 : +1
    if ma5 is not None and ma20 is not None and ma5 > ma20:
        ma_score += 1
        ma_reasons.append("MA5>MA20 정배열")

    # 7. MA20 상승 추세 (최근 5봉 기울기 > 0.1%) : +1
    ma20_col = None
    for c in ("ma20", "MA20"):
        if c in df.columns:
            ma20_col = c
            break
    if ma20_col is not None and len(df) >= 5:
        ma20_series = df[ma20_col].dropna()
        if len(ma20_series) >= 5:
            recent = ma20_series.iloc[-5:]
            base = _safe_float(recent.iloc[0])
            last = _safe_float(recent.iloc[-1])
            if base is not None and last is not None and base > 0:
                slope_pct = (last - base) / base * 100
                if slope_pct > 0.1:
                    ma_score += 1
                    ma_reasons.append(f"MA20 상승 추세(+{slope_pct:.2f}%)")

    # 8. MA5 지지 근접 (종가가 MA5의 ±1% 이내이고 MA5 위) : +1
    if close is not None and ma5 is not None and ma5 > 0:
        diff_pct = abs(close - ma5) / ma5 * 100
        if diff_pct <= 1.0 and close >= ma5:
            ma_score += 1
            ma_reasons.append(f"MA5 지지 근접(diff={diff_pct:.2f}%)")

    # 10. 3중 정배열 (MA5>MA20>MA60) : +1  (일봉은 +2로 오버라이드됨)
    if (ma5 is not None and ma20 is not None and ma60 is not None
            and ma5 > ma20 > ma60):
        ma_score += 1
        ma_reasons.append("3중 정배열(MA5>MA20>MA60)")

    # ADX < 20 횡보장: 이평선 관련 점수 50% 감점
    if adx_val is not None and adx_val < 20 and ma_score > 0:
        original_ma = ma_score
        ma_score = ma_score // 2
        ma_reasons.append(f"ADX 횡보({adx_val:.1f}) → MA 점수 감점({original_ma}→{ma_score})")

    score += ma_score
    reasons.extend(ma_reasons)

    # 9. 거래량 2배 이상 급증 : +1
    vol_ratio = _compute_vol_ratio(df)
    if vol_ratio is not None and vol_ratio >= 2.0:
        score += 1
        reasons.append(f"거래량 급증({vol_ratio:.1f}배)")

    # 11. 스토캐스틱 과매도 (%K < 20 AND %D < 20) : +1
    stoch_k = _safe_float(row.get("stoch_k"))
    stoch_d = _safe_float(row.get("stoch_d"))
    if stoch_k is not None and stoch_d is not None and stoch_k < 20 and stoch_d < 20:
        score += 1
        reasons.append(f"스토캐스틱 과매도(K={stoch_k:.1f}, D={stoch_d:.1f})")

    # 12. OBV 상승 다이버전스 (가격 하락 but OBV 상승, 5봉 기준) : +1
    obv_col = "obv" if "obv" in df.columns else None
    close_col = "close" if "close" in df.columns else ("종가" if "종가" in df.columns else None)
    if obv_col is not None and close_col is not None and len(df) >= 5:
        close_5ago = _safe_float(df[close_col].iloc[-5])
        close_now = _safe_float(df[close_col].iloc[-1])
        obv_5ago = _safe_float(df[obv_col].iloc[-5])
        obv_now = _safe_float(df[obv_col].iloc[-1])
        if (close_5ago is not None and close_now is not None
                and obv_5ago is not None and obv_now is not None):
            if close_now < close_5ago and obv_now > obv_5ago:
                score += 1
                reasons.append("OBV 상승 다이버전스")

    # 13. 지지선 근접 (종가가 support의 ±1% 이내이고 support 위) : +1
    support = _safe_float(row.get("support"))
    if close is not None and support is not None and support > 0:
        support_diff_pct = abs(close - support) / support * 100
        if support_diff_pct <= 1.0 and close >= support:
            score += 1
            reasons.append(f"지지선 근접(diff={support_diff_pct:.2f}%)")

    # 14. VWAP 상회 : +1
    vwap = _safe_float(row.get("vwap"))
    if close is not None and vwap is not None and close > vwap:
        score += 1
        reasons.append("VWAP 상회")

    return score, reasons


def _sell_scores(row: dict, prev_row: dict, df: pd.DataFrame | None = None) -> tuple[int, list[str]]:
    """매도 점수 합산(음수). (점수, reasons) 반환."""
    score = 0
    reasons: list[str] = []

    # 1. RSI > 70 과매수 : -2
    rsi = _safe_float(row.get("rsi"))
    if rsi is not None and rsi > 70:
        score -= 2
        reasons.append(f"RSI 과매수({rsi:.1f})")

    # 2. MACD 데드크로스 : -2
    macd_hist = _safe_float(row.get("macd_hist"))
    prev_macd_hist = _safe_float(prev_row.get("macd_hist"))
    if (macd_hist is not None and prev_macd_hist is not None
            and prev_macd_hist > 0 and macd_hist <= 0):
        score -= 2
        reasons.append("MACD 데드크로스")

    # 3. 볼린저 상단 터치 후 하락 : -1
    close = _safe_float(row.get("close") or row.get("종가"))
    bb_upper = _safe_float(row.get("bb_upper") or row.get("볼린저상단"))
    prev_close = _safe_float(prev_row.get("close") or prev_row.get("종가"))
    prev_bb_upper = _safe_float(prev_row.get("bb_upper") or prev_row.get("볼린저상단"))

    if (close is not None and bb_upper is not None
            and prev_close is not None and prev_bb_upper is not None):
        if prev_close >= prev_bb_upper and close < bb_upper:
            score -= 1
            reasons.append("볼린저 상단 하락 이탈")

    # 4. MA5 < MA20 역배열 : -1
    ma5 = _safe_float(row.get("ma5") or row.get("MA5"))
    ma20 = _safe_float(row.get("ma20") or row.get("MA20"))
    ma60 = _safe_float(row.get("ma60") or row.get("MA60"))
    if ma5 is not None and ma20 is not None and ma5 < ma20:
        score -= 1
        reasons.append("MA5<MA20 역배열")

    # 5. 거래량 급감 (vol_ratio < 0.3) : -1  (이 함수 호출 전 df 없으므로 외부에서 주입)
    # → caller 에서 처리

    # 6. 3중 역배열 (MA5 < MA20 < MA60) : -2
    if (ma5 is not None and ma20 is not None and ma60 is not None
            and ma5 < ma20 < ma60):
        score -= 2
        reasons.append("3중 역배열(MA5<MA20<MA60)")

    # 7. 스토캐스틱 과매수 (%K > 80 AND %D > 80) : -1
    stoch_k = _safe_float(row.get("stoch_k"))
    stoch_d = _safe_float(row.get("stoch_d"))
    if stoch_k is not None and stoch_d is not None and stoch_k > 80 and stoch_d > 80:
        score -= 1
        reasons.append(f"스토캐스틱 과매수(K={stoch_k:.1f}, D={stoch_d:.1f})")

    # 8. OBV 하락 다이버전스 (가격 상승 but OBV 하락, 5봉 기준) : -1
    if df is not None:
        obv_col = "obv" if "obv" in df.columns else None
        close_col = "close" if "close" in df.columns else ("종가" if "종가" in df.columns else None)
        if obv_col is not None and close_col is not None and len(df) >= 5:
            close_5ago = _safe_float(df[close_col].iloc[-5])
            close_now = _safe_float(df[close_col].iloc[-1])
            obv_5ago = _safe_float(df[obv_col].iloc[-5])
            obv_now = _safe_float(df[obv_col].iloc[-1])
            if (close_5ago is not None and close_now is not None
                    and obv_5ago is not None and obv_now is not None):
                if close_now > close_5ago and obv_now < obv_5ago:
                    score -= 1
                    reasons.append("OBV 하락 다이버전스")

    # 9. 저항선 근접 (종가가 resistance의 ±1% 이내이고 resistance 아래) : -1
    resistance = _safe_float(row.get("resistance"))
    if close is not None and resistance is not None and resistance > 0:
        resist_diff_pct = abs(close - resistance) / resistance * 100
        if resist_diff_pct <= 1.0 and close <= resistance:
            score -= 1
            reasons.append(f"저항선 근접(diff={resist_diff_pct:.2f}%)")

    # 10. VWAP 하회 : -1
    vwap = _safe_float(row.get("vwap"))
    if close is not None and vwap is not None and close < vwap:
        score -= 1
        reasons.append("VWAP 하회")

    return score, reasons


def _compute_macd_cross(row: dict, prev_row: dict) -> str | None:
    """현재·이전 행의 macd_hist 로 골든/데드크로스 판단."""
    macd_hist = _safe_float(row.get("macd_hist"))
    prev_macd_hist = _safe_float(prev_row.get("macd_hist"))
    if macd_hist is None or prev_macd_hist is None:
        return None
    if prev_macd_hist < 0 and macd_hist >= 0:
        return "golden"
    if prev_macd_hist > 0 and macd_hist <= 0:
        return "dead"
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect(
    df: pd.DataFrame,
    exec_strength: float = 0.0,
    change_rate: float = 0.0,
) -> SignalResult:
    """
    5분봉 기반 단타 신호 감지.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV + 보조지표 컬럼 포함 DataFrame (원본 수정 안 함)
    exec_strength : float
        체결 강도 (현재 미사용, 확장 가능)
    change_rate : float
        당일 등락률 (%) — 예: -3.5 → -3.5%

    Returns
    -------
    SignalResult
    """
    if df is None or len(df) < 2:
        logger.warning("detect: DataFrame이 비어 있거나 행이 부족합니다.")
        return SignalResult(
            signal_type=SignalType.NEUTRAL,
            strength=SignalStrength.WEAK,
            score=0,
            reasons=["데이터 부족"],
        )

    row = _get_row(df, -1)
    prev_row = _get_row(df, -2)

    rsi_val = _safe_float(row.get("rsi"), default=float("nan"))
    vol_ratio_raw = _compute_vol_ratio(df)
    vol_ratio = vol_ratio_raw if vol_ratio_raw is not None else float("nan")
    macd_cross = _compute_macd_cross(row, prev_row)

    buy_score, buy_reasons = _buy_scores(row, prev_row, df)
    sell_score, sell_reasons = _sell_scores(row, prev_row, df)

    # 거래량 급감 매도 점수 (-1)
    _vol_ratio_val = _safe_float(vol_ratio)
    if _vol_ratio_val is not None and _vol_ratio_val < 0.3:
        sell_score -= 1
        sell_reasons.append(f"거래량 급감({_vol_ratio_val:.2f}배)")

    total_score = buy_score + sell_score

    warnings: list[str] = []

    # 경고: 매수 신호인데 매도 조건도 있는 경우
    if buy_score > 0 and sell_score < 0:
        for r in sell_reasons:
            warnings.append(f"매도 조건 병존: {r}")

    all_reasons = buy_reasons + sell_reasons

    # 무효화 1: 거래량 극히 부족 (0.5배 미만)
    if _vol_ratio_val is not None and _vol_ratio_val < 0.5:
        if buy_score > 0:
            all_reasons.append("거래량 극히 부족 → 매수 점수 무효")
            buy_score = 0
            total_score = sell_score  # 매도 점수만 남김

    # 무효화 2: 당일 -3% 이상 급락
    if change_rate <= -3.0:
        if buy_score > 0 or total_score > 0:
            all_reasons.append(f"당일 급락({change_rate:.1f}%) → 매수 신호 무효")
            buy_score = 0
            total_score = min(total_score, 0)

    signal_type, strength = _determine_strength(total_score, strong_threshold=6)

    return SignalResult(
        signal_type=signal_type,
        strength=strength,
        score=total_score,
        reasons=all_reasons,
        warnings=warnings,
        rsi=rsi_val,
        macd_cross=macd_cross,
        vol_ratio=vol_ratio,
    )


def detect_daily(
    df: pd.DataFrame,
    change_rate: float = 0.0,
) -> SignalResult:
    """
    일봉 기반 스윙 신호 감지.

    detect()와 같은 로직이나 다음이 다름:
    - STRONG 기준: |score| >= 5
    - 3중 정배열 시 +2 (단타는 +1)
    - MA20 상승 기울기 > 2% 이면 +1 추가
    - 가격↓ + 거래량↓ 다이버전스(매도 소진) +1
    - 거래량 무효화 조건 없음
    """
    if df is None or len(df) < 2:
        logger.warning("detect_daily: DataFrame이 비어 있거나 행이 부족합니다.")
        return SignalResult(
            signal_type=SignalType.NEUTRAL,
            strength=SignalStrength.WEAK,
            score=0,
            reasons=["데이터 부족"],
        )

    row = _get_row(df, -1)
    prev_row = _get_row(df, -2)

    rsi_val = _safe_float(row.get("rsi"), default=float("nan"))
    vol_ratio_raw = _compute_vol_ratio(df)
    vol_ratio = vol_ratio_raw if vol_ratio_raw is not None else float("nan")
    macd_cross = _compute_macd_cross(row, prev_row)

    buy_score, buy_reasons = _buy_scores(row, prev_row, df)
    sell_score, sell_reasons = _sell_scores(row, prev_row, df)

    # 거래량 급감 매도 점수 (-1)
    _vol_ratio_val = _safe_float(vol_ratio)
    if _vol_ratio_val is not None and _vol_ratio_val < 0.3:
        sell_score -= 1
        sell_reasons.append(f"거래량 급감({_vol_ratio_val:.2f}배)")

    # 일봉 전용: 3중 정배열 보너스 +1 추가 (buy_scores에서 이미 +1 부여 → 총 +2)
    ma5 = _safe_float(row.get("ma5") or row.get("MA5"))
    ma20 = _safe_float(row.get("ma20") or row.get("MA20"))
    ma60 = _safe_float(row.get("ma60") or row.get("MA60"))

    if (ma5 is not None and ma20 is not None and ma60 is not None
            and ma5 > ma20 > ma60):
        buy_score += 1
        buy_reasons.append("3중 정배열 일봉 보너스(+1)")

    # 일봉 전용: MA20 기울기 > 2% 추가 +1
    ma20_col = None
    for c in ("ma20", "MA20"):
        if c in df.columns:
            ma20_col = c
            break
    if ma20_col is not None and len(df) >= 5:
        ma20_series = df[ma20_col].dropna()
        if len(ma20_series) >= 5:
            recent = ma20_series.iloc[-5:]
            base = _safe_float(recent.iloc[0])
            last = _safe_float(recent.iloc[-1])
            if base is not None and last is not None and base > 0:
                slope_pct = (last - base) / base * 100
                if slope_pct > 2.0:
                    buy_score += 1
                    buy_reasons.append(f"MA20 강한 상승 기울기(+{slope_pct:.2f}%)")

    # 일봉 전용: 가격↓ + 거래량↓ 다이버전스 (매도 소진) +1
    close = _safe_float(row.get("close") or row.get("종가"))
    prev_close_val = _safe_float(prev_row.get("close") or prev_row.get("종가"))

    vol_col = None
    for c in ("volume", "거래량", "Volume"):
        if c in df.columns:
            vol_col = c
            break

    if vol_col is not None and len(df) >= 2:
        cur_vol = _safe_float(df[vol_col].iloc[-1])
        prev_vol = _safe_float(df[vol_col].iloc[-2])

        if (close is not None and prev_close_val is not None
                and cur_vol is not None and prev_vol is not None):
            price_down = close < prev_close_val
            vol_down = cur_vol < prev_vol
            if price_down and vol_down:
                buy_score += 1
                buy_reasons.append("가격↓ 거래량↓ 매도 소진 다이버전스")

    total_score = buy_score + sell_score

    warnings: list[str] = []

    # 경고: 매수 신호인데 매도 조건도 있는 경우
    if buy_score > 0 and sell_score < 0:
        for r in sell_reasons:
            warnings.append(f"매도 조건 병존: {r}")

    all_reasons = buy_reasons + sell_reasons

    # 무효화: 당일 -3% 이상 급락 (일봉은 거래량 무효화 없음)
    if change_rate <= -3.0:
        if buy_score > 0 or total_score > 0:
            all_reasons.append(f"당일 급락({change_rate:.1f}%) → 매수 신호 무효")
            buy_score = 0
            total_score = min(total_score, 0)

    # 일봉 STRONG 기준: |score| >= 5
    signal_type, strength = _determine_strength(total_score, strong_threshold=5)

    return SignalResult(
        signal_type=signal_type,
        strength=strength,
        score=total_score,
        reasons=all_reasons,
        warnings=warnings,
        rsi=rsi_val,
        macd_cross=macd_cross,
        vol_ratio=vol_ratio,
    )
