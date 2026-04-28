"""
하나증권 1Q Open API - CME 호주달러 선물(6A) 자동매매 메인 실행 파일

실행 방법:
  python main.py

요구 사항:
  - Windows OS
  - 하나증권 1QHTS 실행 및 로그인 상태
  - pip install -r requirements.txt
  - config/config.yaml 설정 완료

주의:
  - 실계좌 전 반드시 모의투자(is_mock: true)로 충분히 테스트
  - 거래 시간: CME FX 선물 (일~금 06:00 ~ 익일 05:00 KST, 1시간 점검 휴장)
"""

import sys
import time
import threading
import yaml
from datetime import datetime
from typing import Optional

from api.hana_api import HanaAPI, ORDER_BUY, ORDER_SELL
from strategy.donchian_breakout import DonchianBreakoutStrategy
from strategy.ma_crossover import MACrossoverStrategy
from strategy.adaptive_trend import AdaptiveTrendStrategy
from strategy.multi_tf_aggressive import MultiTFAggressiveStrategy
from strategy.base_strategy import Signal
from risk.risk_manager import RiskManager
from utils.data_handler import DataHandler, OHLCVBar, TIMEFRAME_SECONDS
from utils.logger import setup_logger

logger = setup_logger("main")


# ── 설정 로드 ─────────────────────────────────────────────────────────────────

def load_config(path: str = "config/config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── 자동매매 엔진 ─────────────────────────────────────────────────────────────

class TradingEngine:
    """
    전략 신호 → 주문 실행을 담당하는 핵심 엔진.
    """

    def __init__(self, config: dict):
        cfg = config
        api_cfg      = cfg["api"]
        sym_cfg      = cfg["symbol"]
        strat_cfg    = cfg["strategy"]
        risk_cfg     = cfg["risk"]
        exec_cfg     = cfg["execution"]
        log_cfg      = cfg["logging"]

        # ── API ──────────────────────────────────────────────────────────────
        self.api = HanaAPI(
            account_no=api_cfg["account_no"],
            account_pw=api_cfg["account_pw"],
            is_mock=api_cfg["is_mock"],
        )
        self.api.on_login        = self._on_login
        self.api.on_tick         = self._on_tick
        self.api.on_order_filled = self._on_order_filled

        # ── 심볼 ─────────────────────────────────────────────────────────────
        self.symbol       = sym_cfg["code"]
        self.tick_size    = sym_cfg["tick_size"]
        self.tick_value   = sym_cfg["tick_value"]
        self.contract_month = sym_cfg.get("contract_month", "")

        # ── 전략 선택 ─────────────────────────────────────────────────────────
        strat_name = strat_cfg.get("name", "adaptive_trend")
        if strat_name == "multi_tf_aggressive":
            mt_cfg = strat_cfg.get("multi_tf_aggressive", {})
            self.strategy = MultiTFAggressiveStrategy(
                trend_ema_fast      = mt_cfg.get("trend_ema_fast", 50),
                trend_ema_slow      = mt_cfg.get("trend_ema_slow", 200),
                trend_adx_period    = mt_cfg.get("trend_adx_period", 14),
                trend_adx_threshold = mt_cfg.get("trend_adx_threshold", 18.0),
                atr_period          = mt_cfg.get("atr_period", 14),
                entry_donchian_period = mt_cfg.get("entry_donchian_period", 20),
                entry_rsi_period    = mt_cfg.get("entry_rsi_period", 14),
                rsi_long_max        = mt_cfg.get("rsi_long_max", 75.0),
                rsi_short_min       = mt_cfg.get("rsi_short_min", 25.0),
                initial_stop_atr    = mt_cfg.get("initial_stop_atr", 1.5),
                chandelier_atr      = mt_cfg.get("chandelier_atr", 3.0),
                partial_take_r      = mt_cfg.get("partial_take_r", 3.0),
                partial_close_pct   = mt_cfg.get("partial_close_pct", 0.5),
                pyramid_enabled     = mt_cfg.get("pyramid_enabled", True),
                pyramid_max_units   = mt_cfg.get("pyramid_max_units", 4),
                pyramid_step_atr    = mt_cfg.get("pyramid_step_atr", 0.5),
            )
        elif strat_name == "adaptive_trend":
            at_cfg = strat_cfg.get("adaptive_trend", {})
            self.strategy = AdaptiveTrendStrategy(
                ema_trend_period = at_cfg.get("ema_trend_period", 200),
                donchian_period  = at_cfg.get("donchian_period", 20),
                adx_period       = at_cfg.get("adx_period", 14),
                adx_threshold    = at_cfg.get("adx_threshold", 20.0),
                atr_period       = at_cfg.get("atr_period", 14),
                atr_avg_period   = at_cfg.get("atr_avg_period", 50),
                atr_ratio_min    = at_cfg.get("atr_ratio_min", 0.7),
                rsi_period       = at_cfg.get("rsi_period", 14),
                rsi_long_max     = at_cfg.get("rsi_long_max", 75.0),
                rsi_short_min    = at_cfg.get("rsi_short_min", 25.0),
                initial_stop_atr = at_cfg.get("initial_stop_atr", 2.0),
                chandelier_atr   = at_cfg.get("chandelier_atr", 3.0),
                partial_take_r   = at_cfg.get("partial_take_r", 2.0),
            )
        elif strat_name == "donchian_breakout":
            self.strategy = DonchianBreakoutStrategy(
                entry_period   = strat_cfg["donchian_entry_period"],
                exit_period    = strat_cfg["donchian_exit_period"],
                atr_period     = strat_cfg["atr_period"],
                atr_multiplier = strat_cfg["atr_multiplier"],
            )
        elif strat_name == "ma_crossover":
            self.strategy = MACrossoverStrategy()
        else:
            raise ValueError(f"알 수 없는 전략: {strat_name}")

        # ── 데이터 (멀티 타임프레임 지원) ─────────────────────────────────────
        # 전략이 요구하는 모든 TF에 대해 DataHandler를 생성
        timeframes = self.strategy.get_required_timeframes()
        self.data_dict: dict[str, DataHandler] = {
            tf: DataHandler(self.symbol, max_bars=500, timeframe=tf)
            for tf in timeframes
        }
        # 단일 TF 전략 호환 (구버전 self.data 인터페이스)
        self.data = self.data_dict.get(timeframes[0])

        # ── 리스크 매니저 (Tier 3 회로차단기 옵션 포함) ──────────────────────
        self.risk = RiskManager(
            account_equity_krw          = risk_cfg["account_equity"],
            risk_per_trade_pct          = risk_cfg["risk_per_trade_pct"],
            daily_loss_limit_pct        = risk_cfg["daily_loss_limit_pct"],
            max_positions               = risk_cfg["max_positions"],
            max_contracts_per_trade     = risk_cfg["max_contracts_per_trade"],
            daily_circuit_breaker_pct   = risk_cfg.get("daily_circuit_breaker_pct", 0.0),
            weekly_drawdown_limit_pct   = risk_cfg.get("weekly_drawdown_limit_pct", 0.0),
            consecutive_loss_halt       = risk_cfg.get("consecutive_loss_halt", 0),
            pyramid_size_decay          = risk_cfg.get("pyramid_size_decay", [1.0, 0.75, 0.5, 0.5]),
        )

        # ── 실행 설정 ─────────────────────────────────────────────────────────
        self.check_interval = exec_cfg["check_interval_sec"]
        self.order_type     = exec_cfg["order_type"]

        # ── 내부 상태 ─────────────────────────────────────────────────────────
        self._running        = False
        self._position_side  = "NONE"    # "NONE", "LONG", "SHORT"
        self._position_qty   = 0
        self._entry_price    = 0.0
        self._units_held     = 0         # 피라미드 단위 수 (0 = 신규/없음)
        self._base_contracts = 0         # 1단(초기) 진입 계약 수 — 피라미딩 사이징 기준
        self._active_symbol  = ""        # 실제 거래 종목코드 (만기월 포함, 예: "6AH26")

    # ── 시작 / 종료 ───────────────────────────────────────────────────────────

    def start(self):
        logger.info("=" * 60)
        logger.info(f" 하나증권 해외선물 자동매매 시작")
        logger.info(f" 전략: {self.strategy.name}")
        logger.info(f" 종목: CME {self.symbol} ({'모의투자' if self.api.is_mock else '실계좌'})")
        logger.info("=" * 60)

        if not self.api.connect():
            logger.error("API 연결 실패. 프로그램 종료.")
            sys.exit(1)

    def stop(self):
        self._running = False
        self.api.unsubscribe_realtime(self._active_symbol)
        self.api.disconnect()
        logger.info("자동매매 종료")

    # ── 로그인 완료 콜백 ──────────────────────────────────────────────────────

    def _on_login(self, success: bool):
        if not success:
            logger.error("로그인 실패")
            return

        logger.info("로그인 완료 → 초기 데이터 로드 시작")
        self._active_symbol = self._resolve_symbol()
        self._load_historical_data()

        # 실시간 데이터 구독
        self.api.subscribe_realtime(self._active_symbol)

        # 기존 포지션 복원
        self._sync_position()

        # 전략 실행 루프 시작
        self._running = True
        t = threading.Thread(target=self._strategy_loop, daemon=True)
        t.start()

        logger.info(f"자동매매 루프 시작 (체크 주기: {self.check_interval}초)")

    def _resolve_symbol(self) -> str:
        """최근월물 종목코드 결정 (예: 6A + H26 = 6AH26)"""
        if self.contract_month:
            # config에 수동 지정된 경우 그대로 사용
            return f"{self.symbol}{self.contract_month}"

        # 자동: 현재 날짜 기준 최근월물 계산
        now = datetime.now()
        # CME FX 선물 만기: 3월(H), 6월(M), 9월(U), 12월(Z)
        month_codes = {3: "H", 6: "M", 9: "U", 12: "Z"}
        roll_months = sorted(month_codes.keys())

        target_month = None
        for m in roll_months:
            if now.month <= m:
                target_month = m
                break
        if target_month is None:
            target_month = roll_months[0]
            year = now.year + 1
        else:
            year = now.year

        code = f"{self.symbol}{month_codes[target_month]}{str(year)[-2:]}"
        logger.info(f"최근월물 자동 결정: {code}")
        return code

    # ── 초기 데이터 로드 ──────────────────────────────────────────────────────

    def _load_historical_data(self):
        """전략이 요구하는 모든 TF에 대해 과거 봉 데이터 로드."""
        min_bars = self.strategy.get_min_bars_required()
        bars_needed = min_bars + 50

        for tf, handler in self.data_dict.items():
            logger.info(f"[{tf}] 과거 봉 {bars_needed}개 요청...")
            raw_bars = self.api.get_bars(self._active_symbol, tf, bars_needed)
            if not raw_bars:
                logger.warning(f"[{tf}] 과거 데이터 수신 실패")
                continue

            for b in raw_bars:
                try:
                    if "timestamp" in b:
                        ts = b["timestamp"]
                    else:
                        ts = datetime.strptime(b["date"], "%Y%m%d")
                    handler.add_bar(OHLCVBar(
                        ts, b["open"], b["high"], b["low"], b["close"], b["volume"]
                    ))
                except Exception as e:
                    logger.warning(f"[{tf}] 봉 파싱 오류: {e}")

            logger.info(f"[{tf}] 과거 데이터 {handler.bar_count()}봉 로드 완료")

    # ── 포지션 동기화 ─────────────────────────────────────────────────────────

    def _sync_position(self):
        """API에서 현재 보유 포지션을 읽어 내부 상태에 반영"""
        position = self.api.get_position(self._active_symbol)
        if position:
            self._position_side = position.side
            self._position_qty  = position.qty
            self._entry_price   = position.avg_price
            self._units_held    = 1   # 보수적으로 1단으로 가정 (피라미드 정보 유실)
            self._base_contracts = position.qty
            self.risk.on_position_opened()
            # 전략 내부 상태도 복원 (메인 TF 기준)
            main_tf = self.strategy.get_required_timeframes()[0]
            atr = self.data_dict[main_tf].atr(14) or 0.001
            if hasattr(self.strategy, "set_position"):
                stop = (self._entry_price - 2 * atr
                        if position.side == "LONG"
                        else self._entry_price + 2 * atr)
                self.strategy.set_position(position.side, self._entry_price, stop)
            logger.info(f"기존 포지션 복원: {position}")
        else:
            logger.info("보유 포지션 없음")

    # ── 실시간 틱 콜백 (모든 TF 핸들러에 분배) ───────────────────────────────

    def _on_tick(self, symbol: str, price: float, volume: int, timestamp: datetime):
        for handler in self.data_dict.values():
            handler.update_tick(price, volume, timestamp)

    # ── 전략 실행 루프 ────────────────────────────────────────────────────────

    def _strategy_loop(self):
        """check_interval 초마다 전략 신호를 계산하고 주문 실행"""
        while self._running:
            try:
                self._run_strategy_once()
            except Exception as e:
                logger.error(f"전략 실행 오류: {e}", exc_info=True)
            time.sleep(self.check_interval)

    def _run_strategy_once(self):
        # 회로차단기 우선 체크 — 발동 시 강제 청산
        triggered, reason = self.risk.check_circuit_breaker()
        if triggered:
            if self._position_side != "NONE":
                logger.error(f"회로차단기 강제 청산 → {reason}")
                self._close_position(f"회로차단기: {reason}", full_close=True)
            return

        # 메인 TF 기준 데이터 충분성 확인
        main_tf = self.strategy.get_required_timeframes()[0]
        main_data = self.data_dict[main_tf]
        if main_data.bar_count() < self.strategy.get_min_bars_required():
            logger.debug(f"데이터 부족 ({main_data.bar_count()} / "
                         f"{self.strategy.get_min_bars_required()}봉, {main_tf})")
            return

        # 전략 호출 — 멀티TF 전략은 dict, 단일TF 전략은 자동 resolve
        signal = self.strategy.on_bar(self.data_dict)
        close = main_data.latest_close()

        logger.debug(f"전략 신호: {signal.signal.value} | close={close:.5f} | "
                     f"{self.risk.status_summary()}")

        # ── 신규 진입 ──────────────────────────────────────────────────
        if signal.signal == Signal.LONG and self._position_side == "NONE":
            self._open_position("LONG", signal)

        elif signal.signal == Signal.SHORT and self._position_side == "NONE":
            self._open_position("SHORT", signal)

        # ── 피라미드 추가 진입 ─────────────────────────────────────────
        elif signal.signal == Signal.SCALE_IN and self._position_side != "NONE":
            self._add_to_position(signal)

        # ── 부분 익절 ──────────────────────────────────────────────────
        elif signal.signal == Signal.PARTIAL_TP and self._position_side != "NONE":
            self._partial_close(signal)

        # ── 전량 청산 ──────────────────────────────────────────────────
        elif signal.signal == Signal.EXIT and self._position_side != "NONE":
            self._close_position(signal.reason, full_close=True)

    # ── 주문 실행 ─────────────────────────────────────────────────────────────

    def _open_position(self, side: str, signal):
        can_open, reason = self.risk.can_open_position()
        if not can_open:
            logger.warning(f"포지션 진입 차단: {reason}")
            return

        contracts = self.risk.calc_contracts(signal.entry_price, signal.stop_loss)
        if contracts <= 0:
            logger.warning("계산된 계약 수가 0 - 진입 스킵")
            return

        if side == "LONG":
            result = self.api.send_market_buy(self._active_symbol, contracts)
        else:
            result = self.api.send_market_sell(self._active_symbol, contracts)

        if result.success:
            self._position_side  = side
            self._position_qty   = contracts
            self._entry_price    = signal.entry_price
            self._units_held     = 1
            self._base_contracts = contracts
            self.risk.on_position_opened()
            logger.info(
                f"{'매수' if side == 'LONG' else '매도'} 진입 완료 (1단) | "
                f"{self._active_symbol} {contracts}계약 @ {signal.entry_price:.5f} | "
                f"손절: {signal.stop_loss:.5f} | 사유: {signal.reason}"
            )
        else:
            logger.error(f"주문 실패: {result.msg}")

    def _add_to_position(self, signal):
        """피라미드 추가 진입 (기존 포지션과 같은 방향으로 N계약 추가)"""
        if self._position_side == "NONE":
            return

        next_unit = self._units_held + 1
        add_contracts = self.risk.calc_pyramid_contracts(self._base_contracts, next_unit)
        if add_contracts <= 0:
            logger.warning("피라미드 계약 수 0 - 추가 스킵")
            return

        if self._position_side == "LONG":
            result = self.api.send_market_buy(self._active_symbol, add_contracts)
        else:
            result = self.api.send_market_sell(self._active_symbol, add_contracts)

        if result.success:
            # 평균 진입가 업데이트
            new_total = self._position_qty + add_contracts
            self._entry_price = (
                self._entry_price * self._position_qty
                + signal.entry_price * add_contracts
            ) / new_total
            self._position_qty = new_total
            self._units_held = next_unit
            logger.info(
                f"피라미드 {next_unit}단 추가 | +{add_contracts}계약 "
                f"@ {signal.entry_price:.5f} | 총 {new_total}계약 "
                f"평단={self._entry_price:.5f} 손절={signal.stop_loss:.5f}"
            )
        else:
            logger.error(f"피라미드 주문 실패: {result.msg}")

    def _partial_close(self, signal):
        """부분 익절 (보유의 N% 청산)"""
        if self._position_side == "NONE" or self._position_qty == 0:
            return

        close_qty = max(1, int(self._position_qty * signal.partial_close_pct))
        if close_qty >= self._position_qty:
            # 전량 청산이 되어버리면 그냥 _close_position으로
            self._close_position(signal.reason, full_close=True)
            return

        close_price = self.data_dict[self.strategy.get_required_timeframes()[0]].latest_close()

        if self._position_side == "LONG":
            result = self.api.send_market_sell(self._active_symbol, close_qty)
        else:
            result = self.api.send_market_buy(self._active_symbol, close_qty)

        if result.success:
            pnl_krw = self.risk.calc_pnl_krw(
                self._position_side, self._entry_price, close_price, close_qty
            )
            self._position_qty -= close_qty
            # 부분 청산은 _open_positions 카운트에 영향 없음 (포지션 자체는 유지)
            logger.info(
                f"부분 익절 완료 | {close_qty}계약 청산 @ {close_price:.5f} "
                f"| 손익: {pnl_krw:+,.0f}원 | 잔여 {self._position_qty}계약 | {signal.reason}"
            )
        else:
            logger.error(f"부분 익절 주문 실패: {result.msg}")

    def _close_position(self, reason: str = "", full_close: bool = True):
        if self._position_side == "NONE" or self._position_qty == 0:
            return

        main_tf = self.strategy.get_required_timeframes()[0]
        close_price = self.data_dict[main_tf].latest_close()

        if self._position_side == "LONG":
            result = self.api.send_market_sell(self._active_symbol, self._position_qty)
        else:
            result = self.api.send_market_buy(self._active_symbol, self._position_qty)

        if result.success:
            pnl_krw = self.risk.calc_pnl_krw(
                self._position_side, self._entry_price, close_price, self._position_qty
            )
            self.risk.on_position_closed(pnl_krw)
            logger.info(
                f"전량 청산 완료 | {self._active_symbol} {self._position_qty}계약 "
                f"@ {close_price:.5f} | 손익: {pnl_krw:+,.0f}원 | 사유: {reason}"
            )
            self._position_side  = "NONE"
            self._position_qty   = 0
            self._entry_price    = 0.0
            self._units_held     = 0
            self._base_contracts = 0
        else:
            logger.error(f"청산 주문 실패: {result.msg}")

    # ── 주문 체결 콜백 ────────────────────────────────────────────────────────

    def _on_order_filled(self, order_no: str, symbol: str, qty: int,
                          price: float, side: str):
        logger.info(f"체결 확인 | #{order_no} {symbol} {side} {qty}계약 @ {price:.5f}")


# ── 엔트리포인트 ──────────────────────────────────────────────────────────────

def main():
    config = load_config()
    engine = TradingEngine(config)

    try:
        engine.start()
        logger.info("Ctrl+C로 종료")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("사용자 종료 요청")
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
