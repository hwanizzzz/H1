"""
다중 시장 포트폴리오 백테스트.

여러 시장(금·S&P·호주달러 등)을 하나의 계좌로 동시에 운용한다.
  - 각 시장은 자신의 전략/계약스펙으로 독립적으로 진입·청산
  - 포지션 사이징은 '현재 전체 자산'의 risk_pct로 매 거래 계산 (시장별 독립)
  - 자산곡선은 전 시장의 실현손익 + 미실현손익을 매일 합산

핵심 목적: 비상관 시장에 분산하면 단일 시장 대비
수익이 더 꾸준하고(샤프↑) 낙폭(MDD)이 작아지는 것을 보여준다.
"""

from dataclasses import dataclass, field
from typing import List, Callable, Optional

import numpy as np

from strategy.base_strategy import Signal
from risk.risk_manager import RiskManager
from utils.data_handler import DataHandler, OHLCVBar
from backtest.engine import Trade, BacktestResult


@dataclass
class Market:
    name: str
    bars: List[OHLCVBar]
    tick_size: float
    tick_value: float
    strategy_factory: Callable     # () -> BaseStrategy
    commission_per_contract: float = 0.0


class _MarketState:
    def __init__(self, m: Market, equity: float, risk_pct: float,
                 usd_krw: float, slippage_ticks: int):
        self.m = m
        self.strategy = m.strategy_factory()
        self.data = DataHandler(m.name, max_bars=500)
        self.risk = RiskManager(account_equity_krw=equity, tick_size=m.tick_size,
                                tick_value=m.tick_value, risk_per_trade_pct=risk_pct,
                                max_contracts_per_trade=10, usd_krw_rate=usd_krw)
        self.slippage = slippage_ticks * m.tick_size
        self.side: Optional[str] = None
        self.qty = 0
        self.entry_fill = 0.0
        self.entry_time = None
        self.bars_by_date = {b.timestamp: b for b in m.bars}

    def unrealized_krw(self, usd_krw):
        if self.side is None:
            return 0.0
        close = self.data.latest_close()
        return self.risk.calc_pnl_usd(self.side, self.entry_fill, close, self.qty) * usd_krw


class PortfolioBacktester:
    def __init__(self, markets: List[Market], account_equity_krw: float = 250_000_000,
                 risk_per_trade_pct: float = 1.0, usd_krw_rate: float = 1350.0,
                 slippage_ticks: int = 1):
        self.markets = markets
        self.initial_equity = account_equity_krw
        self.risk_pct = risk_per_trade_pct
        self.usd_krw = usd_krw_rate
        self.slippage_ticks = slippage_ticks

    def run(self):
        states = [_MarketState(m, self.initial_equity, self.risk_pct,
                               self.usd_krw, self.slippage_ticks)
                  for m in self.markets]

        all_dates = sorted({b.timestamp for m in self.markets for b in m.bars})
        equity = self.initial_equity
        result = BacktestResult(self.initial_equity, equity)
        per_market_pnl = {m.name: 0.0 for m in self.markets}

        for d in all_dates:
            for st in states:
                bar = st.bars_by_date.get(d)
                if bar is None:
                    continue
                st.data.add_bar(bar)
                close = bar.close
                st.risk.update_equity(equity)
                signal = st.strategy.on_bar(st.data)

                if signal.signal in (Signal.LONG, Signal.SHORT) and st.side is None:
                    contracts = st.risk.calc_contracts(signal.entry_price, signal.stop_loss)
                    if contracts > 0:
                        st.side = "LONG" if signal.signal == Signal.LONG else "SHORT"
                        st.entry_fill = close + st.slippage if st.side == "LONG" else close - st.slippage
                        st.qty = contracts
                        st.entry_time = d
                    elif hasattr(st.strategy, "reset_position"):
                        st.strategy.reset_position()

                elif signal.signal == Signal.EXIT and st.side is not None:
                    exit_fill = close - st.slippage if st.side == "LONG" else close + st.slippage
                    gross = st.risk.calc_pnl_usd(st.side, st.entry_fill, exit_fill, st.qty)
                    pnl = (gross - st.m.commission_per_contract * st.qty) * self.usd_krw
                    equity += pnl
                    per_market_pnl[st.m.name] += pnl
                    result.trades.append(Trade(st.side, st.entry_time, d,
                                               st.entry_fill, exit_fill, st.qty, pnl,
                                               f"[{st.m.name}] {signal.reason}"))
                    st.side, st.qty, st.entry_fill, st.entry_time = None, 0, 0.0, None

            # 일일 자산 = 실현자산 + 전 시장 미실현
            mtm = equity + sum(st.unrealized_krw(self.usd_krw) for st in states)
            result.equity_curve.append(mtm)
            result.timestamps.append(d)

        # 종료 시 미청산 강제 청산
        for st in states:
            if st.side is not None:
                close = st.data.latest_close()
                exit_fill = close - st.slippage if st.side == "LONG" else close + st.slippage
                gross = st.risk.calc_pnl_usd(st.side, st.entry_fill, exit_fill, st.qty)
                pnl = (gross - st.m.commission_per_contract * st.qty) * self.usd_krw
                equity += pnl
                per_market_pnl[st.m.name] += pnl
                result.trades.append(Trade(st.side, st.entry_time, st.data.bars[-1].timestamp,
                                           st.entry_fill, exit_fill, st.qty, pnl,
                                           f"[{st.m.name}] 종료 강제청산"))

        result.final_equity_krw = equity
        result.per_market_pnl = per_market_pnl
        return result
