"""
리스크 관리 모듈 (다종목 지원)

담당 기능:
  1. ATR 기반 포지션 사이징 (Fixed Fractional)
  2. 일일 손실 한도 체크 (계좌 단위)
  3. 최대 동시 포지션 제한 (계좌 단위)
  4. 종목별 스펙(tick_size/tick_value)으로 손익·계약수 계산

계좌 자산은 원화 기준이므로 USD 환산이 필요합니다. usd_krw_rate 를 주입/갱신하세요.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional
from utils.logger import setup_logger

logger = setup_logger("risk_manager")


@dataclass
class SymbolSpec:
    """선물 종목 스펙 (CME 기준)"""
    code: str
    tick_size: float          # 최소 가격변동 단위
    tick_value: float         # 1틱당 손익 (USD)
    name: str = ""

    @property
    def point_value(self) -> float:
        """1.0 가격 변동당 손익 (USD)"""
        return self.tick_value / self.tick_size


# 기본 스펙 — CME 호주달러 선물(6A): 100,000 AUD, 1틱(0.0001)=$10
DEFAULT_SPEC = SymbolSpec("6A", 0.0001, 10.0, "AUD/USD")


class RiskManager:
    """
    포지션 사이징 공식 (Fixed Fractional + ATR):
      risk_amount(USD) = equity_usd × risk_pct/100
      loss_per_contract(USD) = (stop_distance / tick_size) × tick_value
      contracts = floor(risk_amount / loss_per_contract)

    daily_loss / max_positions 는 계좌 단위로 적용됩니다(다종목 공유).
    """

    def __init__(self, account_equity_krw: float,
                 risk_per_trade_pct: float = 1.0,
                 daily_loss_limit_pct: float = 3.0,
                 max_positions: int = 3,
                 max_contracts_per_trade: int = 5,
                 usd_krw_rate: float = 1350.0,
                 default_spec: SymbolSpec = DEFAULT_SPEC):

        self.account_equity_krw = account_equity_krw
        self.risk_per_trade_pct = risk_per_trade_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_positions = max_positions
        self.max_contracts_per_trade = max_contracts_per_trade
        self.usd_krw_rate = usd_krw_rate
        self.default_spec = default_spec

        self._daily_loss_krw: float = 0.0
        self._today: date = date.today()
        self._open_positions: int = 0

    # ── 포지션 사이징 ─────────────────────────────────────────────────────────

    def calc_contracts(self, entry_price: float, stop_loss: float,
                       spec: Optional[SymbolSpec] = None) -> int:
        spec = spec or self.default_spec
        stop_distance = abs(entry_price - stop_loss)
        if stop_distance < spec.tick_size:
            logger.warning(f"[{spec.code}] 손절 거리가 너무 작음: {stop_distance:.6f}")
            return 0

        equity_usd = self.account_equity_krw / self.usd_krw_rate
        risk_usd = equity_usd * (self.risk_per_trade_pct / 100.0)

        stop_ticks = stop_distance / spec.tick_size
        loss_per_contract_usd = stop_ticks * spec.tick_value
        if loss_per_contract_usd <= 0:
            return 0

        contracts = int(risk_usd / loss_per_contract_usd)
        contracts = max(0, min(contracts, self.max_contracts_per_trade))

        logger.info(
            f"[{spec.code}] 포지션 사이징 | 계좌=${equity_usd:,.0f} "
            f"리스크=${risk_usd:.0f} 손절={stop_distance:.5f}({stop_ticks:.0f}틱) "
            f"계약당손실=${loss_per_contract_usd:.0f} → {contracts}계약"
        )
        return contracts

    # ── 거래 가능 여부 ────────────────────────────────────────────────────────

    def can_open_position(self) -> "tuple[bool, str]":
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
        self.account_equity_krw = equity_krw

    def update_usd_krw(self, rate: float):
        self.usd_krw_rate = rate

    # ── 손익 계산 ─────────────────────────────────────────────────────────────

    def calc_pnl_usd(self, side: str, entry_price: float, exit_price: float,
                     contracts: int, spec: Optional[SymbolSpec] = None) -> float:
        spec = spec or self.default_spec
        diff = exit_price - entry_price if side == "LONG" else entry_price - exit_price
        ticks = diff / spec.tick_size
        return ticks * spec.tick_value * contracts

    def calc_pnl_krw(self, side: str, entry_price: float, exit_price: float,
                     contracts: int, spec: Optional[SymbolSpec] = None) -> float:
        return self.calc_pnl_usd(side, entry_price, exit_price, contracts, spec) * self.usd_krw_rate

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
            f"[리스크 현황] 자산={self.account_equity_krw/10000:.0f}만원(${equity_usd:,.0f}) | "
            f"오픈={self._open_positions}/{self.max_positions} | "
            f"일손실={self._daily_loss_krw/10000:.1f}만원/한도{limit_krw/10000:.0f}만원 | "
            f"위험/거래={self.risk_per_trade_pct}%"
        )
