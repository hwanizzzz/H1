"""
리스크 관리 모듈

담당 기능:
  1. ATR 기반 포지션 사이징 (Fixed Fractional)
  2. 일일 손실 한도 체크
  3. 최대 동시 포지션 제한
  4. 계약당 리스크 USD → 계약 수 변환
"""

from datetime import date
from utils.logger import setup_logger

logger = setup_logger("risk_manager")

# CME 6A (호주달러 선물) 스펙
AUD_TICK_SIZE  = 0.0001   # 최소 가격변동 단위
AUD_TICK_VALUE = 10.0     # 1틱당 손익 (USD)
AUD_POINT_VALUE = AUD_TICK_VALUE / AUD_TICK_SIZE   # = 100,000 (계약 단위)


class RiskManager:
    """
    포지션 사이징 공식 (Fixed Fractional + ATR):
      risk_amount  = account_equity × risk_pct / 100
      stop_ticks   = stop_distance / tick_size
      stop_usd     = stop_ticks × tick_value
      contracts    = floor(risk_amount_in_usd / stop_usd)

    account_equity는 원화 기준이므로 USD 환산이 필요합니다.
    usd_krw_rate를 주입하거나 주기적으로 갱신하세요.
    """

    def __init__(self, account_equity_krw: float,
                 risk_per_trade_pct: float = 1.0,
                 daily_loss_limit_pct: float = 3.0,
                 max_positions: int = 3,
                 max_contracts_per_trade: int = 5,
                 usd_krw_rate: float = 1350.0):

        self.account_equity_krw = account_equity_krw
        self.risk_per_trade_pct = risk_per_trade_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_positions = max_positions
        self.max_contracts_per_trade = max_contracts_per_trade
        self.usd_krw_rate = usd_krw_rate

        self._daily_loss_krw: float = 0.0
        self._today: date = date.today()
        self._open_positions: int = 0

    # ── 포지션 사이징 ─────────────────────────────────────────────────────────

    def calc_contracts(self, entry_price: float, stop_loss: float) -> int:
        """
        진입가와 손절가를 기반으로 적정 계약 수를 계산합니다.

        Args:
            entry_price: 진입가 (AUD/USD)
            stop_loss:   손절가 (AUD/USD)

        Returns:
            추천 계약 수 (0이면 거래 불가)
        """
        stop_distance = abs(entry_price - stop_loss)
        if stop_distance < AUD_TICK_SIZE:
            logger.warning(f"손절 거리가 너무 작음: {stop_distance:.5f}")
            return 0

        # 계좌 자산 → USD 환산
        equity_usd = self.account_equity_krw / self.usd_krw_rate

        # 1회 리스크 금액 (USD)
        risk_usd = equity_usd * (self.risk_per_trade_pct / 100.0)

        # 1계약당 손실 USD
        stop_ticks = stop_distance / AUD_TICK_SIZE
        loss_per_contract_usd = stop_ticks * AUD_TICK_VALUE

        if loss_per_contract_usd <= 0:
            return 0

        contracts = int(risk_usd / loss_per_contract_usd)
        contracts = min(contracts, self.max_contracts_per_trade)
        contracts = max(contracts, 0)

        logger.info(
            f"포지션 사이징 | 계좌=${equity_usd:,.0f} "
            f"리스크=${risk_usd:.0f} "
            f"손절={stop_distance:.5f}({stop_ticks:.0f}틱) "
            f"계약당손실=${loss_per_contract_usd:.0f} "
            f"→ {contracts}계약"
        )
        return contracts

    # ── 거래 가능 여부 체크 ──────────────────────────────────────────────────

    def can_open_position(self) -> tuple[bool, str]:
        """새 포지션을 열 수 있는지 확인. (가능여부, 사유)"""
        self._reset_daily_if_needed()

        if self._open_positions >= self.max_positions:
            return False, f"최대 포지션 수 초과 ({self._open_positions}/{self.max_positions})"

        daily_loss_limit_krw = self.account_equity_krw * (self.daily_loss_limit_pct / 100.0)
        if self._daily_loss_krw >= daily_loss_limit_krw:
            return False, (f"일일 손실 한도 도달 "
                           f"({self._daily_loss_krw:,.0f}원 / 한도 {daily_loss_limit_krw:,.0f}원)")

        return True, "OK"

    # ── 상태 업데이트 ─────────────────────────────────────────────────────────

    def on_position_opened(self):
        self._open_positions += 1
        logger.info(f"포지션 오픈 → 현재 {self._open_positions}개")

    def on_position_closed(self, pnl_krw: float):
        self._open_positions = max(0, self._open_positions - 1)
        if pnl_krw < 0:
            self._daily_loss_krw += abs(pnl_krw)
        logger.info(f"포지션 청산 | 손익={pnl_krw:+,.0f}원 | 일일손실={self._daily_loss_krw:,.0f}원")

    def update_equity(self, equity_krw: float):
        """계좌 잔고 갱신 (주기적으로 호출)"""
        self.account_equity_krw = equity_krw

    def update_usd_krw(self, rate: float):
        """환율 갱신"""
        self.usd_krw_rate = rate

    # ── 손익 계산 헬퍼 ────────────────────────────────────────────────────────

    @staticmethod
    def calc_pnl_usd(side: str, entry_price: float, exit_price: float,
                     contracts: int) -> float:
        """
        선물 손익 계산 (USD 기준).

        Args:
            side:        "LONG" or "SHORT"
            entry_price: 진입가
            exit_price:  청산가
            contracts:   계약 수
        """
        diff = exit_price - entry_price if side == "LONG" else entry_price - exit_price
        ticks = diff / AUD_TICK_SIZE
        return ticks * AUD_TICK_VALUE * contracts

    def calc_pnl_krw(self, side: str, entry_price: float, exit_price: float,
                     contracts: int) -> float:
        pnl_usd = self.calc_pnl_usd(side, entry_price, exit_price, contracts)
        return pnl_usd * self.usd_krw_rate

    # ── 내부 ─────────────────────────────────────────────────────────────────

    def _reset_daily_if_needed(self):
        today = date.today()
        if today != self._today:
            self._daily_loss_krw = 0.0
            self._today = today
            logger.info("일일 손실 집계 초기화")

    def status_summary(self) -> str:
        equity_usd = self.account_equity_krw / self.usd_krw_rate
        limit_krw = self.account_equity_krw * (self.daily_loss_limit_pct / 100.0)
        return (
            f"[리스크 현황] "
            f"자산={self.account_equity_krw/10000:.0f}만원(${equity_usd:,.0f}) | "
            f"오픈포지션={self._open_positions}/{self.max_positions} | "
            f"일손실={self._daily_loss_krw/10000:.1f}만원/한도{limit_krw/10000:.0f}만원"
        )
