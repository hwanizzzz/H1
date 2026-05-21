"""
RAMR 최종 검증 — 단일 종목 집중 + 최적 변형 선택

목표:
  - M6A 단독 vs M6E 단독 vs 2종목 균등 비교
  - 각각 v1(counter-trend allowed)과 v7(with-trend filter) 비교
  - 연도별 분해로 안정성 평가
"""
import logging
from datetime import datetime
from portfolio_backtest import (
    silence_strategy_logs, Channel, summarize_portfolio,
    SYMBOL_SPECS, load_bars_for_strategy,
)
from ramr_v2_test import simulate_with_variant

silence_strategy_logs()

USD_KRW = 1400.0
TOTAL_KRW = 100_000_000
TOTAL_USD = TOTAL_KRW / USD_KRW


def make_ch(symbol, equity_usd, risk=2.0):
    return Channel(channel_id=f"{symbol}", symbol=symbol,
                   strategy_name="ramr",
                   allocated_equity_usd=equity_usd, risk_per_trade_pct=risk)


def run(channels, variant_kwargs, start, end, label):
    sd, ed = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
    for ch in channels:
        spec = SYMBOL_SPECS[ch.symbol]
        bars = load_bars_for_strategy(ch.symbol, "ramr", "data")
        for tf in bars:
            bars[tf] = [b for b in bars[tf] if sd <= b.timestamp <= ed]
        simulate_with_variant(ch, spec, bars, variant_kwargs)
    s = summarize_portfolio(channels, TOTAL_USD)
    final = s['final_equity_usd'] * USD_KRW
    yrs = (ed - sd).days / 365.25
    print(f"  {label:55s}  CAGR={s['cagr_pct']:>+6.2f}% "
          f"ret={s['total_return_pct']:>+8.2f}% MDD={s['max_drawdown_pct']:>5.1f}% "
          f"Sh={s['sharpe']:>+5.2f} trades={s['trades']:>3d}({s['trades']/yrs:>4.1f}/yr) "
          f"WR={s['win_rate']:>4.1f}% PF={s['profit_factor']:>4.2f}  "
          f"final={final/10000:>6.0f}만원")
    return s


def main():
    print("=" * 145)
    print(" RAMR 최종 검증 — 종목/변형/기간 비교")
    print(" 1억원 / 2% risk / 복리 / 실측 ECB 일봉")
    print("=" * 145)

    # variants
    V1 = dict(mr_with_trend_only=False, tc_enabled=True)
    V7 = dict(mr_with_trend_only=True, tc_enabled=True)
    V12 = dict(mr_with_trend_only=True, tc_enabled=True,
                bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85)

    configs = [
        ("2종목 M6E+M6A 균등", lambda: [make_ch("M6E", TOTAL_USD/2), make_ch("M6A", TOTAL_USD/2)]),
        ("M6A 단독", lambda: [make_ch("M6A", TOTAL_USD)]),
        ("M6E 단독", lambda: [make_ch("M6E", TOTAL_USD)]),
    ]

    # ── 11년 누적 (v12) ───────────────────────────────────────
    print("\n [11년 누적 검증 (2014-2024) — v12 최선 변형]")
    print(" " + "-"*143)
    for cfg_label, mk in configs:
        run(mk(), V12, "2014-01-01", "2024-12-31", f"v12 / {cfg_label}")

    # ── 2023-2024 (v12) ───────────────────────────────────────
    print("\n [2023-2024 (v12)]")
    print(" " + "-"*143)
    for cfg_label, mk in configs:
        run(mk(), V12, "2023-01-01", "2024-12-31", f"v12 / {cfg_label}")

    # ── 연도별 (2종목 / v12) ─────────────────────────────────
    print("\n [2종목 M6E+M6A / v12 연도별 분해 (2014-2024)]")
    print(" " + "-"*143)
    for yr in range(2014, 2025):
        run([make_ch("M6E", TOTAL_USD/2), make_ch("M6A", TOTAL_USD/2)],
            V12, f"{yr}-01-01", f"{yr}-12-31", f"{yr}년")

    print("=" * 145)


if __name__ == "__main__":
    main()
