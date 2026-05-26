"""
평균회귀(Mean Reversion) 전략 — 볼린저밴드 + RSI 필터

로직:
  - 종가가 볼린저 하단밴드 이탈 + RSI < 진입 과매도선 → 롱 (반등 기대)
  - 종가가 볼린저 상단밴드 이탈 + RSI > 진입 과매수선 → 숏 (되돌림 기대)
  - 청산: 종가가 중심선(이동평균)으로 회귀하면 익절 EXIT
  - 손절: 진입가 ± atr_multiplier × ATR (밴드 바깥으로 더 이탈 시)

추세장에서는 손실이 누적될 수 있어, 돌파/이평 전략과 포트폴리오로 분산하는 용도.
"""

from strategy.base_strategy import BaseStrategy, TradeSignal, Signal
from utils.logger import setup_logger

logger = setup_logger("mean_reversion")


class MeanReversionStrategy(BaseStrategy):

    def __init__(self, bb_period: int = 20, bb_std: float = 2.0,
                 rsi_period: int = 14, rsi_oversold: float = 30.0,
                 rsi_overbought: float = 70.0,
                 atr_period: int = 14, atr_multiplier: float = 2.5):
        super().__init__("MeanReversion")
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier

        self._position: str = "NONE"
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0

    def get_min_bars_required(self) -> int:
        return max(self.bb_period, self.rsi_period, self.atr_period) + 5

    def on_bar(self, data_handler) -> TradeSignal:
        if data_handler.bar_count() < self.get_min_bars_required():
            return TradeSignal(Signal.NONE)

        bands = data_handler.bollinger(self.bb_period, self.bb_std)
        rsi = data_handler.rsi(self.rsi_period)
        atr = data_handler.atr(self.atr_period)
        close = data_handler.latest_close()

        if bands is None or rsi is None or atr is None or atr == 0:
            return TradeSignal(Signal.NONE)

        lower, mid, upper = bands

        # ── 신규 진입 ─────────────────────────────────────────────────
        if self._position == "NONE":
            if close <= lower and rsi < self.rsi_oversold:
                stop = close - self.atr_multiplier * atr
                self._position = "LONG"
                self._entry_price = close
                self._stop_loss = stop
                logger.info(f"롱 진입 | close={close:.5f} 하단={lower:.5f} "
                            f"RSI={rsi:.1f} 손절={stop:.5f}")
                return TradeSignal(Signal.LONG, close, stop,
                                   f"볼린저 하단이탈+RSI{rsi:.0f}", atr)

            if close >= upper and rsi > self.rsi_overbought:
                stop = close + self.atr_multiplier * atr
                self._position = "SHORT"
                self._entry_price = close
                self._stop_loss = stop
                logger.info(f"숏 진입 | close={close:.5f} 상단={upper:.5f} "
                            f"RSI={rsi:.1f} 손절={stop:.5f}")
                return TradeSignal(Signal.SHORT, close, stop,
                                   f"볼린저 상단이탈+RSI{rsi:.0f}", atr)

        # ── 롱 청산 ───────────────────────────────────────────────────
        elif self._position == "LONG":
            if close <= self._stop_loss:
                reason = f"손절 {self._stop_loss:.5f}"
                logger.info(f"롱 청산 | {reason}")
                self._reset_position()
                return TradeSignal(Signal.EXIT, close, 0.0, reason, atr)
            if close >= mid:
                reason = f"중심선({mid:.5f}) 회귀 익절"
                logger.info(f"롱 청산 | {reason}")
                self._reset_position()
                return TradeSignal(Signal.EXIT, close, 0.0, reason, atr)

        # ── 숏 청산 ───────────────────────────────────────────────────
        elif self._position == "SHORT":
            if close >= self._stop_loss:
                reason = f"손절 {self._stop_loss:.5f}"
                logger.info(f"숏 청산 | {reason}")
                self._reset_position()
                return TradeSignal(Signal.EXIT, close, 0.0, reason, atr)
            if close <= mid:
                reason = f"중심선({mid:.5f}) 회귀 익절"
                logger.info(f"숏 청산 | {reason}")
                self._reset_position()
                return TradeSignal(Signal.EXIT, close, 0.0, reason, atr)

        return TradeSignal(Signal.NONE, atr=atr)

    def _reset_position(self):
        self._position = "NONE"
        self._entry_price = 0.0
        self._stop_loss = 0.0

    def set_position(self, side: str, entry_price: float, stop_loss: float):
        self._position = side
        self._entry_price = entry_price
        self._stop_loss = stop_loss

    @property
    def current_position(self) -> str:
        return self._position
