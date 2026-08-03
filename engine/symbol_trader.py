"""
SymbolTrader — 단일 종목의 데이터 집계·전략 평가·주문 실행 담당.

새 하나 Open API 명세에 맞춰 HanaFuturesClient 를 사용.
여러 SymbolTrader 가 하나의 HanaFuturesClient 와 하나의 RiskManager 를 공유.

주요 흐름:
  1) setup(): 실시간 시세 구독 + 기존 포지션 복원
  2) on_tick(): 틱 → DataHandler.update_tick → 봉 확정 시 _on_bar_closed
  3) _on_bar_closed(): 전략 신호 평가 → 진입/청산 주문
"""

from datetime import datetime
from typing import Optional

from api.hana_api import HanaFuturesClient, Position as ApiPosition
from risk.risk_manager import RiskManager, SymbolSpec
from strategy.base_strategy import Signal
from utils.data_handler import DataHandler, OHLCVBar
from utils.logger import setup_logger

logger = setup_logger("symbol_trader")

# CME 분기물 만기월 코드 (자동 폴백용)
_QUARTERLY = {3: "H", 6: "M", 9: "U", 12: "Z"}


def resolve_contract(base_code: str, contract_code: str = "",
                     now: Optional[datetime] = None) -> str:
    """
    실거래 종목코드 결정.
      contract_code 있으면 그대로 사용 (예: MGC + Q26 → MGCQ26)
      없으면 분기물(HMUZ) 자동 산출 (선물지수용 폴백)
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
                 client: HanaFuturesClient, risk: RiskManager,
                 order_prc_type: str = "MARKET", contract_code: str = "",
                 quote_code: str = "", max_bars: int = 500):
        """
        Args:
            spec           : 종목 스펙 (tick_size, tick_value 등)
            timeframe      : 봉 타임프레임
            strategy       : 전략 인스턴스
            client         : HanaFuturesClient (다종목 공유)
            risk           : RiskManager (계좌 단위, 다종목 공유)
            order_prc_type : "MARKET" / "LIMIT"
            contract_code  : 주문 종목코드 접미어 (예: "Q26" → PRDT_CD = spec.code + "Q26")
            quote_code     : 시세 종목코드 (비우면 order symbol 과 동일).
                             하나 API v3.2 부터 시세/주문 코드 분리 가능.
        """
        self.spec = spec
        self.timeframe = timeframe
        self.strategy = strategy
        self.client = client
        self.risk = risk
        self.order_prc_type = order_prc_type
        self.contract_code = contract_code
        self.quote_code_override = quote_code

        self.data = DataHandler(spec.code, timeframe, max_bars)
        self.data.on_bar_closed = self._on_bar_closed

        self.order_symbol = ""          # 주문용 코드 (예: MGCQ26)
        self.quote_symbol = ""          # 시세 구독용 코드 (대부분 동일)
        self._side = "NONE"             # NONE / LONG / SHORT
        self._qty = 0
        self._entry = 0.0

        # FID 폴링(V10 실시간 대체) 상태
        self._last_cum_volume = 0
        self._poll_count = 0

    # ── 초기화 ────────────────────────────────────────────────────────────────

    def setup(self):
        """로그인 후 호출: 종목코드 결정 → 실시간 구독 → 기존 포지션 복원."""
        self.order_symbol = resolve_contract(self.spec.code, self.contract_code)
        self.quote_symbol = self.quote_code_override or self.order_symbol
        logger.info(f"[{self.spec.code}] 주문 {self.order_symbol} / 시세 {self.quote_symbol}")

        # 실시간 시세 구독
        self.client.subscribe_tick(self.quote_symbol)

        # 기존 포지션 복원
        self._sync_position()

    def _sync_position(self):
        """API 포지션 조회 → 내부 상태 반영."""
        positions = self.client.get_positions()
        mine = next((p for p in positions if p.symbol == self.order_symbol), None)
        if not mine:
            logger.info(f"[{self.order_symbol}] 보유 포지션 없음")
            return
        self._side = mine.side
        self._qty = mine.qty
        self._entry = mine.avg_price
        self.risk.on_position_opened()
        if hasattr(self.strategy, "set_position"):
            atr = self.data.atr(14) or self.spec.tick_size * 10
            stop = (self._entry - 2 * atr if mine.side == "LONG"
                    else self._entry + 2 * atr)
            self.strategy.set_position(mine.side, self._entry, stop)
        logger.info(f"[{self.order_symbol}] 포지션 복원: {mine}")

    # ── 실시간 처리 ───────────────────────────────────────────────────────────

    def route_tick(self, quote_symbol: str, price: float, volume: int, ts: datetime):
        """PortfolioEngine 에서 종목 매칭 후 호출 (V10 실시간용)."""
        if quote_symbol != self.quote_symbol:
            return
        self.data.update_tick(price, volume, ts)   # 봉 확정 시 _on_bar_closed

    def poll_quote(self, quote_dict: dict, now: datetime):
        """
        PortfolioEngine 주기 폴러가 FID 응답 딕셔너리로 호출.
        FID 1000 응답을 pseudo-tick 으로 변환해 DataHandler 에 주입.

        FID 매핑: 4=현재가, 8=시간(HHMMSS), 11=누적거래량,
                 13=시가, 14=고가, 15=저가
        """
        self._poll_count += 1
        debug_this = self._poll_count <= 5   # 첫 5회는 항상 상세 로그

        try:
            price = float((quote_dict.get("4") or "0").replace(",", ""))
            cum_vol_raw = (quote_dict.get("11") or "0").replace(",", "")
            cum_vol = int(float(cum_vol_raw))
            time_str = (quote_dict.get("8") or "").strip()
        except (ValueError, TypeError) as e:
            logger.warning(f"[{self.quote_symbol}] 폴링#{self._poll_count} "
                           f"파싱 실패: {e} | dict={quote_dict}")
            return

        if price <= 0:
            # 응답은 왔지만 시세가 0. 원인 파악 위해 첫 몇 회는 dict 통째로 로그.
            if debug_this:
                logger.warning(f"[{self.quote_symbol}] 폴링#{self._poll_count} "
                               f"price=0 (응답 왔으나 시세 없음). dict={quote_dict}")
            return

        tick_vol = max(0, cum_vol - self._last_cum_volume) if self._last_cum_volume else 0
        self._last_cum_volume = cum_vol
        ts = self._parse_hhmmss(time_str) or now

        if debug_this or self._poll_count % 100 == 0:
            logger.info(f"[{self.quote_symbol}] 폴링#{self._poll_count} "
                        f"price={price} 누적거래량={cum_vol} 틱량={tick_vol} time={time_str}")

        self.data.update_tick(price, tick_vol, ts)

    @staticmethod
    def _parse_hhmmss(hhmmss: str):
        if not hhmmss or len(hhmmss) < 6:
            return None
        try:
            now = datetime.now()
            return now.replace(hour=int(hhmmss[:2]), minute=int(hhmmss[2:4]),
                               second=int(hhmmss[4:6]), microsecond=0)
        except (ValueError, TypeError):
            return None

    def _on_bar_closed(self, bar: OHLCVBar):
        try:
            self._evaluate()
        except Exception as e:
            logger.error(f"[{self.order_symbol}] 전략 평가 오류: {e}", exc_info=True)

    def _evaluate(self):
        if self.data.bar_count() < self.strategy.get_min_bars_required():
            return
        signal = self.strategy.on_bar(self.data)
        close = self.data.latest_close()
        logger.debug(f"[{self.order_symbol}] 신호={signal.signal.value} close={close}")

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
            logger.warning(f"[{self.order_symbol}] 진입 차단: {reason}")
            return
        contracts = self.risk.calc_contracts(signal.entry_price, signal.stop_loss, self.spec)
        if contracts <= 0:
            logger.warning(f"[{self.order_symbol}] 계약수 0 - 진입 스킵")
            return

        api_side = "BUY" if side == "LONG" else "SELL"
        res = self.client.send_order(
            symbol=self.order_symbol, side=api_side, qty=contracts,
            price=signal.entry_price if self.order_prc_type == "LIMIT" else None,
            prc_type=self.order_prc_type,
        )

        if res.success:
            self._side, self._qty, self._entry = side, contracts, signal.entry_price
            self.risk.on_position_opened()
            logger.info(
                f"[{self.order_symbol}] {api_side} 진입 #{res.order_no} "
                f"{contracts}계약 @ {signal.entry_price:.5f} 손절={signal.stop_loss:.5f} "
                f"| {signal.reason}"
            )
        else:
            logger.error(f"[{self.order_symbol}] 주문 실패: {res.msg}")

    def _close(self, reason: str = ""):
        if self._side == "NONE" or self._qty == 0:
            return

        close_price = self.data.latest_close()
        api_side = "SELL" if self._side == "LONG" else "BUY"
        res = self.client.send_order(
            symbol=self.order_symbol, side=api_side, qty=self._qty,
            prc_type=self.order_prc_type,
            price=close_price if self.order_prc_type == "LIMIT" else None,
        )

        if res.success:
            pnl_krw = self.risk.calc_pnl_krw(self._side, self._entry, close_price,
                                             self._qty, self.spec)
            self.risk.on_position_closed(pnl_krw)
            logger.info(
                f"[{self.order_symbol}] 청산 #{res.order_no} {self._qty}계약 "
                f"@ {close_price:.5f} 손익={pnl_krw:+,.0f}원 | {reason}"
            )
            self._side, self._qty, self._entry = "NONE", 0, 0.0
        else:
            logger.error(f"[{self.order_symbol}] 청산 실패: {res.msg}")

    # ── 정리 ─────────────────────────────────────────────────────────────────

    def stop(self):
        if self.quote_symbol:
            try:
                self.client.unsubscribe_tick(self.quote_symbol)
            except Exception:
                pass
