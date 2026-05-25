"""
이벤트 기반 백테스트 엔진.

라이브와 동일한 전략/리스크/데이터 코드를 그대로 재사용해 봉 단위로 시뮬레이션합니다.
  - 신호는 봉 종가에서 발생, 체결도 같은 봉 종가에 슬리피지를 더해 채결되는 것으로 가정
  - 한 번에 한 포지션 (단일 종목 모델)
  - 손익은 USD로 계산 후 원/달러 환율로 원화 환산 (RiskManager와 동일 규칙)

한계: 종가 체결 가정이라 실제 슬리피지/갭/유동성 영향은 보수적으로만 반영됩니다.
"""

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from strategy.base_strategy import Signal
from risk.risk_manager import RiskManager
from utils.data_handler import DataHandler, OHLCVBar


@dataclass
class Trade:
    side: str
    entry_time: object
    exit_time: object
    entry_price: float
    exit_price: float
    contracts: int
    pnl_krw: float
    reason: str = ""


@dataclass
class BacktestResult:
    initial_equity_krw: float
    final_equity_krw: float
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    timestamps: List[object] = field(default_factory=list)

    # ── 성과 지표 ──────────────────────────────────────────────────────────
    @property
    def total_return_pct(self) -> float:
        if self.initial_equity_krw == 0:
            return 0.0
        return (self.final_equity_krw - self.initial_equity_krw) / self.initial_equity_krw * 100

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> List[Trade]:
        return [t for t in self.trades if t.pnl_krw > 0]

    @property
    def losses(self) -> List[Trade]:
        return [t for t in self.trades if t.pnl_krw <= 0]

    @property
    def win_rate(self) -> float:
        return len(self.wins) / self.num_trades * 100 if self.num_trades else 0.0

    @property
    def gross_profit(self) -> float:
        return sum(t.pnl_krw for t in self.wins)

    @property
    def gross_loss(self) -> float:
        return abs(sum(t.pnl_krw for t in self.losses))

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / self.gross_loss if self.gross_loss > 0 else float("inf")

    @property
    def avg_win(self) -> float:
        return self.gross_profit / len(self.wins) if self.wins else 0.0

    @property
    def avg_loss(self) -> float:
        return -self.gross_loss / len(self.losses) if self.losses else 0.0

    @property
    def expectancy(self) -> float:
        return sum(t.pnl_krw for t in self.trades) / self.num_trades if self.num_trades else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        mdd = 0.0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                mdd = max(mdd, (peak - eq) / peak)
        return mdd * 100

    @property
    def sharpe(self) -> float:
        """봉 수익률 기반 연율화 샤프 (일봉 가정: √252)"""
        if len(self.equity_curve) < 3:
            return 0.0
        eq = np.array(self.equity_curve, dtype=float)
        rets = np.diff(eq) / eq[:-1]
        rets = rets[np.isfinite(rets)]
        if rets.std() == 0 or len(rets) == 0:
            return 0.0
        return float(rets.mean() / rets.std() * np.sqrt(252))


class Backtester:
    def __init__(self, strategy, tick_size: float, tick_value: float,
                 account_equity_krw: float = 50_000_000,
                 risk_per_trade_pct: float = 1.0,
                 usd_krw_rate: float = 1350.0,
                 slippage_ticks: int = 1,
                 max_contracts_per_trade: int = 5,
                 max_bars: int = 1000):
        self.strategy = strategy
        self.tick_size = tick_size
        self.tick_value = tick_value
        self.usd_krw_rate = usd_krw_rate
        self.slippage = slippage_ticks * tick_size
        self.initial_equity_krw = account_equity_krw

        self.risk = RiskManager(
            account_equity_krw=account_equity_krw,
            tick_size=tick_size,
            tick_value=tick_value,
            risk_per_trade_pct=risk_per_trade_pct,
            max_contracts_per_trade=max_contracts_per_trade,
            usd_krw_rate=usd_krw_rate,
        )
        self.data = DataHandler("BACKTEST", max_bars=max_bars)

    def run(self, bars: List[OHLCVBar]) -> BacktestResult:
        equity = self.initial_equity_krw
        result = BacktestResult(self.initial_equity_krw, equity)

        side: Optional[str] = None   # "LONG"/"SHORT"/None
        qty = 0
        entry_fill = 0.0
        entry_time = None

        for bar in bars:
            self.data.add_bar(bar)
            close = bar.close

            # 평가손익 반영한 자산 곡선 기록 (최대낙폭/샤프 계산용)
            mtm = equity
            if side is not None:
                mtm = equity + self.risk.calc_pnl_usd(side, entry_fill, close, qty) * self.usd_krw_rate
            result.equity_curve.append(mtm)
            result.timestamps.append(bar.timestamp)

            signal = self.strategy.on_bar(self.data)

            if signal.signal in (Signal.LONG, Signal.SHORT) and side is None:
                contracts = self.risk.calc_contracts(signal.entry_price, signal.stop_loss)
                if contracts > 0:
                    side = "LONG" if signal.signal == Signal.LONG else "SHORT"
                    entry_fill = close + self.slippage if side == "LONG" else close - self.slippage
                    qty = contracts
                    entry_time = bar.timestamp
                else:
                    # 사이징 0 → 실제 진입 안 했으므로 전략 상태 롤백 (라이브와 동일)
                    if hasattr(self.strategy, "reset_position"):
                        self.strategy.reset_position()

            elif signal.signal == Signal.EXIT and side is not None:
                exit_fill = close - self.slippage if side == "LONG" else close + self.slippage
                pnl = self.risk.calc_pnl_usd(side, entry_fill, exit_fill, qty) * self.usd_krw_rate
                equity += pnl
                self.risk.update_equity(equity)
                result.trades.append(Trade(side, entry_time, bar.timestamp,
                                           entry_fill, exit_fill, qty, pnl, signal.reason))
                side, qty, entry_fill, entry_time = None, 0, 0.0, None

        # 마지막 봉에서 미청산 포지션 강제 청산
        if side is not None and bars:
            last = bars[-1]
            exit_fill = last.close - self.slippage if side == "LONG" else last.close + self.slippage
            pnl = self.risk.calc_pnl_usd(side, entry_fill, exit_fill, qty) * self.usd_krw_rate
            equity += pnl
            result.trades.append(Trade(side, entry_time, last.timestamp,
                                       entry_fill, exit_fill, qty, pnl, "백테스트 종료 강제청산"))

        result.final_equity_krw = equity
        return result


def format_report(result: BacktestResult, title: str = "백테스트 결과") -> str:
    man = lambda krw: f"{krw/10000:,.1f}만원"
    lines = [
        "=" * 56,
        f" {title}",
        "=" * 56,
        f" 초기 자산      : {man(result.initial_equity_krw)}",
        f" 최종 자산      : {man(result.final_equity_krw)}",
        f" 총 수익률      : {result.total_return_pct:+.2f}%",
        f" 최대 낙폭(MDD) : {result.max_drawdown_pct:.2f}%",
        f" 샤프 지수      : {result.sharpe:.2f}",
        "-" * 56,
        f" 총 거래 수     : {result.num_trades}",
        f" 승률           : {result.win_rate:.1f}% ({len(result.wins)}승 {len(result.losses)}패)",
        f" 손익비(PF)     : {result.profit_factor:.2f}",
        f" 평균 수익거래  : {man(result.avg_win)}",
        f" 평균 손실거래  : {man(result.avg_loss)}",
        f" 기대값/거래    : {man(result.expectancy)}",
        "=" * 56,
    ]
    return "\n".join(lines)
