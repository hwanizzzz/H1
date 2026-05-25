"""
백테스트 실행 CLI.

사용 예:
  # 실제 일봉 CSV로 Donchian(추세필터 포함) 백테스트
  python -m backtest.run --csv data/6A_daily.csv --strategy donchian

  # 합성 데이터로 엔진 동작 점검
  python -m backtest.run --synthetic --strategy donchian

  # 추세 필터 on/off 비교
  python -m backtest.run --synthetic --strategy donchian --trend 0
  python -m backtest.run --synthetic --strategy donchian --trend 100

config/config.yaml의 종목 스펙·전략 파라미터를 기본값으로 사용하며,
CLI 인자로 일부를 덮어쓸 수 있습니다.
"""

import argparse
import yaml

from strategy.donchian_breakout import DonchianBreakoutStrategy
from strategy.ma_crossover import MACrossoverStrategy
from backtest.engine import Backtester, format_report
from backtest.data_loader import load_csv, generate_synthetic


def build_strategy(name: str, strat_cfg: dict, trend_override):
    if name == "donchian":
        trend = strat_cfg.get("trend_filter_period", 0) if trend_override is None else trend_override
        return DonchianBreakoutStrategy(
            entry_period=strat_cfg.get("donchian_entry_period", 20),
            exit_period=strat_cfg.get("donchian_exit_period", 10),
            atr_period=strat_cfg.get("atr_period", 14),
            atr_multiplier=strat_cfg.get("atr_multiplier", 2.0),
            trend_filter_period=trend,
        )
    if name == "ma":
        return MACrossoverStrategy()
    raise ValueError(f"알 수 없는 전략: {name}")


def main():
    p = argparse.ArgumentParser(description="해외선물 전략 백테스트")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="일봉 CSV 경로")
    src.add_argument("--synthetic", action="store_true", help="합성 데이터로 엔진 점검")
    p.add_argument("--strategy", default="donchian", choices=["donchian", "ma"])
    p.add_argument("--trend", type=int, default=None,
                   help="추세 필터 SMA 기간 덮어쓰기 (0=비활성)")
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--bars", type=int, default=750, help="합성 데이터 봉 수")
    p.add_argument("--seed", type=int, default=42, help="합성 데이터 시드")
    p.add_argument("--tick-size", type=float, default=None,
                   help="종목 틱 사이즈 덮어쓰기 (예: M6A=0.0001)")
    p.add_argument("--tick-value", type=float, default=None,
                   help="틱당 가치 USD 덮어쓰기 (예: M6A=1.0, 6A=10.0, MES=1.25)")
    args = p.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    sym = cfg["symbol"]
    risk = cfg["risk"]
    strat_cfg = cfg["strategy"]

    tick_size = args.tick_size if args.tick_size is not None else sym["tick_size"]
    tick_value = args.tick_value if args.tick_value is not None else sym["tick_value"]

    if args.csv:
        bars = load_csv(args.csv)
        data_label = args.csv
    else:
        bars = generate_synthetic(n=args.bars, start_price=0.6500,
                                  tick_size=tick_size, seed=args.seed)
        data_label = f"합성데이터({args.bars}봉, seed={args.seed})"

    strategy = build_strategy(args.strategy, strat_cfg, args.trend)

    bt = Backtester(
        strategy=strategy,
        tick_size=tick_size,
        tick_value=tick_value,
        account_equity_krw=risk["account_equity"],
        risk_per_trade_pct=risk["risk_per_trade_pct"],
        usd_krw_rate=risk.get("usd_krw_rate", 1350.0),
        slippage_ticks=cfg.get("execution", {}).get("slippage_ticks", 1),
        max_contracts_per_trade=risk["max_contracts_per_trade"],
        max_bars=len(bars) + 10,
    )
    result = bt.run(bars)

    trend_label = (strategy.trend_filter_period
                   if hasattr(strategy, "trend_filter_period") else "-")
    title = (f"{strategy.name} | 종목 {sym['code']} | {data_label} "
             f"| 추세필터={trend_label}")
    print(format_report(result, title))


if __name__ == "__main__":
    main()
