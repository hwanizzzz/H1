"""
2023-2024 백테스트 시나리오 비교

A. Tier 3 baseline (현행 운용): 6A 단독 / 3,000만원 / multi_tf_aggressive 3%
B. 1.5억 단일 6A / multi_tf_aggressive 3%
C. 1.5억 단일 6A / adaptive_trend 2% (일봉)
D. 1.5억 / 4종목 균등 / multi_tf_aggressive 만 (4채널) 3%
E. 1.5억 / 4종목 균등 / adaptive_trend 만 (4채널) 2%
F. 1.5억 / 4종목 × 2전략 = 8채널 (플랜 v2 원안)
G. 1.5억 / 4종목 × 2전략 (adaptive_trend 6%로 사이즈 강화)
"""
import logging
import sys
from portfolio_backtest import (
    silence_strategy_logs, build_default_channels, Channel,
    simulate_channel, summarize_portfolio, SYMBOL_SPECS,
    load_bars_for_strategy,
)
from datetime import datetime

silence_strategy_logs()

START, END = "2023-01-01", "2024-12-31"
USD_KRW = 1400.0

OUT_FILE = "scenarios_output.txt"
_out_buffer = []


def emit(line=""):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    _out_buffer.append(line)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(_out_buffer) + "\n")


def run(channels, total_usd, label):
    sd, ed = datetime.strptime(START, "%Y-%m-%d"), datetime.strptime(END, "%Y-%m-%d")
    for ch in channels:
        spec = SYMBOL_SPECS[ch.symbol]
        bars_per_tf = load_bars_for_strategy(ch.symbol, ch.strategy_name, "data")
        for tf in bars_per_tf:
            bars_per_tf[tf] = [b for b in bars_per_tf[tf] if sd <= b.timestamp <= ed]
        simulate_channel(ch, spec, bars_per_tf)
    s = summarize_portfolio(channels, total_usd)
    krw = s['final_equity_usd'] * USD_KRW
    emit(f" {label:55s}  ret={s['total_return_pct']:>+6.1f}% "
          f"MDD={s['max_drawdown_pct']:>5.1f}% Sh={s['sharpe']:>+5.2f} "
          f"trades={s['trades']:>3d} ({s['trades_per_month']:>4.1f}/mo) "
          f"WR={s['win_rate']:>4.1f}% PF={s['profit_factor']:>5.2f} "
          f"final={krw/10000:>7.0f}만원")
    return s


def make_channel(channel_id, symbol, strategy, equity_usd, risk_pct):
    return Channel(channel_id=channel_id, symbol=symbol,
                   strategy_name=strategy,
                   allocated_equity_usd=equity_usd,
                   risk_per_trade_pct=risk_pct)


def main():
    emit("=" * 130)
    emit(" 2023-01-01 ~ 2024-12-31 (730일, ~24개월) 백테스트 시나리오 비교 (USD/KRW=1400)")
    emit("=" * 130)
    emit(f" {'시나리오':55s}  {'수익률':>8s} {'MDD':>6s} {'Sharpe':>6s} {'거래(월간)':>14s} "
          f"{'승률':>5s} {'PF':>6s}  {'최종자산':>13s}")
    emit(" " + "-"*128)

    # A. Tier 3 baseline (3,000만원 / 6A 단독 / multi_tf_aggressive 3%)
    eq = 30_000_000 / USD_KRW
    run([make_channel("6A-aggr", "6A", "multi_tf_aggressive", eq, 3.0)], eq,
        "A. 3,000만원 / 6A 단독 / multi_tf_aggressive 3% (Tier 3 기준선)")

    # B. 1.5억 / 6A 단독 / multi_tf_aggressive 3%
    eq = 150_000_000 / USD_KRW
    run([make_channel("6A-aggr", "6A", "multi_tf_aggressive", eq, 3.0)], eq,
        "B. 1.5억 / 6A 단독 / multi_tf_aggressive 3%")

    # C. 1.5억 / 6A 단독 / adaptive_trend 2%
    eq = 150_000_000 / USD_KRW
    run([make_channel("6A-adapt", "6A", "adaptive_trend", eq, 2.0)], eq,
        "C. 1.5억 / 6A 단독 / adaptive_trend 2% (일봉)")

    # D. 1.5억 / 4종목 균등 / multi_tf_aggressive 3% (4채널)
    total = 150_000_000 / USD_KRW
    per = total / 4
    channels = [make_channel(f"{s}-aggr", s, "multi_tf_aggressive", per, 3.0)
                for s in ("6A", "6E", "6B", "6C")]
    run(channels, total,
        "D. 1.5억 / 4종목(6A6E6B6C) / multi_tf_aggressive 3% (4채널)")

    # E. 1.5억 / 4종목 균등 / adaptive_trend 2% (4채널)
    channels = [make_channel(f"{s}-adapt", s, "adaptive_trend", per, 2.0)
                for s in ("6A", "6E", "6B", "6C")]
    run(channels, total,
        "E. 1.5억 / 4종목 / adaptive_trend 2% (4채널)")

    # E2. adaptive_trend 6% (사이즈 강화)
    channels = [make_channel(f"{s}-adapt6", s, "adaptive_trend", per, 6.0)
                for s in ("6A", "6E", "6B", "6C")]
    run(channels, total,
        "E2. 1.5억 / 4종목 / adaptive_trend 6% (사이즈 강화)")

    # F. 1.5억 / 8채널 (multi_tf_aggressive 3% + adaptive_trend 2%) - 원안
    per = total / 8
    channels = []
    for s in ("6A", "6E", "6B", "6C"):
        channels.append(make_channel(f"{s}-aggr", s, "multi_tf_aggressive", per, 3.0))
        channels.append(make_channel(f"{s}-adapt", s, "adaptive_trend", per, 2.0))
    run(channels, total,
        "F. 1.5억 / 8채널 = 4종목 × 2전략 (aggr 3% + adapt 2%) 원안")

    # G. 1.5억 / 8채널 with adaptive_trend boosted to 6%
    channels = []
    for s in ("6A", "6E", "6B", "6C"):
        channels.append(make_channel(f"{s}-aggr", s, "multi_tf_aggressive", per, 3.0))
        channels.append(make_channel(f"{s}-adapt", s, "adaptive_trend", per, 6.0))
    run(channels, total,
        "G. 1.5억 / 8채널 (aggr 3% + adapt 6% 사이즈 강화)")

    emit("=" * 130)
    emit()
    emit(" ⚠ 데이터 신뢰도 안내:")
    emit("   - 일봉 (adaptive_trend): ECB 공시 일일 reference rate를 직접 사용 (실측)")
    emit("   - 1H봉 (multi_tf_aggressive): 일봉 OHLC로부터 Brownian bridge 합성")
    emit("     → 진짜 intraday breakout 빈도/순서를 재현하지 못함. 결과는 보조 지표.")
    emit()


if __name__ == "__main__":
    main()
