"""
자본 변경 + 종목 교체 시나리오 분석

(1) 자본 민감도 분석: 5천/1억/2억/5억에서 v12 RAMR 결과 (% 수익률은 동일,
    절대 금액과 사이징 정밀도가 달라짐)
(2) 종목 단독 비교: 6A/6E/6B/6C 모든 단일 종목 + 최선의 2-pair 조합
(3) 최적 조합 추천
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

V12 = dict(mr_with_trend_only=True, tc_enabled=True,
            bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85)


def make_ch(symbol, equity_usd, risk=2.0):
    return Channel(channel_id=symbol, symbol=symbol,
                   strategy_name="ramr",
                   allocated_equity_usd=equity_usd, risk_per_trade_pct=risk)


def run(channels, total_usd, start, end, label):
    sd, ed = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
    for ch in channels:
        spec = SYMBOL_SPECS[ch.symbol]
        bars = load_bars_for_strategy(ch.symbol, "ramr", "data")
        for tf in bars:
            bars[tf] = [b for b in bars[tf] if sd <= b.timestamp <= ed]
        simulate_with_variant(ch, spec, bars, V12)
    s = summarize_portfolio(channels, total_usd)
    final_krw = s['final_equity_usd'] * USD_KRW
    yrs = (ed - sd).days / 365.25
    print(f"  {label:55s}  CAGR={s['cagr_pct']:>+6.2f}% "
          f"ret={s['total_return_pct']:>+8.2f}% MDD={s['max_drawdown_pct']:>5.1f}% "
          f"Sh={s['sharpe']:>+5.2f} trades={s['trades']:>3d}({s['trades']/yrs:>4.1f}/yr) "
          f"WR={s['win_rate']:>4.1f}% PF={s['profit_factor']:>4.2f}  "
          f"final={final_krw/10000:>7.0f}만원")
    return s


def main():
    print("=" * 145)
    print(" RAMR v12 자본 + 종목 민감도 분석 (2014-2024 11년)")
    print(" 전략: with-trend MR + TC / BB1.5σ / RSI(2) 15/85 / 2% risk / 복리")
    print("=" * 145)

    # ── (1) 자본 민감도 (M6A 단독) ────────────────────────
    print("\n [자본 민감도 — M6A 단독 / v12]")
    print(" " + "-"*143)
    for krw in (30_000_000, 50_000_000, 100_000_000, 200_000_000, 500_000_000):
        usd = krw / USD_KRW
        ch = make_ch("M6A", usd)
        run([ch], usd, "2014-01-01", "2024-12-31",
            f"자본 {krw/10000:>5,.0f}만원 (${usd:,.0f})")

    # ── (2) 종목 단독 비교 (1억 / 11년) ──────────────────
    print("\n [종목 단독 비교 — 1억원 / v12 / 11년]")
    print(" " + "-"*143)
    TOTAL = 100_000_000 / USD_KRW
    for sym in ("M6A", "M6E", "M6B", "M6C"):
        ch = make_ch(sym, TOTAL)
        run([ch], TOTAL, "2014-01-01", "2024-12-31", f"{sym} ({SYMBOL_SPECS[sym].name}) 단독")

    # ── (3) 2-pair 조합 (1억 / 11년) ──────────────────────
    print("\n [2-pair 조합 — 1억원 균등 분할 / v12 / 11년]")
    print(" " + "-"*143)
    pairs = [
        ("M6A", "M6E"),
        ("M6A", "M6B"),
        ("M6A", "M6C"),
        ("M6E", "M6B"),
        ("M6E", "M6C"),
        ("M6B", "M6C"),
    ]
    for s1, s2 in pairs:
        chs = [make_ch(s1, TOTAL/2), make_ch(s2, TOTAL/2)]
        run(chs, TOTAL, "2014-01-01", "2024-12-31", f"{s1} + {s2}")

    # ── (4) 4-pair 전체 (참고) ───────────────────────────
    print("\n [4종목 균등 분할 (참고)]")
    print(" " + "-"*143)
    chs = [make_ch(s, TOTAL/4) for s in ("M6A", "M6E", "M6B", "M6C")]
    run(chs, TOTAL, "2014-01-01", "2024-12-31", "M6A+M6E+M6B+M6C 균등 4채널")

    # ── (5) 최선 조합: M6A + M6B (저상관 가정) 검증 ─────
    print("\n [최선 단일 조합 / 자본 변경 — M6A 단독 v12]")
    print(" " + "-"*143)
    for krw in (30_000_000, 100_000_000):
        usd = krw / USD_KRW
        run([make_ch("M6A", usd)], usd, "2023-01-01", "2024-12-31",
            f"M6A / 자본 {krw/10000:>5,.0f}만원 / 2023-2024")
        run([make_ch("M6A", usd)], usd, "2020-01-01", "2022-12-31",
            f"M6A / 자본 {krw/10000:>5,.0f}만원 / 2020-2022")

    print("=" * 145)


if __name__ == "__main__":
    main()
