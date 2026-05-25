"""
다종목 포트폴리오 자동매매 메인 (금 MGC + S&P MES 등 동시 운용).

단일종목 엔진(main.py)과 달리, 하나의 하나증권 API 연결로 여러 시장을
독립적으로 운용하고 계좌(자산·리스크 한도)만 공유한다.
  - 시장별: 자신의 종목코드/거래소/계약스펙/전략/봉 집계/포지션
  - 공유:   계좌 자산, 거래당 리스크%, 동시 포지션 한도, 일일 손실 한도

실행:
  python portfolio_main.py [config/portfolio.yaml]

요구사항: Windows + 1QHTS 로그인 + 해외선물 API 신청.
⚠️ 반드시 is_mock: true(모의투자)로 충분히 검증 후 실계좌 전환.
"""

import sys
import time
import threading
from datetime import datetime, date

import yaml

from api.hana_api import HanaAPI
from strategy.donchian_breakout import DonchianBreakoutStrategy
from strategy.ma_crossover import MACrossoverStrategy
from strategy.mean_reversion import MeanReversionStrategy
from strategy.base_strategy import Signal
from risk.risk_manager import RiskManager
from utils.data_handler import DataHandler, OHLCVBar
from utils.logger import setup_logger

logger = setup_logger("portfolio_main")


def load_config(path: str = "config/portfolio.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_strategy(strat_cfg: dict):
    name = strat_cfg.get("name", "donchian_breakout")
    if name == "donchian_breakout":
        return DonchianBreakoutStrategy(
            entry_period=strat_cfg.get("donchian_entry_period", 20),
            exit_period=strat_cfg.get("donchian_exit_period", 10),
            atr_period=strat_cfg.get("atr_period", 14),
            atr_multiplier=strat_cfg.get("atr_multiplier", 2.0),
            trend_filter_period=strat_cfg.get("trend_filter_period", 0),
        )
    if name == "mean_reversion":
        return MeanReversionStrategy(
            atr_period=strat_cfg.get("atr_period", 14),
            atr_multiplier=strat_cfg.get("atr_multiplier", 3.0),
            trend_filter_period=strat_cfg.get("trend_filter_period", 0),
        )
    if name == "ma_crossover":
        return MACrossoverStrategy()
    raise ValueError(f"알 수 없는 전략: {name}")


class MarketRunner:
    """단일 시장의 데이터·전략·포지션 상태를 보유. 사이징/손익은 엔진의 공유 자산을 사용."""

    def __init__(self, m_cfg: dict, usd_krw: float):
        self.name = m_cfg["name"]
        self.code = m_cfg["code"]
        self.exchange = m_cfg.get("exchange", "CME")
        self.contract_month = m_cfg.get("contract_month", "")
        self.tick_size = m_cfg["tick_size"]
        self.tick_value = m_cfg["tick_value"]

        strat_cfg = m_cfg["strategy"]
        self.strategy = build_strategy(strat_cfg)
        self.data = DataHandler(self.code, max_bars=500,
                                timeframe=strat_cfg.get("timeframe", "1D"))
        # 사이징/손익 계산 전용 (open_positions/daily_loss는 엔진이 전역 관리)
        self.risk = RiskManager(account_equity_krw=0, tick_size=self.tick_size,
                                tick_value=self.tick_value, usd_krw_rate=usd_krw)

        self.active_symbol = ""
        self.side = "NONE"      # NONE/LONG/SHORT
        self.qty = 0
        self.entry_price = 0.0

    def resolve_symbol(self) -> str:
        if self.contract_month:
            return f"{self.code}{self.contract_month}"
        # 월물 미지정: 코드 그대로 사용 (정확한 월물은 config에 지정 권장)
        logger.warning(f"[{self.name}] contract_month 미지정 → '{self.code}' 그대로 사용. "
                       f"정확한 근월물 코드를 config에 지정하세요.")
        return self.code


class PortfolioEngine:
    def __init__(self, config: dict):
        api_cfg = config["api"]
        risk_cfg = config["risk"]
        self.exec_cfg = config["execution"]

        self.api = HanaAPI(api_cfg["account_no"], api_cfg["account_pw"], api_cfg["is_mock"])
        self.api.on_login = self._on_login
        self.api.on_tick = self._on_tick
        self.api.on_order_filled = self._on_order_filled

        self.usd_krw = risk_cfg.get("usd_krw_rate", 1350.0)
        self.equity_krw = risk_cfg["account_equity"]
        self.risk_pct = risk_cfg["risk_per_trade_pct"]
        self.max_positions = risk_cfg["max_positions"]
        self.daily_loss_limit_pct = risk_cfg["daily_loss_limit_pct"]
        self.max_contracts = risk_cfg["max_contracts_per_trade"]

        self.markets = [MarketRunner(m, self.usd_krw) for m in config["markets"]]
        self._by_symbol = {}          # active_symbol -> MarketRunner

        self._running = False
        self._open_positions = 0
        self._daily_loss_krw = 0.0
        self._today = date.today()
        self._lock = threading.Lock()

    # ── 시작/종료 ────────────────────────────────────────────────────────────
    def start(self):
        logger.info("=" * 60)
        logger.info(" 다종목 포트폴리오 자동매매 시작")
        logger.info(f" 시장: {', '.join(m.name for m in self.markets)} "
                    f"({'모의투자' if self.api.is_mock else '실계좌'})")
        logger.info(f" 거래당 리스크 {self.risk_pct}% | 동시포지션 최대 {self.max_positions}")
        logger.info("=" * 60)
        if not self.api.connect():
            logger.error("API 연결 실패. 종료.")
            sys.exit(1)

    def stop(self):
        self._running = False
        for m in self.markets:
            if m.active_symbol:
                self.api.unsubscribe_realtime(m.active_symbol)
        self.api.disconnect()
        logger.info("포트폴리오 자동매매 종료")

    # ── 로그인 콜백 ──────────────────────────────────────────────────────────
    def _on_login(self, success: bool):
        if not success:
            logger.error("로그인 실패")
            return
        logger.info("로그인 완료 → 시장별 초기화")
        for m in self.markets:
            m.active_symbol = m.resolve_symbol()
            self._by_symbol[m.active_symbol] = m
            m.data.on_bar_close = (lambda mk: (lambda bar: self._on_bar_close(mk)))(m)
            self._load_history(m)
            self.api.subscribe_realtime(m.active_symbol)
            self._sync_position(m)

        self._running = True
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        logger.info("포트폴리오 루프 시작")

    def _load_history(self, m: MarketRunner):
        need = m.strategy.get_min_bars_required() + 50
        raw = self.api.get_daily_bars(m.active_symbol, need)
        if not raw:
            logger.warning(f"[{m.name}] 과거 데이터 수신 실패 - 실시간 누적 대기")
            return
        for b in raw:
            try:
                ts = datetime.strptime(b["date"], "%Y%m%d")
                m.data.add_bar(OHLCVBar(ts, b["open"], b["high"], b["low"], b["close"], b["volume"]))
            except Exception as e:
                logger.warning(f"[{m.name}] 봉 파싱 오류: {e}")
        logger.info(f"[{m.name}] 과거 데이터 {m.data.bar_count()}봉 로드")

    def _sync_position(self, m: MarketRunner):
        pos = self.api.get_position(m.active_symbol)
        if pos:
            m.side, m.qty, m.entry_price = pos.side, pos.qty, pos.avg_price
            with self._lock:
                self._open_positions += 1
            atr = m.data.atr(14) or (m.tick_size * 10)
            stop = (m.entry_price - 2 * atr) if pos.side == "LONG" else (m.entry_price + 2 * atr)
            if hasattr(m.strategy, "set_position"):
                m.strategy.set_position(pos.side, m.entry_price, stop)
            logger.info(f"[{m.name}] 기존 포지션 복원: {pos}")

    # ── 실시간 틱 → 해당 시장으로 라우팅 ─────────────────────────────────────
    def _on_tick(self, symbol: str, price: float, volume: int, timestamp: datetime):
        m = self._by_symbol.get(symbol)
        if m:
            m.data.update_tick(price, volume, timestamp)

    def _heartbeat_loop(self):
        interval = self.exec_cfg.get("check_interval_sec", 60)
        while self._running:
            self._reset_daily_if_needed()
            logger.debug(f"[현황] 자산 {self.equity_krw/10000:.0f}만원 | "
                         f"오픈 {self._open_positions}/{self.max_positions} | "
                         f"일손실 {self._daily_loss_krw/10000:.1f}만원")
            time.sleep(interval)

    # ── 봉 마감 시 시장별 전략 평가 ──────────────────────────────────────────
    def _on_bar_close(self, m: MarketRunner):
        try:
            if m.data.bar_count() < m.strategy.get_min_bars_required():
                return
            signal = m.strategy.on_bar(m.data)
            if signal.signal in (Signal.LONG, Signal.SHORT) and m.side == "NONE":
                self._open(m, "LONG" if signal.signal == Signal.LONG else "SHORT", signal)
            elif signal.signal == Signal.EXIT and m.side != "NONE":
                self._close(m, signal.reason)
        except Exception as e:
            logger.error(f"[{m.name}] 전략 실행 오류: {e}", exc_info=True)

    def _can_open(self) -> tuple[bool, str]:
        self._reset_daily_if_needed()
        if self._open_positions >= self.max_positions:
            return False, f"동시 포지션 한도 초과 ({self._open_positions}/{self.max_positions})"
        limit = self.equity_krw * (self.daily_loss_limit_pct / 100.0)
        if self._daily_loss_krw >= limit:
            return False, f"일일 손실 한도 도달 ({self._daily_loss_krw:,.0f}/{limit:,.0f}원)"
        return True, "OK"

    def _open(self, m: MarketRunner, side: str, signal):
        ok, reason = self._can_open()
        if not ok:
            logger.warning(f"[{m.name}] 진입 차단: {reason}")
            if hasattr(m.strategy, "reset_position"):
                m.strategy.reset_position()
            return

        m.risk.update_equity(self.equity_krw)
        m.risk.risk_per_trade_pct = self.risk_pct
        m.risk.max_contracts_per_trade = self.max_contracts
        contracts = m.risk.calc_contracts(signal.entry_price, signal.stop_loss)
        if contracts <= 0:
            logger.warning(f"[{m.name}] 계약수 0 - 진입 스킵")
            if hasattr(m.strategy, "reset_position"):
                m.strategy.reset_position()
            return

        if side == "LONG":
            res = self.api.send_market_buy(m.active_symbol, contracts)
        else:
            res = self.api.send_market_sell(m.active_symbol, contracts)

        if res.success:
            m.side, m.qty, m.entry_price = side, contracts, signal.entry_price
            with self._lock:
                self._open_positions += 1
            logger.info(f"[{m.name}] {side} 진입 {contracts}계약 @ {signal.entry_price:.5f} "
                        f"손절 {signal.stop_loss:.5f} | {signal.reason}")
        else:
            logger.error(f"[{m.name}] 주문 실패: {res.msg}")
            if hasattr(m.strategy, "reset_position"):
                m.strategy.reset_position()

    def _close(self, m: MarketRunner, reason: str = ""):
        if m.side == "NONE" or m.qty == 0:
            return
        price = m.data.latest_close()
        if m.side == "LONG":
            res = self.api.send_market_sell(m.active_symbol, m.qty)
        else:
            res = self.api.send_market_buy(m.active_symbol, m.qty)

        if res.success:
            m.risk.update_equity(self.equity_krw)
            pnl = m.risk.calc_pnl_krw(m.side, m.entry_price, price, m.qty)
            with self._lock:
                self.equity_krw += pnl
                self._open_positions = max(0, self._open_positions - 1)
                if pnl < 0:
                    self._daily_loss_krw += abs(pnl)
            logger.info(f"[{m.name}] 청산 {m.qty}계약 @ {price:.5f} | "
                        f"손익 {pnl:+,.0f}원 | {reason}")
            m.side, m.qty, m.entry_price = "NONE", 0, 0.0
        else:
            logger.error(f"[{m.name}] 청산 실패: {res.msg}")

    def _on_order_filled(self, order_no, symbol, qty, price, side):
        logger.info(f"체결 확인 | #{order_no} {symbol} {side} {qty}계약 @ {price:.5f}")

    def _reset_daily_if_needed(self):
        today = date.today()
        if today != self._today:
            self._daily_loss_krw = 0.0
            self._today = today
            logger.info("일일 손실 집계 초기화")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "config/portfolio.yaml"
    engine = PortfolioEngine(load_config(path))
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
