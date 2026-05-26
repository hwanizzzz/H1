"""
실시간 틱 → OHLCV 봉 집계 및 지표 계산 모듈.

핵심:
  - update_tick() 이 들어오는 틱을 타임프레임 버킷 기준으로 자동 집계합니다.
  - 틱의 버킷이 바뀌면 직전 봉을 확정(append)하고 on_bar_closed 콜백을 호출합니다.
    (이전 골격의 버그 — close_current_bar 미호출로 봉이 늘지 않던 문제 해결)
  - add_bar() 는 API 과거 봉 적재용입니다.
"""

import numpy as np
import pandas as pd
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Callable, List
from utils.logger import setup_logger

logger = setup_logger("data_handler")


# ── 타임프레임 파싱 ───────────────────────────────────────────────────────────

def parse_timeframe(tf: str) -> Optional[int]:
    """
    타임프레임 문자열을 초 단위로 변환.
    일봉("1D")은 None 을 반환(날짜 버킷 사용).
      "1M"=60, "5M"=300, "30M"=1800, "1H"=3600, "4H"=14400, "1D"=None
    """
    tf = tf.strip().upper()
    if tf in ("1D", "D", "DAY", "1DAY"):
        return None
    units = {"M": 60, "H": 3600}
    num = "".join(c for c in tf if c.isdigit())
    unit = "".join(c for c in tf if c.isalpha())
    if not num or unit not in units:
        logger.warning(f"알 수 없는 타임프레임 '{tf}' → 일봉으로 처리")
        return None
    return int(num) * units[unit]


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

    def __repr__(self):
        return (f"OHLCVBar({self.timestamp} O={self.open} H={self.high} "
                f"L={self.low} C={self.close} V={self.volume})")


class DataHandler:
    """
    실시간 틱 데이터를 OHLCV 봉으로 집계하고 전략 지표를 계산합니다.

    Args:
        symbol:    종목코드
        timeframe: "1D", "4H", "1H", "30M", "5M", "1M"
        max_bars:  보관할 최대 봉 수 (deque maxlen)
    """

    def __init__(self, symbol: str, timeframe: str = "1D", max_bars: int = 500):
        self.symbol = symbol
        self.timeframe = timeframe
        self._tf_seconds = parse_timeframe(timeframe)
        self.bars: "deque[OHLCVBar]" = deque(maxlen=max_bars)

        self._current_bar: Optional[OHLCVBar] = None
        self._current_bucket = None

        # 봉 확정 시 호출되는 콜백: on_bar_closed(closed_bar)
        self.on_bar_closed: Optional[Callable[[OHLCVBar], None]] = None

    # ── 봉 적재 / 집계 ────────────────────────────────────────────────────────

    def add_bar(self, bar: OHLCVBar):
        """완성된 봉 추가 (API 과거 봉 적재 시)"""
        self.bars.append(bar)

    def _bucket(self, ts: datetime):
        """타임프레임 버킷 키 산출"""
        if self._tf_seconds is None:
            return ts.date()
        return int(ts.timestamp() // self._tf_seconds)

    def _bucket_open_time(self, ts: datetime) -> datetime:
        """버킷 시작 시각 (봉 timestamp)"""
        if self._tf_seconds is None:
            return datetime(ts.year, ts.month, ts.day)
        epoch = (int(ts.timestamp() // self._tf_seconds)) * self._tf_seconds
        return datetime.fromtimestamp(epoch)

    def update_tick(self, price: float, volume: float = 0.0,
                    timestamp: Optional[datetime] = None) -> bool:
        """
        실시간 틱 수신. 봉 버킷이 바뀌면 직전 봉을 확정한다.

        Returns:
            True  - 새 봉으로 전환되며 직전 봉이 확정됨
            False - 현재 봉 갱신만 됨
        """
        ts = timestamp or datetime.now()
        bucket = self._bucket(ts)

        if self._current_bar is None:
            self._current_bucket = bucket
            self._current_bar = OHLCVBar(self._bucket_open_time(ts),
                                         price, price, price, price, volume)
            return False

        if bucket != self._current_bucket:
            closed = self._current_bar
            self.bars.append(closed)
            self._current_bucket = bucket
            self._current_bar = OHLCVBar(self._bucket_open_time(ts),
                                         price, price, price, price, volume)
            logger.debug(f"봉 확정: {closed}")
            if self.on_bar_closed:
                try:
                    self.on_bar_closed(closed)
                except Exception as e:
                    logger.error(f"on_bar_closed 콜백 오류: {e}", exc_info=True)
            return True

        # 같은 버킷 → 현재 봉 갱신
        self._current_bar.high = max(self._current_bar.high, price)
        self._current_bar.low = min(self._current_bar.low, price)
        self._current_bar.close = price
        self._current_bar.volume += volume
        return False

    def close_current_bar(self) -> Optional[OHLCVBar]:
        """현재 봉을 강제 확정 (세션 종료/타이머 기반 마감용)."""
        if self._current_bar is None:
            return None
        closed = self._current_bar
        self.bars.append(closed)
        self._current_bar = None
        self._current_bucket = None
        if self.on_bar_closed:
            try:
                self.on_bar_closed(closed)
            except Exception as e:
                logger.error(f"on_bar_closed 콜백 오류: {e}", exc_info=True)
        return closed

    # ── 조회 헬퍼 ─────────────────────────────────────────────────────────────

    def to_dataframe(self) -> pd.DataFrame:
        if not self.bars:
            return pd.DataFrame()
        data = [{
            "timestamp": b.timestamp, "open": b.open, "high": b.high,
            "low": b.low, "close": b.close, "volume": b.volume,
        } for b in self.bars]
        return pd.DataFrame(data).set_index("timestamp")

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

    # ── 지표 계산 ──────────────────────────────────────────────────────────────

    def donchian_high(self, period: int, offset: int = 0) -> Optional[float]:
        """
        최근 period 봉의 최고가. offset=1 이면 가장 최근 봉을 제외한
        직전 period 봉 기준(돌파 판정용 — 현재 봉이 채널에 포함돼 돌파가
        불가능해지는 문제 방지).
        """
        bars = list(self.bars)
        end = len(bars) - offset
        start = end - period
        if start < 0 or end <= start:
            return None
        return max(b.high for b in bars[start:end])

    def donchian_low(self, period: int, offset: int = 0) -> Optional[float]:
        bars = list(self.bars)
        end = len(bars) - offset
        start = end - period
        if start < 0 or end <= start:
            return None
        return min(b.low for b in bars[start:end])

    def atr(self, period: int = 14) -> Optional[float]:
        bars = list(self.bars)
        if len(bars) < period + 1:
            return None
        trs = []
        for i in range(1, len(bars)):
            high, low, prev_close = bars[i].high, bars[i].low, bars[i - 1].close
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
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

    def sma(self, period: int) -> Optional[float]:
        closes = self.get_closes()
        if len(closes) < period:
            return None
        return float(np.mean(closes[-period:]))

    def stddev(self, period: int) -> Optional[float]:
        closes = self.get_closes()
        if len(closes) < period:
            return None
        return float(np.std(closes[-period:], ddof=0))

    def bollinger(self, period: int = 20, num_std: float = 2.0):
        """반환: (하단밴드, 중심선, 상단밴드) 또는 None"""
        mid = self.sma(period)
        sd = self.stddev(period)
        if mid is None or sd is None:
            return None
        return (mid - num_std * sd, mid, mid + num_std * sd)

    def rsi(self, period: int = 14) -> Optional[float]:
        closes = self.get_closes()
        if len(closes) < period + 1:
            return None
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = float(np.mean(gains[-period:]))
        avg_loss = float(np.mean(losses[-period:]))
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def adx(self, period: int = 14) -> Optional[float]:
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
            denom = pdi + mdi
            dx_list.append(100 * abs(pdi - mdi) / denom if denom else 0.0)

        if len(dx_list) < period:
            return None
        return float(np.mean(dx_list[-period:]))
