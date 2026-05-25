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
from strategy.mean_reversion import MeanReversionStrategy
from strategy.base_strategy import Signal
from risk.risk_manager import RiskManager
from utils.data_handler import DataHandler, OHLCVBar
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
        strat_name = strat_cfg.get("name", "donchian_breakout")
        if strat_name == "donchian_breakout":
            self.strategy = DonchianBreakoutStrategy(
                entry_period        = strat_cfg["donchian_entry_period"],
                exit_period         = strat_cfg["donchian_exit_period"],
                atr_period          = strat_cfg["atr_period"],
                atr_multiplier      = strat_cfg["atr_multiplier"],
                trend_filter_period = strat_cfg.get("trend_filter_period", 0),
            )
        elif strat_name == "ma_crossover":
            self.strategy = MACrossoverStrategy()
        elif strat_name == "mean_reversion":
            self.strategy = MeanReversionStrategy(
                trend_filter_period = strat_cfg.get("trend_filter_period", 100),
                atr_multiplier      = strat_cfg.get("atr_multiplier", 3.0),
            )
        else:
            raise ValueError(f"알 수 없는 전략: {strat_name}")

        # ── 데이터 ────────────────────────────────────────────────────────────
        self.data = DataHandler(self.symbol, max_bars=500,
                                timeframe=strat_cfg.get("timeframe", "1D"))
        # 봉이 마감될 때마다 전략을 1회 평가 (완성된 봉 기준)
        self.data.on_bar_close = self._on_bar_close

        # ── 리스크 매니저 ─────────────────────────────────────────────────────
        self.risk = RiskManager(
            account_equity_krw      = risk_cfg["account_equity"],
            tick_size               = sym_cfg["tick_size"],
            tick_value              = sym_cfg["tick_value"],
            risk_per_trade_pct      = risk_cfg["risk_per_trade_pct"],
            daily_loss_limit_pct    = risk_cfg["daily_loss_limit_pct"],
            max_positions           = risk_cfg["max_positions"],
            max_contracts_per_trade = risk_cfg["max_contracts_per_trade"],
            usd_krw_rate            = risk_cfg.get("usd_krw_rate", 1350.0),
        )

        # ── 실행 설정 ─────────────────────────────────────────────────────────
        self.check_interval = exec_cfg["check_interval_sec"]
        self.order_type     = exec_cfg["order_type"]

        # ── 내부 상태 ─────────────────────────────────────────────────────────
        self._running        = False
        self._position_side  = "NONE"    # "NONE", "LONG", "SHORT"
        self._position_qty   = 0
        self._entry_price    = 0.0
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
        min_bars = self.strategy.get_min_bars_required()
        bars_needed = min_bars + 50
        logger.info(f"과거 일봉 {bars_needed}개 요청...")

        raw_bars = self.api.get_daily_bars(self._active_symbol, bars_needed)
        if not raw_bars:
            logger.warning("과거 데이터 수신 실패 - 실시간 데이터 누적 후 전략 실행")
            return

        for b in raw_bars:
            try:
                ts = datetime.strptime(b["date"], "%Y%m%d")
                self.data.add_bar(OHLCVBar(ts, b["open"], b["high"], b["low"], b["close"], b["volume"]))
            except Exception as e:
                logger.warning(f"봉 파싱 오류: {e}")

        logger.info(f"과거 데이터 {self.data.bar_count()}봉 로드 완료")

    # ── 포지션 동기화 ─────────────────────────────────────────────────────────

    def _sync_position(self):
        """API에서 현재 보유 포지션을 읽어 내부 상태에 반영"""
        position = self.api.get_position(self._active_symbol)
        if position:
            self._position_side = position.side
            self._position_qty  = position.qty
            self._entry_price   = position.avg_price
            self.risk.on_position_opened()
            # 전략 내부 상태도 복원
            if hasattr(self.strategy, "set_position"):
                stop = (self._entry_price - 2 * (self.data.atr(14) or 0.001)
                        if position.side == "LONG"
                        else self._entry_price + 2 * (self.data.atr(14) or 0.001))
                self.strategy.set_position(position.side, self._entry_price, stop)
            logger.info(f"기존 포지션 복원: {position}")
        else:
            logger.info("보유 포지션 없음")

    # ── 실시간 틱 콜백 ────────────────────────────────────────────────────────

    def _on_tick(self, symbol: str, price: float, volume: int, timestamp: datetime):
        self.data.update_tick(price, volume, timestamp)

    # ── 전략 실행 루프 ────────────────────────────────────────────────────────

    def _strategy_loop(self):
        """heartbeat: check_interval 초마다 리스크 현황을 로깅.

        실제 전략 평가는 봉 마감 콜백(_on_bar_close)에서 일어납니다.
        과거에는 이 루프가 매 주기마다 '형성 중인 봉'으로 전략을 돌려
        완성되지 않은 데이터로 신호를 내는 문제가 있었습니다.
        """
        while self._running:
            try:
                logger.debug(self.risk.status_summary())
            except Exception as e:
                logger.error(f"heartbeat 오류: {e}", exc_info=True)
            time.sleep(self.check_interval)

    def _on_bar_close(self, completed_bar):
        """봉 마감 시 호출되어 전략을 1회 평가한다."""
        try:
            self._run_strategy_once()
        except Exception as e:
            logger.error(f"전략 실행 오류: {e}", exc_info=True)

    def _run_strategy_once(self):
        if self.data.bar_count() < self.strategy.get_min_bars_required():
            logger.debug(f"데이터 부족 ({self.data.bar_count()} / {self.strategy.get_min_bars_required()}봉)")
            return

        signal = self.strategy.on_bar(self.data)
        close  = self.data.latest_close()

        logger.debug(f"전략 신호: {signal.signal.value} | close={close:.5f} | {self.risk.status_summary()}")

        if signal.signal == Signal.LONG and self._position_side == "NONE":
            self._open_position("LONG", signal)

        elif signal.signal == Signal.SHORT and self._position_side == "NONE":
            self._open_position("SHORT", signal)

        elif signal.signal == Signal.EXIT and self._position_side != "NONE":
            self._close_position(signal.reason)

    # ── 주문 실행 ─────────────────────────────────────────────────────────────

    def _open_position(self, side: str, signal):
        can_open, reason = self.risk.can_open_position()
        if not can_open:
            logger.warning(f"포지션 진입 차단: {reason}")
            self._rollback_strategy()
            return

        contracts = self.risk.calc_contracts(signal.entry_price, signal.stop_loss)
        if contracts <= 0:
            logger.warning("계산된 계약 수가 0 - 진입 스킵")
            self._rollback_strategy()
            return

        if side == "LONG":
            result = self.api.send_market_buy(self._active_symbol, contracts)
        else:
            result = self.api.send_market_sell(self._active_symbol, contracts)

        if result.success:
            self._position_side = side
            self._position_qty  = contracts
            self._entry_price   = signal.entry_price
            self.risk.on_position_opened()
            logger.info(
                f"{'매수' if side == 'LONG' else '매도'} 진입 완료 | "
                f"{self._active_symbol} {contracts}계약 @ {signal.entry_price:.5f} | "
                f"손절: {signal.stop_loss:.5f} | 사유: {signal.reason}"
            )
        else:
            logger.error(f"주문 실패: {result.msg}")
            self._rollback_strategy()

    def _rollback_strategy(self):
        """진입이 실제로 체결되지 않았을 때 전략 내부 포지션 상태를 되돌림 (desync 방지)"""
        if self._position_side == "NONE" and hasattr(self.strategy, "reset_position"):
            self.strategy.reset_position()
            logger.info("진입 미체결 → 전략 포지션 상태 롤백")

    def _close_position(self, reason: str = ""):
        if self._position_side == "NONE" or self._position_qty == 0:
            return

        close_price = self.data.latest_close()

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
                f"청산 완료 | {self._active_symbol} {self._position_qty}계약 @ {close_price:.5f} | "
                f"손익: {pnl_krw:+,.0f}원 | 사유: {reason}"
            )
            self._position_side = "NONE"
            self._position_qty  = 0
            self._entry_price   = 0.0
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
