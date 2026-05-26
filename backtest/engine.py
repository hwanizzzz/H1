"""
백테스트 엔진 — 수수료·슬리피지 포함

전략을 과거 OHLCV 봉에 적용해 거래를 시뮬레이션하고 성과 지표를 산출한다.

체결 모델:
  - 신호는 봉 종가 기준으로 발생, 체결도 해당 봉 종가에 슬리피지를 더해 처리(보수적).
  - 진입 매수: fill = close + slippage_ticks*tick_size  (불리하게)
  - 진입 매도: fill = close - slippage_ticks*tick_size
  - 청산도 동일하게 불리한 방향으로 슬리피지 적용
  - 수수료: 계약·체결(편도)당 commission_per_contract USD (진입/청산 각각 부과)

리스크/사이징은 RiskManager 를 그대로 재사용한다.
"""

from dataclasses import dataclass, field
from typing import List, Optional
import numpy as np

from strategy.base_strategy import Signal
from risk.risk_manager import RiskManager, SymbolSpec
from utils.data_handler import DataHandler, OHLCVBar


@dataclass
class Trade:
    entry_time: object
    exit_time: object
    side: str
    entry_price: float
    exit_price: float
    contracts: int
    pnl_usd: float
    commission_usd: float
    reason: str = ""

    @property
    def net_pnl_usd(self) -> float:
        return self.pnl_usd - self.commission_usd


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    initial_equity_usd: float = 0.0

    # 산출 지표
    final_equity_usd: float = 0.0
    total_return_pct: float = 0.0
    num_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    sharpe: float = 0.0

    def summary_row(self) -> str:
        return (f"{self.symbol:<8} {self.strategy:<18} "
                f"수익률 {self.total_return_pct:+7.1f}% | "
                f"거래 {self.num_trades:3d} | "
                f"승률 {self.win_rate:5.1f}% | "
                f"PF {self.profit_factor:4.2f} | "
                f"MDD {self.max_drawdown_pct:5.1f}% | "
                f"Sharpe {self.sharpe:5.2f}")

    def report(self) -> str:
        lines = [
            "=" * 64,
            f" 백테스트 결과: {self.symbol} / {self.strategy}",
            "=" * 64,
            f" 초기자산      : ${self.initial_equity_usd:,.0f}",
            f" 최종자산      : ${self.final_equity_usd:,.0f}",
            f" 총수익률      : {self.total_return_pct:+.2f}%",
            f" 총거래수      : {self.num_trades}",
            f" 승률          : {self.win_rate:.1f}%",
            f" 손익비(PF)    : {self.profit_factor:.2f}",
            f" 평균이익/손실 : ${self.avg_win_usd:,.0f} / ${self.avg_loss_usd:,.0f}",
            f" 최대낙폭(MDD) : {self.max_drawdown_pct:.1f}%",
            f" Sharpe        : {self.sharpe:.2f}",
            "=" * 64,
        ]
        return "\n".join(lines)


class BacktestEngine:

    def __init__(self, spec: SymbolSpec, timeframe: str = "1D",
                 commission_per_contract: float = 2.5,
                 slippage_ticks: float = 1.0,
                 initial_equity_krw: float = 10_000_000,
                 risk_per_trade_pct: float = 3.0,
                 max_contracts_per_trade: int = 5,
                 usd_krw_rate: float = 1350.0):
        self.spec = spec
        self.timeframe = timeframe
        self.commission_per_contract = commission_per_contract
        self.slippage_ticks = slippage_ticks
        self.initial_equity_krw = initial_equity_krw
        self.usd_krw_rate = usd_krw_rate

        self.risk = RiskManager(
            account_equity_krw=initial_equity_krw,
            risk_per_trade_pct=risk_per_trade_pct,
            daily_loss_limit_pct=100.0,      # 백테스트는 일일 한도 비활성
            max_positions=1,
            max_contracts_per_trade=max_contracts_per_trade,
            usd_krw_rate=usd_krw_rate,
            default_spec=spec,
        )

    def _slip(self, price: float, is_buy: bool) -> float:
        d = self.slippage_ticks * self.spec.tick_size
        return price + d if is_buy else price - d

    def run(self, strategy, bars: List[OHLCVBar]) -> BacktestResult:
        data = DataHandler(self.spec.code, timeframe=self.timeframe,
                           max_bars=len(bars) + 1)

        equity_usd = self.initial_equity_krw / self.usd_krw_rate
        result = BacktestResult(symbol=self.spec.code, strategy=strategy.name,
                                initial_equity_usd=equity_usd)
        result.equity_curve.append(equity_usd)

        pos_side: Optional[str] = None
        pos_qty = 0
        pos_entry = 0.0
        pos_entry_time = None

        min_bars = strategy.get_min_bars_required()

        for bar in bars:
            data.add_bar(bar)
            if data.bar_count() < min_bars:
                continue

            signal = strategy.on_bar(data)
            close = bar.close

            if signal.signal in (Signal.LONG, Signal.SHORT) and pos_side is None:
                contracts = self.risk.calc_contracts(signal.entry_price,
                                                     signal.stop_loss, self.spec)
                if contracts <= 0:
                    continue
                is_buy = signal.signal == Signal.LONG
                fill = self._slip(close, is_buy)
                pos_side = "LONG" if is_buy else "SHORT"
                pos_qty = contracts
                pos_entry = fill
                pos_entry_time = bar.timestamp

            elif signal.signal == Signal.EXIT and pos_side is not None:
                is_buy_to_close = pos_side == "SHORT"
                fill = self._slip(close, is_buy_to_close)
                pnl_usd = self.risk.calc_pnl_usd(pos_side, pos_entry, fill,
                                                 pos_qty, self.spec)
                commission = self.commission_per_contract * pos_qty * 2  # 진입+청산
                equity_usd += pnl_usd - commission
                result.trades.append(Trade(
                    entry_time=pos_entry_time, exit_time=bar.timestamp,
                    side=pos_side, entry_price=pos_entry, exit_price=fill,
                    contracts=pos_qty, pnl_usd=pnl_usd, commission_usd=commission,
                    reason=signal.reason,
                ))
                result.equity_curve.append(equity_usd)
                pos_side = None
                pos_qty = 0
                pos_entry = 0.0
                pos_entry_time = None

        self._finalize(result, equity_usd)
        return result

    def _finalize(self, result: BacktestResult, final_equity_usd: float):
        result.final_equity_usd = final_equity_usd
        result.num_trades = len(result.trades)
        if result.initial_equity_usd > 0:
            result.total_return_pct = (
                (final_equity_usd - result.initial_equity_usd)
                / result.initial_equity_usd * 100.0
            )

        if not result.trades:
            return

        nets = [t.net_pnl_usd for t in result.trades]
        wins = [p for p in nets if p > 0]
        losses = [p for p in nets if p <= 0]

        result.win_rate = len(wins) / len(nets) * 100.0
        result.avg_win_usd = float(np.mean(wins)) if wins else 0.0
        result.avg_loss_usd = float(np.mean(losses)) if losses else 0.0

        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        result.profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        # 최대 낙폭
        curve = np.array(result.equity_curve)
        peak = np.maximum.accumulate(curve)
        dd = (curve - peak) / peak
        result.max_drawdown_pct = abs(float(dd.min())) * 100.0

        # Sharpe (거래 단위 수익률 기준)
        rets = np.diff(curve) / curve[:-1]
        if len(rets) > 1 and np.std(rets) > 0:
            result.sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(len(rets)))
