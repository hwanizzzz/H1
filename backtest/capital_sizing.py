"""
표준 CME 계약 기준 최소 운용자본 분석.

핵심 원리:
  1회 거래 손실 = 2×ATR(손절폭) × tick_value × 계약수 (USD)
  리스크 룰: 1회 손실 ≤ 계좌자산 × risk_pct
  → 1계약을 잡으려면  계좌자산(USD) ≥ (2×ATR/tick_size) × tick_value / risk_pct

이 스크립트는 데이터의 대표 ATR로 종목별 1계약 손절 리스크를 구하고,
risk_pct별 최소 자본을 계산한 뒤, 자본 구간별로 백테스트를 돌려 검증합니다.

사용 예:
  python -m backtest.capital_sizing                       # 합성 6A 표준계약
  python -m backtest.capital_sizing --csv data/6A.csv
  python -m backtest.capital_sizing --tick-value 50 --tick-size 0.25  # ES (S&P500 E-mini)
"""

import argparse
import numpy as np

from strategy.donchian_breakout import DonchianBreakoutStrategy
from backtest.engine import Backtester
from backtest.data_loader import load_csv, generate_synthetic
from utils.data_handler import DataHandler


USD_KRW = 1350.0


def representative_atr(bars, atr_period=14, atr_mult=2.0):
    """데이터 전 구간의 중앙값 ATR과 그에 따른 2×ATR 손절폭(가격)을 반환."""
    dh = DataHandler("CALC", max_bars=len(bars) + 10)
    atrs = []
    for b in bars:
        dh.add_bar(b)
        a = dh.atr(atr_period)
        if a:
            atrs.append(a)
    median_atr = float(np.median(atrs)) if atrs else 0.0
    return median_atr, median_atr * atr_mult


def min_capital_krw(stop_distance, tick_size, tick_value, risk_pct, usd_krw=USD_KRW):
    """1계약 진입에 필요한 최소 자본(원화)."""
    stop_ticks = stop_distance / tick_size
    loss_per_contract_usd = stop_ticks * tick_value
    min_equity_usd = loss_per_contract_usd / (risk_pct / 100.0)
    return min_equity_usd * usd_krw, loss_per_contract_usd


def main():
    p = argparse.ArgumentParser(description="표준계약 최소 운용자본 분석")
    p.add_argument("--csv", help="일봉 CSV (없으면 합성 데이터)")
    p.add_argument("--tick-size", type=float, default=0.0001, help="틱 사이즈 (6A=0.0001)")
    p.add_argument("--tick-value", type=float, default=10.0, help="틱당 USD (6A=10)")
    p.add_argument("--atr-mult", type=float, default=2.0)
    p.add_argument("--bars", type=int, default=750)
    args = p.parse_args()

    if args.csv:
        bars = load_csv(args.csv)
        label = args.csv
    else:
        bars = generate_synthetic(n=args.bars, start_price=0.6500, tick_size=args.tick_size)
        label = f"합성({args.bars}봉)"

    median_atr, stop_dist = representative_atr(bars, atr_mult=args.atr_mult)
    stop_ticks = stop_dist / args.tick_size

    print("=" * 60)
    print(f" 최소 운용자본 분석 | {label}")
    print(f" 틱={args.tick_size} 틱가치=${args.tick_value} 환율={USD_KRW:.0f}원")
    print("=" * 60)
    print(f" 대표 ATR(중앙값)        : {median_atr:.5f}")
    print(f" 손절폭(2×ATR)           : {stop_dist:.5f} ({stop_ticks:.0f}틱)")
    print("-" * 60)
    print(" [1계약 진입에 필요한 최소 자본]")
    for rp in (1.0, 2.0, 3.0):
        cap_krw, loss_usd = min_capital_krw(stop_dist, args.tick_size, args.tick_value, rp)
        print(f"   리스크 {rp:.0f}%/거래 → 1계약 손실 ${loss_usd:,.0f} "
              f"→ 최소자본 {cap_krw/10000:,.0f}만원")
    print("-" * 60)

    # 자본 구간별 백테스트 검증 (리스크 2% 가정)
    print(" [자본 구간별 백테스트] (Donchian 추세필터100, 리스크 2%/거래)")
    print(f"   {'자본':>10} {'거래수':>6} {'수익률':>9} {'MDD':>7} {'PF':>6}")
    for cap_man in (5000, 10000, 15000, 20000, 30000):
        strat = DonchianBreakoutStrategy(trend_filter_period=100)
        bt = Backtester(strat, args.tick_size, args.tick_value,
                        account_equity_krw=cap_man * 10000,
                        risk_per_trade_pct=2.0, usd_krw_rate=USD_KRW,
                        slippage_ticks=1, max_contracts_per_trade=10,
                        max_bars=len(bars) + 10)
        r = bt.run(bars)
        pf = r.profit_factor
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"   {cap_man:>8,}만 {r.num_trades:>6} {r.total_return_pct:>+8.2f}% "
              f"{r.max_drawdown_pct:>6.2f}% {pf_s:>6}")
    print("=" * 60)
    print(" ※ 합성데이터 수치는 엔진/자본규모 검증용이며 실성과 근거가 아닙니다.")
    print("   실제 일봉 CSV(--csv)로 재실행하세요.")


if __name__ == "__main__":
    main()
