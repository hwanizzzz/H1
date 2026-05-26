"""
SymbolTrader — 단일 종목의 데이터 집계·전략 평가·주문 실행을 담당.

여러 SymbolTrader 가 하나의 HanaAPI 와 하나의 RiskManager(계좌 단위 한도)를
공유하여 다종목 포트폴리오 매매를 구성한다.

이벤트 흐름:
  api 틱 → on_tick → DataHandler.update_tick → (봉 확정 시) _on_bar_closed
  → 전략 평가 → 진입/청산 주문
"""

from datetime import datetime
from typing import Optional

from api.hana_api import HanaAPI
from risk.risk_manager import RiskManager, SymbolSpec
from strategy.base_strategy import Signal
from utils.data_handler import DataHandler, OHLCVBar
from utils.logger import setup_logger

logger = setup_logger("symbol_trader")

# CME 분기물 만기월 코드 (FX·지수)
_QUARTERLY = {3: "H", 6: "M", 9: "U", 12: "Z"}


def resolve_contract(base_code: str, contract_code: str = "",
                     now: Optional[datetime] = None) -> str:
    """
    실거래 종목코드 결정.
      contract_code 가 있으면 그대로 사용 (예: base=MGC, code=J26 → MGCJ26)
      없으면 분기물(HMUZ) 자동 산출 (FX·지수용 폴백)
    """
    if contract_code:
        return f"{base_code}{contract_code}"

    now = now or datetime.now()
    target = next((m for m in sorted(_QUARTERLY) if now.month <= m), None)
    if target is None:
        target, year = 3, now.year + 1
    else:
        year = now.year
    code = f"{base_code}{_QUARTERLY[target]}{str(year)[-2:]}"
    logger.info(f"[{base_code}] 분기물 자동 결정: {code} "
                f"(정확한 월물은 config contract_code 로 지정 권장)")
    return code


class SymbolTrader:

    def __init__(self, spec: SymbolSpec, timeframe: str, strategy,
                 api: HanaAPI, risk: RiskManager,
                 order_type: str = "MARKET", contract_code: str = "",
                 max_bars: int = 500):
        self.spec = spec
        self.timeframe = timeframe
        self.strategy = strategy
        self.api = api
        self.risk = risk
        self.order_type = order_type
        self.contract_code = contract_code

        self.data = DataHandler(spec.code, timeframe, max_bars)
        self.data.on_bar_closed = self._on_bar_closed

        self.active_symbol = ""
        self._side = "NONE"      # NONE / LONG / SHORT
        self._qty = 0
        self._entry = 0.0

    # ── 초기화 ────────────────────────────────────────────────────────────────

    def setup(self):
        """로그인 후 호출: 종목 결정 → 과거데이터 → 구독 → 포지션 복원"""
        self.active_symbol = resolve_contract(self.spec.code, self.contract_code)
        self._load_history()
        self.api.subscribe_realtime(self.active_symbol)
        self._sync_position()

    def _load_history(self):
        need = self.strategy.get_min_bars_required() + 50
        logger.info(f"[{self.active_symbol}] 과거 봉 {need}개 요청")
        raw = self.api.get_daily_bars(self.active_symbol, need)
        if not raw:
            logger.warning(f"[{self.active_symbol}] 과거 데이터 없음 - 실시간 누적 대기")
            return
        for b in raw:
            try:
                ts = datetime.strptime(b["date"], "%Y%m%d")
                self.data.add_bar(OHLCVBar(ts, b["open"], b["high"],
                                           b["low"], b["close"], b["volume"]))
            except Exception as e:
                logger.warning(f"봉 파싱 오류: {e}")
        logger.info(f"[{self.active_symbol}] 과거 {self.data.bar_count()}봉 로드")

    def _sync_position(self):
        pos = self.api.get_position(self.active_symbol)
        if not pos:
            logger.info(f"[{self.active_symbol}] 보유 포지션 없음")
            return
        self._side = pos.side
        self._qty = pos.qty
        self._entry = pos.avg_price
        self.risk.on_position_opened()
        if hasattr(self.strategy, "set_position"):
            atr = self.data.atr(14) or self.spec.tick_size * 10
            stop = (self._entry - 2 * atr if pos.side == "LONG"
                    else self._entry + 2 * atr)
            self.strategy.set_position(pos.side, self._entry, stop)
        logger.info(f"[{self.active_symbol}] 포지션 복원: {pos}")

    # ── 실시간 처리 ───────────────────────────────────────────────────────────

    def on_tick(self, price: float, volume: int, ts: datetime):
        self.data.update_tick(price, volume, ts)   # 봉 확정 시 _on_bar_closed 호출

    def _on_bar_closed(self, bar: OHLCVBar):
        try:
            self._evaluate()
        except Exception as e:
            logger.error(f"[{self.active_symbol}] 전략 평가 오류: {e}", exc_info=True)

    def _evaluate(self):
        if self.data.bar_count() < self.strategy.get_min_bars_required():
            return
        signal = self.strategy.on_bar(self.data)
        close = self.data.latest_close()
        logger.debug(f"[{self.active_symbol}] 신호={signal.signal.value} close={close}")

        if signal.signal == Signal.LONG and self._side == "NONE":
            self._open("LONG", signal)
        elif signal.signal == Signal.SHORT and self._side == "NONE":
            self._open("SHORT", signal)
        elif signal.signal == Signal.EXIT and self._side != "NONE":
            self._close(signal.reason)

    # ── 주문 ─────────────────────────────────────────────────────────────────

    def _open(self, side: str, signal):
        can, reason = self.risk.can_open_position()
        if not can:
            logger.warning(f"[{self.active_symbol}] 진입 차단: {reason}")
            return
        contracts = self.risk.calc_contracts(signal.entry_price, signal.stop_loss, self.spec)
        if contracts <= 0:
            logger.warning(f"[{self.active_symbol}] 계약수 0 - 진입 스킵")
            return

        if side == "LONG":
            res = self.api.send_market_buy(self.active_symbol, contracts)
        else:
            res = self.api.send_market_sell(self.active_symbol, contracts)

        if res.success:
            self._side, self._qty, self._entry = side, contracts, signal.entry_price
            self.risk.on_position_opened()
            logger.info(f"[{self.active_symbol}] {'매수' if side=='LONG' else '매도'} 진입 "
                        f"{contracts}계약 @ {signal.entry_price:.5f} 손절={signal.stop_loss:.5f} "
                        f"| {signal.reason}")
        else:
            logger.error(f"[{self.active_symbol}] 주문 실패: {res.msg}")

    def _close(self, reason: str = ""):
        if self._side == "NONE" or self._qty == 0:
            return
        close_price = self.data.latest_close()
        if self._side == "LONG":
            res = self.api.send_market_sell(self.active_symbol, self._qty)
        else:
            res = self.api.send_market_buy(self.active_symbol, self._qty)

        if res.success:
            pnl_krw = self.risk.calc_pnl_krw(self._side, self._entry, close_price,
                                             self._qty, self.spec)
            self.risk.on_position_closed(pnl_krw)
            logger.info(f"[{self.active_symbol}] 청산 {self._qty}계약 @ {close_price:.5f} "
                        f"손익={pnl_krw:+,.0f}원 | {reason}")
            self._side, self._qty, self._entry = "NONE", 0, 0.0
        else:
            logger.error(f"[{self.active_symbol}] 청산 실패: {res.msg}")

    def stop(self):
        if self.active_symbol:
            self.api.unsubscribe_realtime(self.active_symbol)
