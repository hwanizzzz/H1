"""
RAMR 백테스트 — 1억원 자본 / M6E + M6A 2종목 / 2% 리스크 / 복리 재투자

설정:
  총 자본: 1억원 (USD/KRW=1400 → $71,428)
  종목 배분: M6E 5,000만원 + M6A 5,000만원 (각 $35,714)
  Micro 선물: tick value M6E=$1.25, M6A=$1.00 (standard의 1/10)
  리스크: 2% per trade
  안전망: 일일 -4% 자동 정지, 5연패 24h 정지
  복리: 매 거래 후 equity 갱신 → 다음 사이즈 자동 확대

평가 구간:
  2023-01-01 ~ 2024-12-31 (24개월)
  데이터: ECB 공시 일일 reference rate (실측)
"""
import logging
import sys
from datetime import datetime
from portfolio_backtest import (
    silence_strategy_logs, Channel, simulate_channel,
    summarize_portfolio, SYMBOL_SPECS, load_bars_for_strategy,
)

silence_strategy_logs()

USD_KRW = 1400.0
TOTAL_KRW = 100_000_000     # 1억원
TOTAL_USD = TOTAL_KRW / USD_KRW
PER_SYMBOL_USD = TOTAL_USD / 2

START, END = "2023-01-01", "2024-12-31"


def make_channel(symbol, risk_pct=2.0, daily_cb=4.0):
    return Channel(
        channel_id=f"{symbol}-RAMR",
        symbol=symbol,
        strategy_name="ramr",
        allocated_equity_usd=PER_SYMBOL_USD,
        risk_per_trade_pct=risk_pct,
    )


def show_channel_detail(ch, spec):
    print(f"\n[{ch.channel_id}] (allocated ${ch.allocated_equity_usd:,.0f})")
    n = len(ch.trades)
    pnl = ch.equity_usd - ch.allocated_equity_usd
    pnl_pct = pnl / ch.allocated_equity_usd * 100
    wins = [t for t in ch.trades if t.pnl_usd > 0]
    losses = [t for t in ch.trades if t.pnl_usd <= 0]
    wr = len(wins) / n * 100 if n else 0
    win_sum = sum(t.pnl_usd for t in wins)
    loss_sum = abs(sum(t.pnl_usd for t in losses))
    pf = win_sum / loss_sum if loss_sum > 0 else float('inf')
    avg_win = win_sum / len(wins) if wins else 0
    avg_loss = -loss_sum / len(losses) if losses else 0
    expectancy = win_sum / n + (-loss_sum) / n if n else 0

    print(f"  Trades        : {n}  ({n/24:.1f}/month over 24 months)")
    print(f"  Win rate      : {wr:.1f}%   ({len(wins)}W / {len(losses)}L)")
    print(f"  Profit factor : {pf:.2f}")
    print(f"  Avg win       : ${avg_win:+,.0f}")
    print(f"  Avg loss      : ${avg_loss:+,.0f}")
    print(f"  Expectancy    : ${expectancy:+,.0f} / trade")
    print(f"  PnL           : ${pnl:+,.0f} ({pnl_pct:+.1f}%)")
    print(f"  Max DD        : {ch.max_dd*100:.1f}%")
    print(f"  Final equity  : ${ch.equity_usd:,.0f}")

    # Sample first 5 and last 5 trades
    if n > 0:
        print(f"  First 3 trades:")
        for t in ch.trades[:3]:
            print(f"    {t.entry_date:%Y-%m-%d} {t.side:5s} entry={t.entry_price:.5f} "
                  f"exit={t.exit_price:.5f} pnl=${t.pnl_usd:+,.0f}  {t.reason[:60]}")
        if n > 3:
            print(f"  Last 3 trades:")
            for t in ch.trades[-3:]:
                print(f"    {t.entry_date:%Y-%m-%d} {t.side:5s} entry={t.entry_price:.5f} "
                      f"exit={t.exit_price:.5f} pnl=${t.pnl_usd:+,.0f}  {t.reason[:60]}")


def run(channels, label):
    sd = datetime.strptime(START, "%Y-%m-%d")
    ed = datetime.strptime(END, "%Y-%m-%d")
    for ch in channels:
        spec = SYMBOL_SPECS[ch.symbol]
        bars_per_tf = load_bars_for_strategy(ch.symbol, ch.strategy_name, "data")
        for tf in bars_per_tf:
            bars_per_tf[tf] = [b for b in bars_per_tf[tf] if sd <= b.timestamp <= ed]
        # aggressive circuit breakers
        simulate_channel(ch, spec, bars_per_tf,
                        global_circuit_breaker_pct=8.0,
                        per_channel_circuit_breaker_pct=4.0)
        show_channel_detail(ch, spec)

    s = summarize_portfolio(channels, TOTAL_USD)
    krw_final = s['final_equity_usd'] * USD_KRW
    print()
    print("=" * 70)
    print(f" PORTFOLIO TOTAL — {label}")
    print("=" * 70)
    print(f" Initial equity   : ${s['initial_equity_usd']:>10,.0f}   ({TOTAL_KRW/10000:,.0f}만원)")
    print(f" Final equity     : ${s['final_equity_usd']:>10,.0f}   ({krw_final/10000:,.0f}만원)")
    print(f" Total return     : {s['total_return_pct']:>+9.2f}%")
    print(f" CAGR             : {s['cagr_pct']:>+9.2f}%")
    print(f" Max drawdown     : {s['max_drawdown_pct']:>10.2f}%")
    print(f" Sharpe (daily)   : {s['sharpe']:>+10.2f}")
    print(f" Total trades     : {s['trades']:>10d}  ({s['trades_per_month']:.1f}/month)")
    print(f" Win rate         : {s['win_rate']:>10.1f}%")
    print(f" Profit factor    : {s['profit_factor']:>10.2f}")
    print("=" * 70)
    return s


def main():
    print("=" * 70)
    print(" RAMR — Regime-Adaptive Mean Reversion")
    print(" 2023-2024 백테스트 (1억원 / M6E + M6A / 2% risk / 복리)")
    print("=" * 70)
    print(f" Total capital  : {TOTAL_KRW/10000:,.0f}만원 (≈ ${TOTAL_USD:,.0f})")
    print(f" Per-symbol     : ${PER_SYMBOL_USD:,.0f}  ({PER_SYMBOL_USD * USD_KRW/10000:,.0f}만원)")
    print(f" Strategy       : RAMR (regime-switched MR + TC)")
    print(f" Risk/trade     : 2.0%   (compounding)")
    print(f" Daily limit    : -4.0%   ⇒ 24h halt")
    print(f" Period         : {START} ~ {END}")

    channels = [
        make_channel("M6E"),
        make_channel("M6A"),
    ]
    run(channels, "RAMR / M6E+M6A")

    print()
    print(" ✓ 데이터 신뢰도: 실측 ECB FX 일봉 (외부 공시 reference rate)")
    print(" ✓ 복리 적용: 매 거래 후 equity 갱신 → 다음 사이즈 = current_equity × 2%")
    print(" ✓ Micro 선물 사용: M6E(tick $1.25), M6A(tick $1.00)")


if __name__ == "__main__":
    main()
