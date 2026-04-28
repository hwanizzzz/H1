"""
리스크 관리 모듈

담당 기능:
  1. ATR 기반 포지션 사이징 (Fixed Fractional)
  2. 일일 손실 한도 체크
  3. 최대 동시 포지션 제한
  4. 계약당 리스크 USD → 계약 수 변환
"""

from collections import deque
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple
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
                 usd_krw_rate: float = 1350.0,
                 # ── Tier 3 어그레시브 파라미터 ─────────────────────────
                 daily_circuit_breaker_pct: float = 0.0,    # 0이면 비활성
                 weekly_drawdown_limit_pct: float = 0.0,    # 0이면 비활성
                 consecutive_loss_halt: int = 0,            # 0이면 비활성
                 pyramid_size_decay: Optional[List[float]] = None,
                 ):

        self.account_equity_krw = account_equity_krw
        self.initial_equity_krw = account_equity_krw
        self.risk_per_trade_pct = risk_per_trade_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_positions = max_positions
        self.max_contracts_per_trade = max_contracts_per_trade
        self.usd_krw_rate = usd_krw_rate

        # 회로 차단기 설정
        self.daily_circuit_breaker_pct = daily_circuit_breaker_pct
        self.weekly_drawdown_limit_pct = weekly_drawdown_limit_pct
        self.consecutive_loss_halt = consecutive_loss_halt
        self.pyramid_size_decay = pyramid_size_decay or [1.0, 0.75, 0.5, 0.5]

        # 상태
        self._daily_loss_krw: float = 0.0
        self._today: date = date.today()
        self._open_positions: int = 0

        # 회로차단기 추적
        self._weekly_pnl: deque = deque(maxlen=7)  # 최근 7일 손익(원)
        self._consecutive_losses: int = 0
        self._halt_until: Optional[datetime] = None
        self._halt_reason: str = ""
        self._weekly_peak_equity: float = account_equity_krw

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

    # ── 피라미드 사이징 ───────────────────────────────────────────────────

    def calc_pyramid_contracts(self, base_contracts: int, units_held: int) -> int:
        """피라미드 추가 단위 사이즈 계산 (안티-마틴게일).

        Args:
            base_contracts: 1단(초기)에 계산된 계약 수
            units_held:     현재까지 보유 중인 단위 수 (1=초기, 2~4=피라미드 추가)

        Returns:
            추가 단위에 사용할 계약 수.
        """
        if units_held <= 0 or units_held > len(self.pyramid_size_decay):
            return 0
        decay = self.pyramid_size_decay[units_held - 1]
        return max(1, int(base_contracts * decay))

    # ── 거래 가능 여부 체크 ──────────────────────────────────────────────────

    def can_open_position(self) -> Tuple[bool, str]:
        """새 포지션을 열 수 있는지 확인. (가능여부, 사유)"""
        self._reset_daily_if_needed()

        # 회로차단기 정지 중인지 확인
        if self._halt_until and datetime.now() < self._halt_until:
            return False, f"거래 정지 중 ({self._halt_reason}, 해제: {self._halt_until:%Y-%m-%d %H:%M})"

        if self._open_positions >= self.max_positions:
            return False, f"최대 포지션 수 초과 ({self._open_positions}/{self.max_positions})"

        daily_loss_limit_krw = self.account_equity_krw * (self.daily_loss_limit_pct / 100.0)
        if self._daily_loss_krw >= daily_loss_limit_krw:
            return False, (f"일일 손실 한도 도달 "
                           f"({self._daily_loss_krw:,.0f}원 / 한도 {daily_loss_limit_krw:,.0f}원)")

        return True, "OK"

    # ── 회로차단기 ───────────────────────────────────────────────────────────

    def check_circuit_breaker(self) -> Tuple[bool, str]:
        """회로차단기 트리거 여부 체크.

        Returns:
            (triggered, reason): triggered=True면 즉시 강제 청산 + 거래 정지.
        """
        # 1) 일일 손실 회로차단기 (강제 청산 임계)
        if self.daily_circuit_breaker_pct > 0:
            limit = self.account_equity_krw * (self.daily_circuit_breaker_pct / 100.0)
            if self._daily_loss_krw >= limit:
                reason = (f"일일 회로차단기 발동 (손실 {self._daily_loss_krw/10000:.1f}만원 "
                          f"≥ {limit/10000:.1f}만원)")
                self._halt_until = datetime.now() + timedelta(hours=24)
                self._halt_reason = reason
                logger.error(f"⛔ {reason} → 24시간 거래 정지")
                return True, reason

        # 2) 주간 낙폭 회로차단기
        if self.weekly_drawdown_limit_pct > 0 and len(self._weekly_pnl) > 0:
            week_pnl = sum(self._weekly_pnl)
            week_dd_pct = -week_pnl / self.initial_equity_krw * 100
            if week_dd_pct >= self.weekly_drawdown_limit_pct:
                reason = (f"주간 낙폭 한도 ({week_dd_pct:.1f}% ≥ "
                          f"{self.weekly_drawdown_limit_pct:.1f}%)")
                self._halt_until = datetime.now() + timedelta(days=3)
                self._halt_reason = reason
                logger.error(f"⛔ {reason} → 3일 거래 정지")
                return True, reason

        # 3) 연속 손실 회로차단기
        if self.consecutive_loss_halt > 0 and self._consecutive_losses >= self.consecutive_loss_halt:
            reason = f"연속 손실 {self._consecutive_losses}회 (한도 {self.consecutive_loss_halt})"
            self._halt_until = datetime.now() + timedelta(hours=24)
            self._halt_reason = reason
            logger.error(f"⛔ {reason} → 24시간 거래 정지")
            return True, reason

        return False, ""

    def reset_halt(self):
        """수동으로 거래 정지 해제 (사용자 확인 후)"""
        self._halt_until = None
        self._halt_reason = ""
        self._consecutive_losses = 0
        logger.info("거래 정지 수동 해제")

    @property
    def is_halted(self) -> bool:
        return bool(self._halt_until and datetime.now() < self._halt_until)

    # ── 상태 업데이트 ─────────────────────────────────────────────────────────

    def on_position_opened(self):
        self._open_positions += 1
        logger.info(f"포지션 오픈 → 현재 {self._open_positions}개")

    def on_position_closed(self, pnl_krw: float):
        self._open_positions = max(0, self._open_positions - 1)
        if pnl_krw < 0:
            self._daily_loss_krw += abs(pnl_krw)
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0  # 익절 시 카운터 리셋
        # 주간 손익 누적
        self._weekly_pnl.append(pnl_krw)
        logger.info(f"포지션 청산 | 손익={pnl_krw:+,.0f}원 | 일일손실={self._daily_loss_krw:,.0f}원 "
                    f"| 연속손실={self._consecutive_losses}회")

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
