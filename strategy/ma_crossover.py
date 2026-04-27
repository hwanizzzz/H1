"""
EMA Crossover + ADX 필터 전략 (보조 전략)

로직:
  - 단기 EMA(9)가 장기 EMA(21)를 상향 돌파 + ADX > 25 → 롱
  - 단기 EMA(9)가 장기 EMA(21)를 하향 돌파 + ADX > 25 → 숏
  - 반대 방향 크로스 발생 시 청산
  - 손절: 진입가 ± 1.5 × ATR(14)

ADX 필터로 횡보장 진입 차단 → 승률 향상
"""

from strategy.base_strategy import BaseStrategy, TradeSignal, Signal
from utils.logger import setup_logger

logger = setup_logger("ma_crossover")


class MACrossoverStrategy(BaseStrategy):

    def __init__(self, fast_period: int = 9, slow_period: int = 21,
                 adx_period: int = 14, adx_threshold: float = 25.0,
                 atr_period: int = 14, atr_multiplier: float = 1.5):
        super().__init__("MACrossover")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier

        self._position: str = "NONE"
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._prev_fast_ema: float = 0.0
        self._prev_slow_ema: float = 0.0

    def get_min_bars_required(self) -> int:
        return self.slow_period * 2 + self.adx_period + 5

    def on_bar(self, data_handler) -> TradeSignal:
        if data_handler.bar_count() < self.get_min_bars_required():
            return TradeSignal(Signal.NONE)

        fast_ema = data_handler.ema(self.fast_period)
        slow_ema = data_handler.ema(self.slow_period)
        adx      = data_handler.adx(self.adx_period)
        atr      = data_handler.atr(self.atr_period)
        close    = data_handler.latest_close()

        if None in (fast_ema, slow_ema, adx, atr):
            return TradeSignal(Signal.NONE)

        prev_fast = self._prev_fast_ema
        prev_slow = self._prev_slow_ema

        # 이전값 저장
        self._prev_fast_ema = fast_ema
        self._prev_slow_ema = slow_ema

        if prev_fast == 0 or prev_slow == 0:
            return TradeSignal(Signal.NONE)

        golden_cross = prev_fast <= prev_slow and fast_ema > slow_ema
        dead_cross   = prev_fast >= prev_slow and fast_ema < slow_ema
        trend_strong = adx >= self.adx_threshold

        if self._position == "NONE":
            if golden_cross and trend_strong:
                stop = close - self.atr_multiplier * atr
                self._position = "LONG"
                self._entry_price = close
                self._stop_loss = stop
                logger.info(f"롱 진입 | EMA{self.fast_period}>{self.slow_period} "
                            f"ADX={adx:.1f} close={close:.5f} 손절={stop:.5f}")
                return TradeSignal(Signal.LONG, close, stop,
                                   f"골든크로스+ADX{adx:.1f}", atr)

            if dead_cross and trend_strong:
                stop = close + self.atr_multiplier * atr
                self._position = "SHORT"
                self._entry_price = close
                self._stop_loss = stop
                logger.info(f"숏 진입 | EMA{self.fast_period}<{self.slow_period} "
                            f"ADX={adx:.1f} close={close:.5f} 손절={stop:.5f}")
                return TradeSignal(Signal.SHORT, close, stop,
                                   f"데드크로스+ADX{adx:.1f}", atr)

        elif self._position == "LONG":
            trailing_stop = close - self.atr_multiplier * atr
            if trailing_stop > self._stop_loss:
                self._stop_loss = trailing_stop

            if close <= self._stop_loss or dead_cross:
                reason = "데드크로스 청산" if dead_cross else f"손절 {self._stop_loss:.5f}"
                logger.info(f"롱 청산 | {reason}")
                self._reset_position()
                return TradeSignal(Signal.EXIT, close, 0.0, reason, atr)

        elif self._position == "SHORT":
            trailing_stop = close + self.atr_multiplier * atr
            if trailing_stop < self._stop_loss:
                self._stop_loss = trailing_stop

            if close >= self._stop_loss or golden_cross:
                reason = "골든크로스 청산" if golden_cross else f"손절 {self._stop_loss:.5f}"
                logger.info(f"숏 청산 | {reason}")
                self._reset_position()
                return TradeSignal(Signal.EXIT, close, 0.0, reason, atr)

        return TradeSignal(Signal.NONE, atr=atr)

    def _reset_position(self):
        self._position = "NONE"
        self._entry_price = 0.0
        self._stop_loss = 0.0

    def set_position(self, side: str, entry_price: float, stop_loss: float):
        """재시작 시 기존 포지션 복원 / 주문 거절 시 상태 되돌림"""
        self._position = side
        self._entry_price = entry_price
        self._stop_loss = stop_loss

    @property
    def current_position(self) -> str:
        return self._position
