"""
RAMR — Regime-Adaptive Mean Reversion 전략

설계 배경:
  2023-2024 백테스트에서 기존 추세추종 전략(adaptive_trend, multi_tf_aggressive)이
  전 시나리오 손실. 4종목 모두 순이동/등락폭 비율 22~54% 의 횡보장이라
  추세 모델로는 채찍질 손실이 누적됨.

  글로벌 헤지펀드 리서치(AQR Style Premia / Renaissance Stat-Arb / 2024 SSRN
  FX Mean Reversion / Dual-Regime 학술 논문) 종합 결과:
    - 횡보장에는 평균회귀가 통계적으로 유효
    - ADX < 20 영역에서 Bollinger Band + Connors RSI(2)가 가장 강건
    - 단, 강한 추세 진입 시점에는 평균회귀가 폭발하므로 ADX > 25에선 추세 추종으로
      자동 스위칭 필요 (Dual-Regime 시스템)

═══════════════════════════════════════════════════════════════════════════
전략 구조 (단일 TF: 1D)
─────────────────────────────────────────────────────────────────────────
  [1] Regime Classifier (매 봉)
        TREND_UP  : ADX > 25 AND close > EMA200 AND EMA50 > EMA200
        TREND_DN  : ADX > 25 AND close < EMA200 AND EMA50 < EMA200
        RANGE     : ADX < 20
        TRANSITION: 그 외 (거래 금지)

  [2] RANGE 영역 — 평균회귀 (MEAN-REV, 메인 엔진)
        롱: Close < BB(20, 2.0σ) 하단 AND RSI(2) < 10
        숏: Close > BB(20, 2.0σ) 상단 AND RSI(2) > 90
        손절: 1.5 × ATR(14)
        익절: BB 중심선(SMA20) 도달 OR 5봉 경과
        트레일링: +1R 도달 시 BE 이동

  [3] TREND 영역 — 추세 지속 (TREND-CONT, 보조 엔진)
        롱(TREND_UP): EMA20 풀백 후 RSI(14) 50 상향 돌파
        숏(TREND_DN): 미러
        손절: 2.0 × ATR
        부분익절: +3R 에서 50% 청산 + 잔여 BE
        잔여 트레일: Chandelier 3 × ATR
═══════════════════════════════════════════════════════════════════════════
"""

from typing import Optional, List
from strategy.base_strategy import BaseStrategy, TradeSignal, Signal
from utils.logger import setup_logger

logger = setup_logger("ramr")


class RAMRStrategy(BaseStrategy):
    """Regime-Adaptive Mean Reversion."""

    def __init__(
        self,
        # ── Regime ────────────────────────────────────────
        ema_trend_period: int = 200,
        ema_fast_period: int = 50,
        adx_period: int = 14,
        adx_range_max: float = 20.0,
        adx_trend_min: float = 25.0,
        # ── Mean Reversion ────────────────────────────────
        bb_period: int = 20,
        bb_stddev: float = 2.0,
        rsi_short_period: int = 2,
        mr_rsi_long_max: float = 10.0,
        mr_rsi_short_min: float = 90.0,
        mr_stop_atr: float = 1.5,
        mr_max_bars: int = 5,
        mr_with_trend_only: bool = True,
        # 평균회귀를 장기추세(EMA200) 방향과 일치하는 경우에만 진입.
        # Connors TPS 시스템 아이디어 — 추세 거꾸로 평균회귀 진입을 차단.
        # ── Trend Continuation ────────────────────────────
        trend_ema_period: int = 20,
        trend_rsi_period: int = 14,
        trend_rsi_neutral: float = 50.0,
        tc_stop_atr: float = 2.0,
        tc_partial_r: float = 3.0,
        tc_partial_pct: float = 0.5,
        tc_chandelier_atr: float = 3.0,
        tc_enabled: bool = True,
        # ── Shared ────────────────────────────────────────
        atr_period: int = 14,
    ):
        super().__init__("RAMR")
        self.ema_trend_period = ema_trend_period
        self.ema_fast_period = ema_fast_period
        self.adx_period = adx_period
        self.adx_range_max = adx_range_max
        self.adx_trend_min = adx_trend_min
        self.bb_period = bb_period
        self.bb_stddev = bb_stddev
        self.rsi_short_period = rsi_short_period
        self.mr_rsi_long_max = mr_rsi_long_max
        self.mr_rsi_short_min = mr_rsi_short_min
        self.mr_stop_atr = mr_stop_atr
        self.mr_max_bars = mr_max_bars
        self.mr_with_trend_only = mr_with_trend_only
        self.tc_enabled = tc_enabled
        self.trend_ema_period = trend_ema_period
        self.trend_rsi_period = trend_rsi_period
        self.trend_rsi_neutral = trend_rsi_neutral
        self.tc_stop_atr = tc_stop_atr
        self.tc_partial_r = tc_partial_r
        self.tc_partial_pct = tc_partial_pct
        self.tc_chandelier_atr = tc_chandelier_atr
        self.atr_period = atr_period

        # 포지션 상태
        self._mode: str = "NONE"          # "NONE" | "MR" | "TC"
        self._position: str = "NONE"      # "NONE" | "LONG" | "SHORT"
        self._entry_price: float = 0.0
        self._initial_stop: float = 0.0
        self._stop_loss: float = 0.0
        self._risk_unit: float = 0.0
        self._bb_mid_at_entry: float = 0.0
        self._partial_taken: bool = False
        self._highest_since_entry: float = 0.0
        self._lowest_since_entry: float = 0.0
        self._bars_in_trade: int = 0
        self._last_rsi: float = 50.0      # for trend RSI cross detection

    # ── 인터페이스 ────────────────────────────────────────────

    def get_required_timeframes(self) -> List[str]:
        return ["1D"]

    def get_min_bars_required(self) -> int:
        return max(self.ema_trend_period, self.bb_period, self.adx_period * 2) + 10

    def on_bar(self, data) -> TradeSignal:
        data = self._resolve_data(data, "1D")
        if data is None or data.bar_count() < self.get_min_bars_required():
            return TradeSignal(Signal.NONE)

        close = data.latest_close()
        atr = data.atr(self.atr_period)
        adx = data.adx(self.adx_period)
        ema200 = data.ema(self.ema_trend_period)
        ema50 = data.ema(self.ema_fast_period)
        bb_up, bb_mid, bb_dn = data.bollinger_bands(self.bb_period, self.bb_stddev)
        rsi2 = data.rsi(self.rsi_short_period)
        rsi14 = data.rsi(self.trend_rsi_period)
        ema20 = data.ema(self.trend_ema_period)

        # 모든 지표 유효성
        if any(v is None for v in (close, atr, adx, ema200, ema50,
                                    bb_up, bb_mid, bb_dn, rsi2, rsi14, ema20)) \
           or atr == 0:
            self._last_rsi = rsi14 if rsi14 is not None else self._last_rsi
            return TradeSignal(Signal.NONE)

        # ── 보유 포지션이 있으면 우선 관리 ───────────────────
        if self._position != "NONE":
            sig = self._manage_open_position(close, atr, bb_mid, ema200)
            self._last_rsi = rsi14
            return sig

        # ── Regime 분류 ──────────────────────────────────────
        regime = self._classify_regime(close, adx, ema50, ema200)

        # ── RANGE 영역 → 평균회귀 (with-trend 필터) ─────────
        if regime == "RANGE":
            # 롱: BB 하단 이탈 + RSI(2) 과매도 (장기 상승 추세에서만)
            trend_allows_long = (not self.mr_with_trend_only) or (close > ema200)
            if close < bb_dn and rsi2 < self.mr_rsi_long_max and trend_allows_long:
                stop = close - self.mr_stop_atr * atr
                self._open("MR", "LONG", close, stop, bb_mid)
                reason = (f"[MR-RANGE] ADX={adx:.1f} BB↓ RSI2={rsi2:.1f} "
                          f"{'(>EMA200)' if close > ema200 else ''}")
                logger.info(f"MR 롱 진입 | close={close:.5f} stop={stop:.5f} | {reason}")
                self._last_rsi = rsi14
                return TradeSignal(Signal.LONG, close, stop, reason, atr)

            # 숏: BB 상단 돌파 + RSI(2) 과매수 (장기 하락 추세에서만)
            trend_allows_short = (not self.mr_with_trend_only) or (close < ema200)
            if close > bb_up and rsi2 > self.mr_rsi_short_min and trend_allows_short:
                stop = close + self.mr_stop_atr * atr
                self._open("MR", "SHORT", close, stop, bb_mid)
                reason = (f"[MR-RANGE] ADX={adx:.1f} BB↑ RSI2={rsi2:.1f} "
                          f"{'(<EMA200)' if close < ema200 else ''}")
                logger.info(f"MR 숏 진입 | close={close:.5f} stop={stop:.5f} | {reason}")
                self._last_rsi = rsi14
                return TradeSignal(Signal.SHORT, close, stop, reason, atr)

        # ── TREND 영역 → 추세 지속 ───────────────────────────
        if self.tc_enabled and regime == "TREND_UP":
            # EMA20 풀백 후 RSI 50 상향 돌파 (이전 < 50, 현재 >= 50)
            pullback = close >= ema20 and close <= ema20 * 1.005   # 풀백 후 EMA20 근처
            rsi_cross_up = self._last_rsi < self.trend_rsi_neutral <= rsi14
            if pullback and rsi_cross_up:
                stop = close - self.tc_stop_atr * atr
                self._open("TC", "LONG", close, stop, bb_mid)
                reason = (f"[TC-TRUP] ADX={adx:.1f} EMA20풀백 RSI {self._last_rsi:.1f}→{rsi14:.1f}")
                logger.info(f"TC 롱 진입 | close={close:.5f} stop={stop:.5f} | {reason}")
                self._last_rsi = rsi14
                return TradeSignal(Signal.LONG, close, stop, reason, atr,
                                   take_profit_targets=[self.tc_partial_r])

        elif self.tc_enabled and regime == "TREND_DN":
            pullback = close <= ema20 and close >= ema20 * 0.995
            rsi_cross_dn = self._last_rsi > self.trend_rsi_neutral >= rsi14
            if pullback and rsi_cross_dn:
                stop = close + self.tc_stop_atr * atr
                self._open("TC", "SHORT", close, stop, bb_mid)
                reason = (f"[TC-TRDN] ADX={adx:.1f} EMA20풀백 RSI {self._last_rsi:.1f}→{rsi14:.1f}")
                logger.info(f"TC 숏 진입 | close={close:.5f} stop={stop:.5f} | {reason}")
                self._last_rsi = rsi14
                return TradeSignal(Signal.SHORT, close, stop, reason, atr,
                                   take_profit_targets=[self.tc_partial_r])

        # TRANSITION/RANGE 신호 없음
        self._last_rsi = rsi14
        return TradeSignal(Signal.NONE, atr=atr)

    # ── Regime 분류 ────────────────────────────────────────

    def _classify_regime(self, close, adx, ema_fast, ema_slow) -> str:
        if adx >= self.adx_trend_min:
            if close > ema_slow and ema_fast > ema_slow:
                return "TREND_UP"
            if close < ema_slow and ema_fast < ema_slow:
                return "TREND_DN"
        if adx < self.adx_range_max:
            return "RANGE"
        return "TRANSITION"

    # ── 포지션 관리 ────────────────────────────────────────

    def _manage_open_position(self, close, atr, bb_mid, ema200) -> TradeSignal:
        self._bars_in_trade += 1

        if self._position == "LONG":
            self._highest_since_entry = max(self._highest_since_entry, close)
            r_mult = (close - self._entry_price) / self._risk_unit if self._risk_unit else 0

            if self._mode == "MR":
                # 평균회귀: BB 중심선 도달 시 청산
                if close >= self._bb_mid_at_entry:
                    return self._close(close, atr, "MR target: BB mid 도달")
                # 5봉 경과 청산
                if self._bars_in_trade >= self.mr_max_bars:
                    return self._close(close, atr, f"MR time stop ({self._bars_in_trade}봉)")
                # +1R 도달 → BE 이동
                if r_mult >= 1.0 and self._stop_loss < self._entry_price:
                    self._stop_loss = self._entry_price
                # 손절
                if close <= self._stop_loss:
                    return self._close(close, atr, f"MR stop {self._stop_loss:.5f}")

            elif self._mode == "TC":
                # +3R 부분익절 → BE 이동
                if not self._partial_taken and r_mult >= self.tc_partial_r:
                    self._partial_taken = True
                    self._stop_loss = max(self._stop_loss, self._entry_price)
                    logger.info(f"+{self.tc_partial_r}R 부분익절 → BE: {self._stop_loss:.5f}")
                    return TradeSignal(Signal.PARTIAL_TP, close, self._stop_loss,
                                       f"+{self.tc_partial_r}R 부분익절", atr,
                                       partial_close_pct=self.tc_partial_pct)
                # Chandelier (부분익절 후)
                if self._partial_taken:
                    chand = self._highest_since_entry - self.tc_chandelier_atr * atr
                    if chand > self._stop_loss:
                        self._stop_loss = chand
                # 손절
                if close <= self._stop_loss:
                    return self._close(close, atr, f"TC stop {self._stop_loss:.5f}")
                # 추세반전 청산
                if close < ema200:
                    return self._close(close, atr, "EMA200 하향 (추세반전)")

        elif self._position == "SHORT":
            self._lowest_since_entry = min(self._lowest_since_entry, close)
            r_mult = (self._entry_price - close) / self._risk_unit if self._risk_unit else 0

            if self._mode == "MR":
                if close <= self._bb_mid_at_entry:
                    return self._close(close, atr, "MR target: BB mid 도달")
                if self._bars_in_trade >= self.mr_max_bars:
                    return self._close(close, atr, f"MR time stop ({self._bars_in_trade}봉)")
                if r_mult >= 1.0 and self._stop_loss > self._entry_price:
                    self._stop_loss = self._entry_price
                if close >= self._stop_loss:
                    return self._close(close, atr, f"MR stop {self._stop_loss:.5f}")

            elif self._mode == "TC":
                if not self._partial_taken and r_mult >= self.tc_partial_r:
                    self._partial_taken = True
                    self._stop_loss = min(self._stop_loss, self._entry_price)
                    logger.info(f"+{self.tc_partial_r}R 부분익절 → BE: {self._stop_loss:.5f}")
                    return TradeSignal(Signal.PARTIAL_TP, close, self._stop_loss,
                                       f"+{self.tc_partial_r}R 부분익절", atr,
                                       partial_close_pct=self.tc_partial_pct)
                if self._partial_taken:
                    chand = self._lowest_since_entry + self.tc_chandelier_atr * atr
                    if chand < self._stop_loss:
                        self._stop_loss = chand
                if close >= self._stop_loss:
                    return self._close(close, atr, f"TC stop {self._stop_loss:.5f}")
                if close > ema200:
                    return self._close(close, atr, "EMA200 상향 (추세반전)")

        return TradeSignal(Signal.NONE, atr=atr)

    # ── 내부 ──────────────────────────────────────────────

    def _open(self, mode, side, entry, stop, bb_mid):
        self._mode = mode
        self._position = side
        self._entry_price = entry
        self._initial_stop = stop
        self._stop_loss = stop
        self._risk_unit = abs(entry - stop)
        self._bb_mid_at_entry = bb_mid
        self._partial_taken = False
        self._highest_since_entry = entry
        self._lowest_since_entry = entry
        self._bars_in_trade = 0

    def _close(self, close, atr, reason) -> TradeSignal:
        logger.info(f"{self._mode} {self._position} 청산 | {reason} | "
                    f"close={close:.5f} bars={self._bars_in_trade}")
        self._reset_position()
        return TradeSignal(Signal.EXIT, close, 0.0, reason, atr)

    def _reset_position(self):
        self._mode = "NONE"
        self._position = "NONE"
        self._entry_price = 0.0
        self._initial_stop = 0.0
        self._stop_loss = 0.0
        self._risk_unit = 0.0
        self._bb_mid_at_entry = 0.0
        self._partial_taken = False
        self._highest_since_entry = 0.0
        self._lowest_since_entry = 0.0
        self._bars_in_trade = 0

    @property
    def current_position(self) -> str:
        return self._position

    @property
    def current_mode(self) -> str:
        return self._mode
