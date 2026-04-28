"""
Multi-Timeframe Aggressive Strategy — CME 호주달러 선물(6A) Tier 3 어그레시브

설계 의도:
  3,000만원(~$21,500) 자본으로 6A를 운용할 때 일봉만으로는
  (a) 1% 리스크 사이즈가 0계약이 되어 거래가 안 되고,
  (b) 거래 빈도가 월 1~2회로 너무 낮아 통계적 안정성이 떨어집니다.

  4시간봉을 메인 추세 필터로 + 1시간봉을 진입 트리거로 사용해
  거래 빈도를 월 15~25회로 끌어올리고, 추세가 살아있는 동안
  포지션을 4단까지 추가(피라미딩)해 수익률을 극대화합니다.

═══════════════════════════════════════════════════════════════════════════
타임프레임 역할 분담
─────────────────────────────────────────────────────────────────────────
  4H (메인 추세):
    - EMA50, EMA200 정렬로 추세 방향 결정
    - ADX(14) ≥ 18 추세 강도 필터
    - ATR(14) 변동성 측정 (피라미딩 트리거 거리 기준)

  1H (진입 트리거):
    - Donchian 20봉 돌파로 모멘텀 진입
    - RSI(14) 과열 회피 (롱 < 75 / 숏 > 25)

═══════════════════════════════════════════════════════════════════════════
진입 로직 (4H 추세 + 1H 트리거)
─────────────────────────────────────────────────────────────────────────
  롱 진입:
    [4H] close > EMA50 > EMA200, ADX ≥ 18
    [1H] close > Donchian-20 신고가, RSI < 75
    → 1단 진입, 손절 = 진입가 - 1.5 × ATR(4H)

  숏 진입: 위 조건의 미러.

═══════════════════════════════════════════════════════════════════════════
피라미딩 (Turtle 변형, 최대 4단)
─────────────────────────────────────────────────────────────────────────
  - 진입 후 +0.5 × ATR(4H) 우호 이동마다 1단 추가
  - 최대 4단까지 (4번까지만 추가)
  - 모든 단위는 동일한 추적 손절 공유 (Chandelier 3 × ATR(4H))
  - 단위 수에 따라 사이즈 감소 (1.0, 0.75, 0.5, 0.5)

═══════════════════════════════════════════════════════════════════════════
청산 로직
─────────────────────────────────────────────────────────────────────────
  - 초기 손절: 진입가 ± 1.5 × ATR(4H) (= 1R)
  - +3R 도달 시 보유의 50% 부분 익절
  - 잔여 50%는 Chandelier Exit (최고가 - 3 × ATR(4H)) 추적
  - 4H EMA50 반대 이탈 → 즉시 전량 청산 (추세 반전)
"""

from typing import Optional, List
from strategy.base_strategy import BaseStrategy, TradeSignal, Signal
from utils.logger import setup_logger

logger = setup_logger("multi_tf_aggressive")


class MultiTFAggressiveStrategy(BaseStrategy):

    def __init__(
        self,
        # 4H 추세 필터
        trend_ema_fast: int = 50,
        trend_ema_slow: int = 200,
        trend_adx_period: int = 14,
        trend_adx_threshold: float = 18.0,
        atr_period: int = 14,
        # 1H 진입 트리거
        entry_donchian_period: int = 20,
        entry_rsi_period: int = 14,
        rsi_long_max: float = 75.0,
        rsi_short_min: float = 25.0,
        # 손절·익절
        initial_stop_atr: float = 1.5,
        chandelier_atr: float = 3.0,
        partial_take_r: float = 3.0,
        partial_close_pct: float = 0.5,
        # 피라미딩
        pyramid_enabled: bool = True,
        pyramid_max_units: int = 4,
        pyramid_step_atr: float = 0.5,
    ):
        super().__init__("MultiTFAggressive")
        self.trend_ema_fast = trend_ema_fast
        self.trend_ema_slow = trend_ema_slow
        self.trend_adx_period = trend_adx_period
        self.trend_adx_threshold = trend_adx_threshold
        self.atr_period = atr_period
        self.entry_donchian_period = entry_donchian_period
        self.entry_rsi_period = entry_rsi_period
        self.rsi_long_max = rsi_long_max
        self.rsi_short_min = rsi_short_min
        self.initial_stop_atr = initial_stop_atr
        self.chandelier_atr = chandelier_atr
        self.partial_take_r = partial_take_r
        self.partial_close_pct = partial_close_pct
        self.pyramid_enabled = pyramid_enabled
        self.pyramid_max_units = pyramid_max_units
        self.pyramid_step_atr = pyramid_step_atr

        # ── 포지션 상태 ───────────────────────────────────────────────
        self._position: str = "NONE"
        self._entry_price: float = 0.0
        self._initial_stop: float = 0.0
        self._stop_loss: float = 0.0
        self._risk_unit: float = 0.0           # 1R = |entry - initial_stop|
        self._units_held: int = 0              # 0 ~ pyramid_max_units
        self._next_pyramid_price: float = 0.0  # 다음 피라미딩 트리거 가격
        self._highest_since_entry: float = 0.0
        self._lowest_since_entry: float = 0.0
        self._partial_taken: bool = False
        self._bars_in_trade: int = 0

    # ── 필수 메서드 ──────────────────────────────────────────────────────

    def get_required_timeframes(self) -> List[str]:
        return ["4H", "1H"]

    def get_min_bars_required(self) -> int:
        # 4H 기준 최소 봉 수 (EMA200 충족)
        return max(self.trend_ema_slow, self.atr_period * 2) + 5

    def on_bar(self, data) -> TradeSignal:
        # 멀티TF data dict에서 두 핸들러 추출
        if not isinstance(data, dict):
            logger.warning("MultiTFAggressive 전략은 dict 형태의 멀티TF 데이터를 요구합니다")
            return TradeSignal(Signal.NONE)

        d4h = data.get("4H")
        d1h = data.get("1H")

        if d4h is None or d1h is None:
            return TradeSignal(Signal.NONE)

        if d4h.bar_count() < self.get_min_bars_required():
            return TradeSignal(Signal.NONE)
        if d1h.bar_count() < self.entry_donchian_period + self.entry_rsi_period + 5:
            return TradeSignal(Signal.NONE)

        # ── 4H 추세 지표 ───────────────────────────────────────────────
        ema_fast = d4h.ema(self.trend_ema_fast)
        ema_slow = d4h.ema(self.trend_ema_slow)
        adx_4h   = d4h.adx(self.trend_adx_period)
        atr_4h   = d4h.atr(self.atr_period)
        close_4h = d4h.latest_close()

        if None in (ema_fast, ema_slow, adx_4h, atr_4h, close_4h) or atr_4h == 0:
            return TradeSignal(Signal.NONE)

        # ── 1H 진입 지표 ───────────────────────────────────────────────
        close_1h = d1h.latest_close()
        donchian_high_1h = d1h.donchian_high(self.entry_donchian_period)
        donchian_low_1h  = d1h.donchian_low(self.entry_donchian_period)
        rsi_1h = d1h.rsi(self.entry_rsi_period)

        if None in (close_1h, donchian_high_1h, donchian_low_1h, rsi_1h):
            return TradeSignal(Signal.NONE)

        # ── 보유 포지션 관리 우선 ──────────────────────────────────────
        if self._position != "NONE":
            return self._manage_open_position(close_1h, atr_4h, ema_fast)

        # ── 신규 진입 시그널 ───────────────────────────────────────────

        trend_up = close_4h > ema_fast > ema_slow
        trend_dn = close_4h < ema_fast < ema_slow
        strong   = adx_4h >= self.trend_adx_threshold

        if not strong:
            return TradeSignal(Signal.NONE, atr=atr_4h)

        # 롱 진입
        if trend_up and close_1h > donchian_high_1h and rsi_1h < self.rsi_long_max:
            stop = close_1h - self.initial_stop_atr * atr_4h
            self._open("LONG", close_1h, stop, atr_4h)
            reason = (f"[4H] EMA50/200 정배열 ADX={adx_4h:.1f} | "
                      f"[1H] 신고가 돌파 RSI={rsi_1h:.1f}")
            logger.info(f"롱 진입(1단) | close={close_1h:.5f} 손절={stop:.5f} | {reason}")
            return TradeSignal(
                Signal.LONG, close_1h, stop, reason, atr_4h,
                take_profit_targets=[self.partial_take_r],
            )

        # 숏 진입
        if trend_dn and close_1h < donchian_low_1h and rsi_1h > self.rsi_short_min:
            stop = close_1h + self.initial_stop_atr * atr_4h
            self._open("SHORT", close_1h, stop, atr_4h)
            reason = (f"[4H] EMA50/200 역배열 ADX={adx_4h:.1f} | "
                      f"[1H] 신저가 이탈 RSI={rsi_1h:.1f}")
            logger.info(f"숏 진입(1단) | close={close_1h:.5f} 손절={stop:.5f} | {reason}")
            return TradeSignal(
                Signal.SHORT, close_1h, stop, reason, atr_4h,
                take_profit_targets=[self.partial_take_r],
            )

        return TradeSignal(Signal.NONE, atr=atr_4h)

    # ── 포지션 관리 (피라미딩 + 부분 익절 + 추적 손절) ──────────────────

    def _manage_open_position(self, close: float, atr_4h: float,
                              ema_fast_4h: float) -> TradeSignal:
        self._bars_in_trade += 1

        if self._position == "LONG":
            self._highest_since_entry = max(self._highest_since_entry, close)
            r_multiple = (close - self._entry_price) / self._risk_unit if self._risk_unit else 0.0

            # 1) 피라미딩 트리거 체크
            if (self.pyramid_enabled and
                self._units_held < self.pyramid_max_units and
                close >= self._next_pyramid_price):
                add_qty = 1
                self._units_held += 1
                self._next_pyramid_price = close + self.pyramid_step_atr * atr_4h
                # 피라미드 추가 시 손절을 새 진입가 기준으로 끌어올림
                new_stop = close - self.initial_stop_atr * atr_4h
                self._stop_loss = max(self._stop_loss, new_stop)
                reason = f"피라미드 {self._units_held}/{self.pyramid_max_units}단 추가"
                logger.info(f"롱 추가진입 | close={close:.5f} 손절={self._stop_loss:.5f} | {reason}")
                return TradeSignal(
                    Signal.SCALE_IN, close, self._stop_loss, reason, atr_4h,
                    scale_qty=add_qty,
                )

            # 2) 부분 익절 (+3R 도달)
            if not self._partial_taken and r_multiple >= self.partial_take_r:
                self._partial_taken = True
                self._stop_loss = max(self._stop_loss, self._entry_price)
                logger.info(f"+{self.partial_take_r:.0f}R 부분익절 ({self.partial_close_pct*100:.0f}%) "
                            f"+ BE 이동: {self._stop_loss:.5f}")
                return TradeSignal(
                    Signal.PARTIAL_TP, close, self._stop_loss,
                    f"+{self.partial_take_r:.0f}R 도달 부분 익절", atr_4h,
                    partial_close_pct=self.partial_close_pct,
                )

            # 3) Chandelier 추적 손절 (부분익절 후에만)
            if self._partial_taken:
                chandelier = self._highest_since_entry - self.chandelier_atr * atr_4h
                if chandelier > self._stop_loss:
                    self._stop_loss = chandelier

            # 4) 청산 트리거
            if close <= self._stop_loss:
                return self._close(close, atr_4h, f"손절/추적손절 {self._stop_loss:.5f}")
            if close < ema_fast_4h:
                return self._close(close, atr_4h, "4H EMA50 하향 이탈 (추세반전)")

        elif self._position == "SHORT":
            self._lowest_since_entry = min(self._lowest_since_entry, close)
            r_multiple = (self._entry_price - close) / self._risk_unit if self._risk_unit else 0.0

            if (self.pyramid_enabled and
                self._units_held < self.pyramid_max_units and
                close <= self._next_pyramid_price):
                add_qty = 1
                self._units_held += 1
                self._next_pyramid_price = close - self.pyramid_step_atr * atr_4h
                new_stop = close + self.initial_stop_atr * atr_4h
                self._stop_loss = min(self._stop_loss, new_stop)
                reason = f"피라미드 {self._units_held}/{self.pyramid_max_units}단 추가"
                logger.info(f"숏 추가진입 | close={close:.5f} 손절={self._stop_loss:.5f} | {reason}")
                return TradeSignal(
                    Signal.SCALE_IN, close, self._stop_loss, reason, atr_4h,
                    scale_qty=add_qty,
                )

            if not self._partial_taken and r_multiple >= self.partial_take_r:
                self._partial_taken = True
                self._stop_loss = min(self._stop_loss, self._entry_price)
                logger.info(f"+{self.partial_take_r:.0f}R 부분익절 ({self.partial_close_pct*100:.0f}%) "
                            f"+ BE 이동: {self._stop_loss:.5f}")
                return TradeSignal(
                    Signal.PARTIAL_TP, close, self._stop_loss,
                    f"+{self.partial_take_r:.0f}R 도달 부분 익절", atr_4h,
                    partial_close_pct=self.partial_close_pct,
                )

            if self._partial_taken:
                chandelier = self._lowest_since_entry + self.chandelier_atr * atr_4h
                if chandelier < self._stop_loss:
                    self._stop_loss = chandelier

            if close >= self._stop_loss:
                return self._close(close, atr_4h, f"손절/추적손절 {self._stop_loss:.5f}")
            if close > ema_fast_4h:
                return self._close(close, atr_4h, "4H EMA50 상향 이탈 (추세반전)")

        return TradeSignal(Signal.NONE, atr=atr_4h)

    # ── 내부 유틸 ────────────────────────────────────────────────────────

    def _open(self, side: str, entry: float, stop: float, atr_4h: float):
        self._position = side
        self._entry_price = entry
        self._initial_stop = stop
        self._stop_loss = stop
        self._risk_unit = abs(entry - stop)
        self._units_held = 1
        if side == "LONG":
            self._next_pyramid_price = entry + self.pyramid_step_atr * atr_4h
        else:
            self._next_pyramid_price = entry - self.pyramid_step_atr * atr_4h
        self._highest_since_entry = entry
        self._lowest_since_entry = entry
        self._partial_taken = False
        self._bars_in_trade = 0

    def _close(self, close: float, atr: float, reason: str) -> TradeSignal:
        logger.info(f"{self._position} 전량청산 | {reason} | close={close:.5f} "
                    f"units={self._units_held} bars={self._bars_in_trade}")
        self._reset_position()
        return TradeSignal(Signal.EXIT, close, 0.0, reason, atr)

    def _reset_position(self):
        self._position = "NONE"
        self._entry_price = 0.0
        self._initial_stop = 0.0
        self._stop_loss = 0.0
        self._risk_unit = 0.0
        self._units_held = 0
        self._next_pyramid_price = 0.0
        self._highest_since_entry = 0.0
        self._lowest_since_entry = 0.0
        self._partial_taken = False
        self._bars_in_trade = 0

    # ── 외부 인터페이스 ──────────────────────────────────────────────────

    def set_position(self, side: str, entry_price: float, stop_loss: float):
        """엔진 재시작 시 기존 포지션 복원 (피라미드 정보 유실 - 1단으로 가정)"""
        self._position = side
        self._entry_price = entry_price
        self._initial_stop = stop_loss
        self._stop_loss = stop_loss
        self._risk_unit = abs(entry_price - stop_loss)
        self._units_held = 1
        self._highest_since_entry = entry_price
        self._lowest_since_entry = entry_price
        self._partial_taken = False
        # 다음 피라미드 가격은 보수적으로 재계산 불가 → 비활성
        self._next_pyramid_price = float("inf") if side == "LONG" else float("-inf")

    @property
    def current_position(self) -> str:
        return self._position

    @property
    def units_held(self) -> int:
        return self._units_held
