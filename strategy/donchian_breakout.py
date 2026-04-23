"""
Donchian Channel Breakout 전략 (터틀 트레이딩 변형)

CME 호주달러 선물(6A) 최적화 파라미터:
  - 진입 채널: 20봉 고점/저점
  - 청산 채널: 10봉 고점/저점
  - 손절:     진입가 ± 2 × ATR(14)
  - 시간봉 or 일봉 권장

논리:
  - 20봉 신고가 돌파  → 롱 진입
  - 20봉 신저가 하락  → 숏 진입
  - 롱 포지션: 10봉 저점 이탈 또는 손절 → 청산
  - 숏 포지션: 10봉 고점 돌파 또는 손절 → 청산
"""

from strategy.base_strategy import BaseStrategy, TradeSignal, Signal
from utils.logger import setup_logger

logger = setup_logger("donchian_breakout")


class DonchianBreakoutStrategy(BaseStrategy):

    def __init__(self, entry_period: int = 20, exit_period: int = 10,
                 atr_period: int = 14, atr_multiplier: float = 2.0):
        super().__init__("DonchianBreakout")
        self.entry_period = entry_period
        self.exit_period = exit_period
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier

        self._position: str = "NONE"   # "NONE", "LONG", "SHORT"
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0

    def get_min_bars_required(self) -> int:
        return max(self.entry_period, self.atr_period) + 5

    def on_bar(self, data_handler) -> TradeSignal:
        if data_handler.bar_count() < self.get_min_bars_required():
            return TradeSignal(Signal.NONE)

        close = data_handler.latest_close()
        atr   = data_handler.atr(self.atr_period)

        if atr is None or atr == 0:
            return TradeSignal(Signal.NONE)

        # 진입 채널 (현재 봉 제외 - 직전 N봉 기준)
        entry_high = data_handler.donchian_high(self.entry_period)
        entry_low  = data_handler.donchian_low(self.entry_period)

        # 청산 채널
        exit_high = data_handler.donchian_high(self.exit_period)
        exit_low  = data_handler.donchian_low(self.exit_period)

        if None in (entry_high, entry_low, exit_high, exit_low):
            return TradeSignal(Signal.NONE)

        # ── 포지션 없음: 신규 진입 신호 체크 ─────────────────────────────

        if self._position == "NONE":
            if close > entry_high:
                stop = close - self.atr_multiplier * atr
                self._position = "LONG"
                self._entry_price = close
                self._stop_loss = stop
                logger.info(f"롱 진입 신호 | close={close:.5f} "
                            f"20일고점={entry_high:.5f} 손절={stop:.5f} ATR={atr:.5f}")
                return TradeSignal(Signal.LONG, close, stop,
                                   f"20봉 고점({entry_high:.5f}) 상향 돌파", atr)

            if close < entry_low:
                stop = close + self.atr_multiplier * atr
                self._position = "SHORT"
                self._entry_price = close
                self._stop_loss = stop
                logger.info(f"숏 진입 신호 | close={close:.5f} "
                            f"20일저점={entry_low:.5f} 손절={stop:.5f} ATR={atr:.5f}")
                return TradeSignal(Signal.SHORT, close, stop,
                                   f"20봉 저점({entry_low:.5f}) 하향 이탈", atr)

        # ── 롱 포지션: 청산 조건 ─────────────────────────────────────────

        elif self._position == "LONG":
            # 추적 손절 업데이트 (수익 구간에서 손절 상향)
            trailing_stop = close - self.atr_multiplier * atr
            if trailing_stop > self._stop_loss:
                self._stop_loss = trailing_stop

            if close <= self._stop_loss:
                reason = f"손절 ({self._stop_loss:.5f}) 또는 10봉 저점 이탈"
                logger.info(f"롱 청산 신호 | {reason}")
                self._reset_position()
                return TradeSignal(Signal.EXIT, close, 0.0, reason, atr)

            if close < exit_low:
                reason = f"10봉 저점({exit_low:.5f}) 이탈"
                logger.info(f"롱 청산 신호 | {reason}")
                self._reset_position()
                return TradeSignal(Signal.EXIT, close, 0.0, reason, atr)

        # ── 숏 포지션: 청산 조건 ─────────────────────────────────────────

        elif self._position == "SHORT":
            trailing_stop = close + self.atr_multiplier * atr
            if trailing_stop < self._stop_loss:
                self._stop_loss = trailing_stop

            if close >= self._stop_loss:
                reason = f"손절 ({self._stop_loss:.5f}) 또는 10봉 고점 돌파"
                logger.info(f"숏 청산 신호 | {reason}")
                self._reset_position()
                return TradeSignal(Signal.EXIT, close, 0.0, reason, atr)

            if close > exit_high:
                reason = f"10봉 고점({exit_high:.5f}) 돌파"
                logger.info(f"숏 청산 신호 | {reason}")
                self._reset_position()
                return TradeSignal(Signal.EXIT, close, 0.0, reason, atr)

        return TradeSignal(Signal.NONE, atr=atr)

    def _reset_position(self):
        self._position = "NONE"
        self._entry_price = 0.0
        self._stop_loss = 0.0

    def set_position(self, side: str, entry_price: float, stop_loss: float):
        """재시작 시 기존 포지션 복원"""
        self._position = side
        self._entry_price = entry_price
        self._stop_loss = stop_loss

    @property
    def current_position(self) -> str:
        return self._position
