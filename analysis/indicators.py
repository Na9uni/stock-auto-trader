"""기술적 분석 지표 계산 모듈 - RSI, MACD, 볼린저밴드, 이동평균 등 19개 지표."""

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
