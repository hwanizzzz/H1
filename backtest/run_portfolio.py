"""
개별 시장 vs 분산 포트폴리오 비교 백테스트 (일봉, 추세추종).

사용:
  python -m backtest.run_portfolio

마이크로 계약 스펙으로 사이징하여 2.5억 계좌에서 거래가 성립하도록 함.
(표준계약만 가능하면 자본을 더 키우거나 시장 수를 줄여야 함)
"""

import os
from strategy.donchian_breakout import DonchianBreakoutStrategy
from backtest.data_loader import load_csv
from backtest.engine import Backtester
from backtest.portfolio import PortfolioBacktester, Market

EQUITY = 250_000_000
RISK_PCT = 1.0
USD_KRW = 1350.0

def strat():
    return DonchianBreakoutStrategy(entry_period=20, exit_period=10,
                                    atr_period=14, atr_multiplier=2.0,
                                    trend_filter_period=100)

# (이름, CSV, 틱사이즈, 틱가치USD[마이크로], 왕복수수료USD)
MARKET_DEFS = [
    ("금(MGC)",      "data/XAUUSD_1D.csv", 0.10,   1.0,  5.0),
    ("S&P500(MES)",  "data/SPXUSD_1D.csv", 0.25,   1.25, 3.0),
    ("호주달러(M6A)", "data/AUDUSD_1D.csv", 0.0001, 1.0,  3.0),
    ("원유(MCL)",     "data/WTIUSD_1D.csv", 0.01,   1.0,  3.0),
]


def fmt(r, name):
    return (f" {name:<16} 거래{r.num_trades:>4}  "
            f"수익률{r.total_return_pct:>+7.1f}%  MDD{r.max_drawdown_pct:>5.1f}%  "
            f"승률{r.win_rate:>4.0f}%  PF{r.profit_factor:>5.2f}  샤프{r.sharpe:>5.2f}")


def main():
    markets = []
    print("=" * 78)
    print(" 개별 시장 성과 (일봉 / Donchian 추세필터100 / 리스크 1% / 마이크로계약)")
    print("=" * 78)
    for name, csv, ts, tv, comm in MARKET_DEFS:
        if not os.path.exists(csv):
            print(f" {name:<16} (데이터 없음: {csv} - 건너뜀)")
            continue
        bars = load_csv(csv)
        bt = Backtester(strat(), ts, tv, account_equity_krw=EQUITY,
                        risk_per_trade_pct=RISK_PCT, usd_krw_rate=USD_KRW,
                        slippage_ticks=1, commission_per_contract=comm,
                        max_contracts_per_trade=10, max_bars=500)
        r = bt.run(bars)
        print(fmt(r, name))
        markets.append(Market(name, bars, ts, tv, strat, comm))

    if len(markets) < 2:
        print("\n포트폴리오 비교에는 2개 이상 시장이 필요합니다.")
        return

    print("-" * 78)
    pb = PortfolioBacktester(markets, account_equity_krw=EQUITY,
                             risk_per_trade_pct=RISK_PCT, usd_krw_rate=USD_KRW,
                             slippage_ticks=1)
    pr = pb.run()
    print(fmt(pr, f"★ 분산({len(markets)}종)"))
    print("-" * 78)
    print(" [분산 포트폴리오 시장별 손익 기여]")
    for name, pnl in pr.per_market_pnl.items():
        print(f"   {name:<16} {pnl/10000:>+10,.0f}만원")
    print("=" * 78)
    print(" ※ 분산의 핵심: 합산 시 수익률은 평균 이상, MDD는 개별보다 작고 샤프는 높아지는지 확인.")


if __name__ == "__main__":
    main()
