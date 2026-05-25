"""
평균회귀(Mean Reversion) 전략 — 볼린저밴드 기반.

돌파전략(추세추종)과 정반대 성격:
  - 가격이 평균에서 너무 벗어나면(과매도/과매수) "되돌아온다"에 베팅
  - 횡보장에서 강하고, 강한 추세장에서는 약함

로직 (period=20, k=2.0 기본):
  중심선 = 종가 20봉 단순이동평균(SMA)
  상단/하단 = 중심선 ± k × 표준편차
  - 종가 < 하단 → 롱 진입,  종가 ≥ 중심선이면 청산(평균 복귀 = 익절)
  - 종가 > 상단 → 숏 진입,  종가 ≤ 중심선이면 청산
  - 손절: 진입가 ± atr_mult × ATR  (되돌림 실패 시 손실 제한)

trend_filter_period>0이면 장기추세와 같은 방향만 진입(눌림목 매수/되돌림 매도).
"""

import numpy as np

from strategy.base_strategy import BaseStrategy, TradeSignal, Signal
from utils.logger import setup_logger

logger = setup_logger("mean_reversion")


class MeanReversionStrategy(BaseStrategy):

    def __init__(self, period: int = 20, num_std: float = 2.0,
                 atr_period: int = 14, atr_multiplier: float = 3.0,
                 trend_filter_period: int = 0):
        super().__init__("MeanReversion")
        self.period = period
        self.num_std = num_std
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.trend_filter_period = trend_filter_period

        self._position = "NONE"
        self._entry_price = 0.0
        self._stop_loss = 0.0

    def get_min_bars_required(self) -> int:
        base = max(self.period, self.atr_period) + 5
        if self.trend_filter_period > 0:
            return max(base, self.trend_filter_period + 5)
        return base

    def on_bar(self, data_handler) -> TradeSignal:
        if data_handler.bar_count() < self.get_min_bars_required():
            return TradeSignal(Signal.NONE)

        closes = data_handler.get_closes()
        atr = data_handler.atr(self.atr_period)
        close = data_handler.latest_close()
        if atr is None or atr == 0:
            return TradeSignal(Signal.NONE)

        window = closes[-self.period:]
        mid = float(np.mean(window))
        std = float(np.std(window))
        if std == 0:
            return TradeSignal(Signal.NONE, atr=atr)
        upper = mid + self.num_std * std
        lower = mid - self.num_std * std

        trend = None
        if self.trend_filter_period > 0:
            trend = data_handler.sma(self.trend_filter_period)
            if trend is None:
                return TradeSignal(Signal.NONE, atr=atr)
        long_ok = trend is None or close > trend
        short_ok = trend is None or close < trend

        if self._position == "NONE":
            if close < lower and long_ok:
                stop = close - self.atr_multiplier * atr
                self._position, self._entry_price, self._stop_loss = "LONG", close, stop
                return TradeSignal(Signal.LONG, close, stop,
                                   f"하단밴드({lower:.5f}) 이탈 과매도", atr)
            if close > upper and short_ok:
                stop = close + self.atr_multiplier * atr
                self._position, self._entry_price, self._stop_loss = "SHORT", close, stop
                return TradeSignal(Signal.SHORT, close, stop,
                                   f"상단밴드({upper:.5f}) 돌파 과매수", atr)

        elif self._position == "LONG":
            if close <= self._stop_loss:
                self._reset_position()
                return TradeSignal(Signal.EXIT, close, 0.0, f"손절 {self._stop_loss:.5f}", atr)
            if close >= mid:
                self._reset_position()
                return TradeSignal(Signal.EXIT, close, 0.0, f"평균({mid:.5f}) 복귀 익절", atr)

        elif self._position == "SHORT":
            if close >= self._stop_loss:
                self._reset_position()
                return TradeSignal(Signal.EXIT, close, 0.0, f"손절 {self._stop_loss:.5f}", atr)
            if close <= mid:
                self._reset_position()
                return TradeSignal(Signal.EXIT, close, 0.0, f"평균({mid:.5f}) 복귀 익절", atr)

        return TradeSignal(Signal.NONE, atr=atr)

    def _reset_position(self):
        self._position = "NONE"
        self._entry_price = 0.0
        self._stop_loss = 0.0

    def reset_position(self):
        self._reset_position()

    @property
    def current_position(self) -> str:
        return self._position
