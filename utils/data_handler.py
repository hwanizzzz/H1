import pandas as pd
import numpy as np
from collections import deque
from datetime import datetime, timedelta
from typing import Optional
from utils.logger import setup_logger

logger = setup_logger("data_handler")


# 타임프레임 → 초 환산 (UTC 기준 봉 분할에 사용)
TIMEFRAME_SECONDS = {
    "1M":  60,
    "5M":  300,
    "15M": 900,
    "30M": 1800,
    "1H":  3600,
    "2H":  7200,
    "4H":  14400,
    "1D":  86400,
}


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

    timeframe: "1M", "5M", "15M", "30M", "1H", "2H", "4H", "1D" 중 선택
               분봉 모드에서는 update_tick()이 봉 경계에서 자동으로
               close_current_bar()를 호출합니다.
    """

    def __init__(self, symbol: str, max_bars: int = 500, timeframe: str = "1D"):
        self.symbol = symbol
        self.timeframe = timeframe.upper()
        if self.timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"지원하지 않는 timeframe: {timeframe}")
        self.bar_seconds = TIMEFRAME_SECONDS[self.timeframe]
        self.bars: deque[OHLCVBar] = deque(maxlen=max_bars)
        self._current_bar: Optional[OHLCVBar] = None
        self._current_bar_open_ts: Optional[datetime] = None

    def add_bar(self, bar: OHLCVBar):
        """완성된 봉 추가 (API로부터 과거 봉 수신 시)"""
        self.bars.append(bar)
        logger.debug(f"[{self.timeframe}] 봉 추가: {bar.timestamp} "
                     f"O={bar.open} H={bar.high} L={bar.low} C={bar.close}")

    def _bar_open_time(self, ts: datetime) -> datetime:
        """타임스탬프를 timeframe 경계로 정규화 (UTC 기준)."""
        if ts is None:
            ts = datetime.utcnow()
        # epoch 기준으로 bar_seconds 단위로 절단
        epoch = ts.timestamp()
        bucket = int(epoch // self.bar_seconds) * self.bar_seconds
        return datetime.utcfromtimestamp(bucket)

    def update_tick(self, price: float, volume: float = 0.0, timestamp=None):
        """실시간 틱 수신 시 현재 봉 업데이트.
        timeframe 경계를 넘으면 이전 봉을 자동 확정하고 새 봉 시작.
        """
        if timestamp is None:
            timestamp = datetime.utcnow()

        bucket_open = self._bar_open_time(timestamp)

        # 새 봉 시작 (또는 첫 틱)
        if self._current_bar is None or bucket_open != self._current_bar_open_ts:
            # 기존 봉이 있으면 확정 후 저장
            if self._current_bar is not None:
                self.bars.append(self._current_bar)
                logger.debug(f"[{self.timeframe}] 봉 자동확정: "
                             f"{self._current_bar.timestamp} C={self._current_bar.close}")
            self._current_bar = OHLCVBar(bucket_open, price, price, price, price, volume)
            self._current_bar_open_ts = bucket_open
        else:
            self._current_bar.high = max(self._current_bar.high, price)
            self._current_bar.low = min(self._current_bar.low, price)
            self._current_bar.close = price
            self._current_bar.volume += volume

    def close_current_bar(self):
        """현재 봉을 강제로 확정하고 저장 (보통은 update_tick이 자동 처리)."""
        if self._current_bar is not None:
            self.bars.append(self._current_bar)
            self._current_bar = None
            self._current_bar_open_ts = None

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
        """직전 period 봉의 최고가 (현재 봉 제외 - 표준 Turtle 컨벤션).
        오늘 close > donchian_high(N) → 신고가 돌파 신호.
        """
        if len(self.bars) < period + 1:
            return None
        return max(b.high for b in list(self.bars)[-period - 1:-1])

    def donchian_low(self, period: int) -> Optional[float]:
        """직전 period 봉의 최저가 (현재 봉 제외)."""
        if len(self.bars) < period + 1:
            return None
        return min(b.low for b in list(self.bars)[-period - 1:-1])

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

    def rsi(self, period: int = 14) -> Optional[float]:
        """RSI (Relative Strength Index) - Wilder's smoothing"""
        closes = self.get_closes()
        if len(closes) < period + 1:
            return None
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        avg_gain = float(np.mean(gains[:period]))
        avg_loss = float(np.mean(losses[:period]))

        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return float(100.0 - 100.0 / (1.0 + rs))

    def sma(self, period: int) -> Optional[float]:
        closes = self.get_closes()
        if len(closes) < period:
            return None
        return float(np.mean(closes[-period:]))

    def highest_close_since_entry(self, lookback: int) -> Optional[float]:
        """최근 lookback봉 동안의 최고 종가 (Chandelier Exit용)"""
        if len(self.bars) < lookback:
            return None
        return max(b.high for b in list(self.bars)[-lookback:])

    def lowest_close_since_entry(self, lookback: int) -> Optional[float]:
        if len(self.bars) < lookback:
            return None
        return min(b.low for b in list(self.bars)[-lookback:])

    def bollinger_bands(self, period: int = 20, num_stddev: float = 2.0):
        """Return (upper, middle, lower) Bollinger Bands."""
        closes = self.get_closes()
        if len(closes) < period:
            return None, None, None
        window = closes[-period:]
        mid = float(np.mean(window))
        std = float(np.std(window, ddof=0))
        return mid + num_stddev * std, mid, mid - num_stddev * std

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
