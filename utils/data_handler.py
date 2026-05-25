import pandas as pd
import numpy as np
from collections import deque
from typing import Optional
from utils.logger import setup_logger

logger = setup_logger("data_handler")


class OHLCVBar:
    """단일 OHLCV 봉"""
    __slots__ = ("timestamp", "open", "high", "low", "close", "volume")

    def __init__(self, timestamp, open_: float, high: float, low: float,
                 close: float, volume: float = 0.0):
        self.timestamp = timestamp
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


class DataHandler:
    """
    실시간 틱 데이터를 OHLCV 봉으로 집계하고
    전략에 필요한 지표를 계산합니다.
    """

    def __init__(self, symbol: str, max_bars: int = 500):
        self.symbol = symbol
        self.bars: deque[OHLCVBar] = deque(maxlen=max_bars)
        self._current_bar: Optional[OHLCVBar] = None

    def add_bar(self, bar: OHLCVBar):
        """완성된 봉 추가 (API로부터 일봉 데이터 수신 시)"""
        self.bars.append(bar)
        logger.debug(f"봉 추가: {bar.timestamp} O={bar.open} H={bar.high} L={bar.low} C={bar.close}")

    def update_tick(self, price: float, volume: float = 0.0, timestamp=None):
        """실시간 틱 수신 시 현재 봉 업데이트"""
        if self._current_bar is None:
            self._current_bar = OHLCVBar(timestamp, price, price, price, price, volume)
        else:
            self._current_bar.high = max(self._current_bar.high, price)
            self._current_bar.low = min(self._current_bar.low, price)
            self._current_bar.close = price
            self._current_bar.volume += volume

    def close_current_bar(self):
        """현재 봉을 확정하고 저장"""
        if self._current_bar is not None:
            self.bars.append(self._current_bar)
            self._current_bar = None

    def to_dataframe(self) -> pd.DataFrame:
        if not self.bars:
            return pd.DataFrame()
        data = [{
            "timestamp": b.timestamp,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        } for b in self.bars]
        df = pd.DataFrame(data).set_index("timestamp")
        return df

    def get_closes(self) -> np.ndarray:
        return np.array([b.close for b in self.bars])

    def get_highs(self) -> np.ndarray:
        return np.array([b.high for b in self.bars])

    def get_lows(self) -> np.ndarray:
        return np.array([b.low for b in self.bars])

    def bar_count(self) -> int:
        return len(self.bars)

    def latest_close(self) -> Optional[float]:
        return self.bars[-1].close if self.bars else None

    # ── 지표 계산 ──────────────────────────────────────────────

    def donchian_high(self, period: int, exclude_current: bool = True) -> Optional[float]:
        """직전 period 봉의 최고가 (진입 롱 기준선).

        exclude_current=True면 현재(가장 최근) 봉을 제외한 직전 N봉으로 계산합니다.
        돌파 판단은 '현재 종가 > 직전 N봉 고점'이어야 하므로 기본값을 True로 둡니다.
        현재 봉을 포함하면 고점 >= 종가가 되어 돌파가 절대 성립하지 않습니다.
        """
        bars = list(self.bars)
        end = len(bars) - 1 if exclude_current else len(bars)
        window = bars[end - period:end]
        if len(window) < period:
            return None
        return max(b.high for b in window)

    def donchian_low(self, period: int, exclude_current: bool = True) -> Optional[float]:
        """직전 period 봉의 최저가 (진입 숏 기준선)."""
        bars = list(self.bars)
        end = len(bars) - 1 if exclude_current else len(bars)
        window = bars[end - period:end]
        if len(window) < period:
            return None
        return min(b.low for b in window)

    def sma(self, period: int) -> Optional[float]:
        """단순이동평균 (추세 필터용)"""
        closes = self.get_closes()
        if len(closes) < period:
            return None
        return float(np.mean(closes[-period:]))

    def atr(self, period: int = 14) -> Optional[float]:
        """Average True Range"""
        bars = list(self.bars)
        if len(bars) < period + 1:
            return None
        trs = []
        for i in range(1, len(bars)):
            high = bars[i].high
            low = bars[i].low
            prev_close = bars[i - 1].close
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        return float(np.mean(trs[-period:]))

    def ema(self, period: int) -> Optional[float]:
        closes = self.get_closes()
        if len(closes) < period:
            return None
        k = 2.0 / (period + 1)
        ema_val = closes[-period]
        for c in closes[-period + 1:]:
            ema_val = c * k + ema_val * (1 - k)
        return float(ema_val)

    def adx(self, period: int = 14) -> Optional[float]:
        """ADX (Average Directional Index) - 추세 강도"""
        bars = list(self.bars)
        if len(bars) < period * 2:
            return None

        plus_dm_list, minus_dm_list, tr_list = [], [], []
        for i in range(1, len(bars)):
            up = bars[i].high - bars[i - 1].high
            down = bars[i - 1].low - bars[i].low
            plus_dm_list.append(up if up > down and up > 0 else 0.0)
            minus_dm_list.append(down if down > up and down > 0 else 0.0)
            h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
            tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))

        def smooth(arr, n):
            result = [sum(arr[:n])]
            for v in arr[n:]:
                result.append(result[-1] - result[-1] / n + v)
            return result

        tr_s = smooth(tr_list, period)
        pdm_s = smooth(plus_dm_list, period)
        mdm_s = smooth(minus_dm_list, period)

        dx_list = []
        for tr, pdm, mdm in zip(tr_s, pdm_s, mdm_s):
            if tr == 0:
                continue
            pdi = 100 * pdm / tr
            mdi = 100 * mdm / tr
            dx_list.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0)

        if len(dx_list) < period:
            return None
        return float(np.mean(dx_list[-period:]))
