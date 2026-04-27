import pandas as pd
import numpy as np
from collections import deque
from datetime import datetime, timedelta
from typing import Optional
from utils.logger import setup_logger

logger = setup_logger("data_handler")


# 지원 봉 단위 → timedelta
_TIMEFRAME_MAP = {
    "1M":  timedelta(minutes=1),
    "5M":  timedelta(minutes=5),
    "15M": timedelta(minutes=15),
    "30M": timedelta(minutes=30),
    "1H":  timedelta(hours=1),
    "4H":  timedelta(hours=4),
    "1D":  timedelta(days=1),
}


def _bar_open_time(ts: datetime, tf: timedelta) -> datetime:
    """타임스탬프를 봉 시작 시각으로 정규화."""
    if tf >= timedelta(days=1):
        return datetime(ts.year, ts.month, ts.day)
    epoch = datetime(1970, 1, 1)
    secs = int((ts - epoch).total_seconds())
    period = int(tf.total_seconds())
    aligned = secs - (secs % period)
    return epoch + timedelta(seconds=aligned)


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

    def __init__(self, symbol: str, max_bars: int = 500, timeframe: str = "1D"):
        self.symbol = symbol
        self.bars: deque[OHLCVBar] = deque(maxlen=max_bars)
        self._current_bar: Optional[OHLCVBar] = None
        tf = _TIMEFRAME_MAP.get(timeframe.upper())
        if tf is None:
            raise ValueError(f"지원하지 않는 timeframe: {timeframe}")
        self.timeframe = timeframe.upper()
        self._tf_delta: timedelta = tf

    def add_bar(self, bar: OHLCVBar):
        """완성된 봉 추가 (API로부터 일봉 데이터 수신 시)"""
        self.bars.append(bar)
        logger.debug(f"봉 추가: {bar.timestamp} O={bar.open} H={bar.high} L={bar.low} C={bar.close}")

    def update_tick(self, price: float, volume: float = 0.0, timestamp: Optional[datetime] = None):
        """
        실시간 틱 수신 시 현재 봉 업데이트.
        타임스탬프가 현재 봉의 timeframe 경계를 넘으면 현재 봉을 마감하고 새 봉을 시작한다.
        """
        if timestamp is None:
            timestamp = datetime.now()

        bar_open = _bar_open_time(timestamp, self._tf_delta)

        if self._current_bar is None:
            self._current_bar = OHLCVBar(bar_open, price, price, price, price, volume)
            return

        # 새로운 봉 구간으로 진입한 경우 → 이전 봉 확정
        if bar_open > self._current_bar.timestamp:
            self.bars.append(self._current_bar)
            logger.debug(
                f"봉 마감: {self._current_bar.timestamp} "
                f"O={self._current_bar.open} H={self._current_bar.high} "
                f"L={self._current_bar.low} C={self._current_bar.close}"
            )
            self._current_bar = OHLCVBar(bar_open, price, price, price, price, volume)
            return

        self._current_bar.high = max(self._current_bar.high, price)
        self._current_bar.low = min(self._current_bar.low, price)
        self._current_bar.close = price
        self._current_bar.volume += volume

    def close_current_bar(self):
        """현재 봉을 강제 확정 (예: 종료 시)"""
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

    def donchian_high(self, period: int) -> Optional[float]:
        """최근 period 봉의 최고가 (진입 롱 기준선)"""
        if len(self.bars) < period:
            return None
        return max(b.high for b in list(self.bars)[-period:])

    def donchian_low(self, period: int) -> Optional[float]:
        """최근 period 봉의 최저가 (진입 숏 기준선)"""
        if len(self.bars) < period:
            return None
        return min(b.low for b in list(self.bars)[-period:])

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
