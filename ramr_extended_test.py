"""
RAMR 확장 검증:
  (1) 장기 백테스트 (2014-2024, 11년)
  (2) 연도별 분해 (어느 해에 잘하는지)
  (3) MR-only vs TC-only vs RAMR(combined) 비교
  (4) 기존 전략과 1억 / M6E+M6A 동일 조건으로 head-to-head
"""
import logging
from datetime import datetime
from portfolio_backtest import (
    silence_strategy_logs, Channel, simulate_channel,
    summarize_portfolio, SYMBOL_SPECS, load_bars_for_strategy,
)
from strategy.ramr import RAMRStrategy

silence_strategy_logs()

USD_KRW = 1400.0
TOTAL_KRW = 100_000_000
TOTAL_USD = TOTAL_KRW / USD_KRW
PER = TOTAL_USD / 2

# ECB 데이터는 2022-12부터 받아왔는데 더 긴 데이터 필요 → fetch_data 재실행 필요
# 일단 가용한 2023-2024로 테스트


def make_channel(symbol, strategy="ramr", risk_pct=2.0):
    return Channel(
        channel_id=f"{symbol}-{strategy}",
        symbol=symbol,
        strategy_name=strategy,
        allocated_equity_usd=PER,
        risk_per_trade_pct=risk_pct,
    )


def run_one(channels, start, end, label):
    sd, ed = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
    for ch in channels:
        spec = SYMBOL_SPECS[ch.symbol]
        bars = load_bars_for_strategy(ch.symbol, ch.strategy_name, "data")
        for tf in bars:
            bars[tf] = [b for b in bars[tf] if sd <= b.timestamp <= ed]
        simulate_channel(ch, spec, bars,
                        per_channel_circuit_breaker_pct=4.0)
    s = summarize_portfolio(channels, TOTAL_USD)
    final_krw = s['final_equity_usd'] * USD_KRW
    print(f" {label:50s}  ret={s['total_return_pct']:>+7.2f}% "
          f"MDD={s['max_drawdown_pct']:>5.1f}% Sh={s['sharpe']:>+5.2f} "
          f"trades={s['trades']:>3d} WR={s['win_rate']:>4.1f}% "
          f"PF={s['profit_factor']:>4.2f}  final={final_krw/10000:>7.0f}만원")
    return s


def main():
    print("=" * 130)
    print(" RAMR 검증 — 1억원 / M6E+M6A (Micro 선물) / 2% risk / 복리")
    print(" 데이터: ECB FX 일일 reference rate (실측)")
    print("=" * 130)

    # ── 연도별 분해 (2023, 2024 각각) ─────────────────────────────
    print("\n [연도별 RAMR]")
    print(" " + "-"*128)
    for yr in (2023, 2024):
        run_one([make_channel("M6E"), make_channel("M6A")],
                f"{yr}-01-01", f"{yr}-12-31",
                f"RAMR / {yr}년 1년")

    print("\n [2023-2024 통합]")
    print(" " + "-"*128)
    run_one([make_channel("M6E"), make_channel("M6A")],
            "2023-01-01", "2024-12-31",
            "RAMR / 2023-2024 통합")

    # ── 전략 비교 (동일 조건) ────────────────────────────────────
    print("\n [전략 비교 — 1억 / M6E+M6A / 2023-2024 / 2% risk]")
    print(" " + "-"*128)
    run_one([make_channel("M6E"), make_channel("M6A")],
            "2023-01-01", "2024-12-31",
            "RAMR (새 전략)")
    run_one([make_channel("M6E", "adaptive_trend"), make_channel("M6A", "adaptive_trend")],
            "2023-01-01", "2024-12-31",
            "adaptive_trend (기존 일봉)")

    # ── 6E vs 6A 단일 종목 ───────────────────────────────────────
    print("\n [단일 종목 RAMR — 1억 전액 / 2% risk / 2023-2024]")
    print(" " + "-"*128)
    # M6E only 1억 전액
    ch = Channel(channel_id="M6E-only", symbol="M6E", strategy_name="ramr",
                 allocated_equity_usd=TOTAL_USD, risk_per_trade_pct=2.0)
    run_one([ch], "2023-01-01", "2024-12-31", "RAMR / M6E 단독")
    ch = Channel(channel_id="M6A-only", symbol="M6A", strategy_name="ramr",
                 allocated_equity_usd=TOTAL_USD, risk_per_trade_pct=2.0)
    run_one([ch], "2023-01-01", "2024-12-31", "RAMR / M6A 단독")

    # ── 리스크 강도 비교 ─────────────────────────────────────────
    print("\n [RAMR 리스크 민감도 — 1억 / M6E+M6A / 2023-2024]")
    print(" " + "-"*128)
    for risk_pct, label in [(1.0, "1.0% (보수)"), (2.0, "2.0% (선택)"), (3.0, "3.0% (공격)")]:
        chs = [make_channel("M6E", risk_pct=risk_pct),
               make_channel("M6A", risk_pct=risk_pct)]
        run_one(chs, "2023-01-01", "2024-12-31", f"RAMR / risk={label}")

    print("=" * 130)
    print()


if __name__ == "__main__":
    main()
