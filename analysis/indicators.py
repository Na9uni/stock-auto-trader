"""기술적 분석 지표 계산 모듈 - RSI, MACD, 볼린저밴드, 이동평균, ADX, VWAP 등 26개 지표."""

import logging

import numpy as np
import pandas as pd


class TechnicalIndicators:
    def __init__(self):
        self.logger = logging.getLogger("stock_analysis")

    def get_all_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """모든 지표 계산하여 새 DataFrame 반환 (원본 불변)."""
        result = df.copy()
        result = self._add_moving_averages(result)
        result = self._add_rsi(result)
        result = self._add_macd(result)
        result = self._add_bollinger_bands(result)
        result = self._add_vol_ratio(result)
        result = self._add_atr(result)
        result = self._add_stochastic(result)
        result = self._add_obv(result)
        result = self._add_adx(result)
        result = self._add_vwap(result)
        result = self._add_support_resistance(result)
        result = self._add_pivot_points(result)

        added_cols = [c for c in result.columns if c not in df.columns]
        self.logger.info(f"모든 기술적 지표 계산 완료 (총 {len(added_cols)}개 컬럼)")
        return result

    def _add_moving_averages(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()
        for period in [5, 10, 20, 60]:
            result[f"ma{period}"] = result["close"].rolling(window=period).mean()
        return result

    def _add_rsi(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        result = df.copy()
        delta = result["close"].diff()

        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)

        avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        both_zero = (avg_gain == 0) & (avg_loss == 0)
        rsi[both_zero] = 50.0

        result["rsi"] = rsi
        return result

    def _add_macd(
        self,
        df: pd.DataFrame,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> pd.DataFrame:
        result = df.copy()
        ema_fast = result["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = result["close"].ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        macd_signal = macd.ewm(span=signal, adjust=False).mean()

        result["macd"] = macd
        result["macd_signal"] = macd_signal
        result["macd_hist"] = macd - macd_signal
        return result

    def _add_bollinger_bands(
        self, df: pd.DataFrame, period: int = 20, std_dev: float = 2.0
    ) -> pd.DataFrame:
        result = df.copy()
        middle = result["close"].rolling(window=period).mean()
        std = result["close"].rolling(window=period).std(ddof=1)

        result["bb_upper"] = middle + std_dev * std
        result["bb_middle"] = middle
        result["bb_lower"] = middle - std_dev * std
        return result

    def _add_vol_ratio(self, df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
        result = df.copy()
        avg_vol = result["volume"].rolling(window=period).mean()
        result["vol_ratio"] = result["volume"] / avg_vol
        return result

    def _add_atr(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        result = df.copy()
        prev_close = result["close"].shift(1)

        tr = pd.concat(
            [
                result["high"] - result["low"],
                (result["high"] - prev_close).abs(),
                (result["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        result["atr"] = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        return result

    def _add_stochastic(
        self,
        df: pd.DataFrame,
        k_period: int = 14,
        d_period: int = 3,
    ) -> pd.DataFrame:
        result = df.copy()
        low_min = result["low"].rolling(window=k_period).min()
        high_max = result["high"].rolling(window=k_period).max()

        denom = high_max - low_min
        stoch_k = np.where(denom == 0, 50.0, (result["close"] - low_min) / denom * 100)

        result["stoch_k"] = stoch_k
        result["stoch_d"] = pd.Series(stoch_k, index=result.index).rolling(window=d_period).mean()
        return result

    def _add_obv(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()
        direction = np.sign(result["close"].diff()).fillna(0)
        obv = (direction * result["volume"]).cumsum()
        result["obv"] = obv
        return result

    def _add_adx(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """ADX (Average Directional Index) 계산 — +DI, -DI, ADX 컬럼 추가."""
        result = df.copy()
        high = result["high"]
        low = result["low"]
        close = result["close"]

        prev_high = high.shift(1)
        prev_low = low.shift(1)
        prev_close = close.shift(1)

        # True Range
        tr = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        # +DM / -DM
        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        plus_dm = pd.Series(plus_dm, index=result.index)
        minus_dm = pd.Series(minus_dm, index=result.index)

        # Wilder smoothing (EWM alpha=1/period)
        alpha = 1 / period
        atr_smooth = tr.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
        plus_dm_smooth = plus_dm.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
        minus_dm_smooth = minus_dm.ewm(alpha=alpha, min_periods=period, adjust=False).mean()

        # +DI / -DI
        plus_di = np.where(atr_smooth == 0, 0.0, plus_dm_smooth / atr_smooth * 100)
        minus_di = np.where(atr_smooth == 0, 0.0, minus_dm_smooth / atr_smooth * 100)

        plus_di = pd.Series(plus_di, index=result.index)
        minus_di = pd.Series(minus_di, index=result.index)

        # DX → ADX
        di_sum = plus_di + minus_di
        dx = np.where(di_sum == 0, 0.0, (plus_di - minus_di).abs() / di_sum * 100)
        dx = pd.Series(dx, index=result.index)

        adx = dx.ewm(alpha=alpha, min_periods=period, adjust=False).mean()

        result["plus_di"] = plus_di
        result["minus_di"] = minus_di
        result["adx"] = adx
        return result

    def _add_vwap(self, df: pd.DataFrame) -> pd.DataFrame:
        """VWAP (Volume Weighted Average Price) 계산 — 일별 리셋."""
        result = df.copy()
        typical_price = (result["high"] + result["low"] + result["close"]) / 3
        tp_vol = typical_price * result["volume"]

        # datetime 컬럼이 있으면 일별 그룹으로 VWAP 리셋
        if "datetime" in result.columns:
            try:
                dates = pd.to_datetime(result["datetime"].astype(str).str[:8])
                groups = dates.ne(dates.shift()).cumsum()
                cum_tp_vol = tp_vol.groupby(groups).cumsum()
                cum_vol = result["volume"].groupby(groups).cumsum()
            except Exception:
                cum_tp_vol = tp_vol.cumsum()
                cum_vol = result["volume"].cumsum()
        else:
            cum_tp_vol = tp_vol.cumsum()
            cum_vol = result["volume"].cumsum()

        result["vwap"] = np.where(cum_vol == 0, np.nan, cum_tp_vol / cum_vol)
        return result

    def _add_support_resistance(self, df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
        """최근 N봉 기준 지지선(support)·저항선(resistance) 계산."""
        result = df.copy()
        result["support"] = result["low"].rolling(window=period).min()
        result["resistance"] = result["high"].rolling(window=period).max()
        return result

    def _add_pivot_points(self, df: pd.DataFrame) -> pd.DataFrame:
        """피봇 포인트 — 전일 H/L/C 기반 PP, R1, S1, R2, S2 계산."""
        result = df.copy()
        prev_high = result["high"].shift(1)
        prev_low = result["low"].shift(1)
        prev_close = result["close"].shift(1)

        pp = (prev_high + prev_low + prev_close) / 3
        result["pivot_pp"] = pp
        result["pivot_r1"] = 2 * pp - prev_low
        result["pivot_s1"] = 2 * pp - prev_high
        result["pivot_r2"] = pp + (prev_high - prev_low)
        result["pivot_s2"] = pp - (prev_high - prev_low)
        return result
