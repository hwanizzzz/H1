"""
백테스트 실행 CLI

사용 예:
  # 합성 데이터로 3개 전략 비교 (엔진 동작 점검)
  python run_backtest.py

  # 실데이터 CSV 1종목 백테스트
  python run_backtest.py --csv data/MGC_1D.csv --symbol MGC \
      --tick-size 0.10 --tick-value 1.0

  # config.yaml 의 symbols 전체를 data/ 의 CSV로 검증
  python run_backtest.py --portfolio --data-dir data --timeframe 1D

CSV 형식: date,open,high,low,close,volume  (헤더 필수)
실데이터 파일명 규칙(포트폴리오 모드): {symbol}_{timeframe}.csv  예: MGC_1D.csv
"""

import argparse
import os
import sys
import yaml

from backtest.engine import BacktestEngine, BacktestResult
from backtest.data_loader import load_csv, generate_synthetic
from risk.risk_manager import SymbolSpec
from strategy.factory import create_strategy, AVAILABLE_STRATEGIES


def run_one(spec: SymbolSpec, bars, timeframe: str, args) -> list:
    results = []
    for strat_name in AVAILABLE_STRATEGIES:
        strategy = create_strategy({"name": strat_name})
        engine = BacktestEngine(
            spec=spec,
            timeframe=timeframe,
            commission_per_contract=args.commission,
            slippage_ticks=args.slippage,
            initial_equity_krw=args.equity,
            risk_per_trade_pct=args.risk,
            max_contracts_per_trade=args.max_contracts,
            usd_krw_rate=args.usd_krw,
        )
        results.append(engine.run(strategy, bars))
    return results


def print_table(results: list):
    print("\n" + "-" * 90)
    print(" 전략 비교")
    print("-" * 90)
    for r in results:
        print("  " + r.summary_row())
    print("-" * 90)


def main():
    p = argparse.ArgumentParser(description="해외선물 전략 백테스트")
    p.add_argument("--csv", help="단일 종목 CSV 경로")
    p.add_argument("--symbol", default="SYNTH", help="종목코드")
    p.add_argument("--tick-size", type=float, default=0.25, dest="tick_size")
    p.add_argument("--tick-value", type=float, default=1.25, dest="tick_value")
    p.add_argument("--timeframe", default="1D")
    p.add_argument("--portfolio", action="store_true",
                   help="config.yaml symbols 전체를 data-dir CSV로 검증")
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--data-dir", default="data", dest="data_dir")
    p.add_argument("--commission", type=float, default=2.5,
                   help="계약·편도당 수수료 USD")
    p.add_argument("--slippage", type=float, default=1.0, help="슬리피지(틱)")
    p.add_argument("--equity", type=float, default=10_000_000, help="초기자산(원)")
    p.add_argument("--risk", type=float, default=3.0, help="거래당 리스크 %%")
    p.add_argument("--max-contracts", type=int, default=5, dest="max_contracts")
    p.add_argument("--usd-krw", type=float, default=1350.0, dest="usd_krw")
    p.add_argument("--detail", action="store_true", help="최고 전략 상세 리포트 출력")
    args = p.parse_args()

    all_results = []

    if args.portfolio:
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        symbols = cfg.get("symbols")
        if not symbols:
            # 단일 symbol 설정도 허용
            s = cfg["symbol"]
            symbols = [s]
        for s in symbols:
            spec = SymbolSpec(s["code"], s["tick_size"], s["tick_value"],
                              s.get("name", ""))
            tf = s.get("timeframe", args.timeframe)
            path = os.path.join(args.data_dir, f"{spec.code}_{tf}.csv")
            if not os.path.exists(path):
                print(f"[건너뜀] 데이터 없음: {path}")
                continue
            bars = load_csv(path)
            print(f"\n### {spec.code} ({spec.name}) — {len(bars)}봉 [{tf}]")
            res = run_one(spec, bars, tf, args)
            print_table(res)
            all_results.extend(res)

    elif args.csv:
        spec = SymbolSpec(args.symbol, args.tick_size, args.tick_value)
        bars = load_csv(args.csv)
        print(f"### {spec.code} — {len(bars)}봉 [{args.timeframe}]")
        all_results = run_one(spec, bars, args.timeframe, args)
        print_table(all_results)

    else:
        print("실데이터 미지정 → 합성 데이터로 엔진 점검 실행")
        spec = SymbolSpec("SYNTH", args.tick_size, args.tick_value)
        bars = generate_synthetic(n=1500, tick_size=args.tick_size)
        print(f"### 합성 데이터 — {len(bars)}봉")
        all_results = run_one(spec, bars, "1D", args)
        print_table(all_results)

    if args.detail and all_results:
        best = max(all_results, key=lambda r: r.total_return_pct)
        print("\n" + best.report())

    return 0


if __name__ == "__main__":
    sys.exit(main())
