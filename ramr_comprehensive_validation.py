"""
RAMR 종합 검증 패키지

(A) Walk-forward OOS — 2014-2019 훈련 / 2020-2024 테스트
(B) 실거래 스트레스 — 수수료/슬리피지/누락 시나리오
(C) Monte Carlo — 1000회 bootstrap 신뢰구간
(D) 구성요소 분해 — MR-only vs TC-only vs RAMR(combined)
(E) M6A 단독 수익률 상승 전략 탐색 (파라미터/오버레이)
(F) 1H/4H 합성 데이터 CAGR 추정 (헤비 주의)
"""
import logging
import math
import random
import sys
from datetime import datetime, timedelta
from copy import deepcopy
from portfolio_backtest import (
    silence_strategy_logs, Channel, summarize_portfolio,
    SYMBOL_SPECS, load_bars_for_strategy, aggregate_bars, TIMEFRAME_SECONDS,
    Trade, simulate_channel, SLIPPAGE_TICKS, COMMISSION_USD_PER_CONTRACT,
)
from ramr_v2_test import simulate_with_variant
from strategy.ramr import RAMRStrategy
from utils.data_handler import DataHandler

silence_strategy_logs()

USD_KRW = 1400.0
TOTAL_USD = 100_000_000 / USD_KRW   # 1억 1억원

V12 = dict(mr_with_trend_only=True, tc_enabled=True,
            bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85)


def make_ch(symbol, equity=TOTAL_USD, risk=2.0):
    return Channel(channel_id=symbol, symbol=symbol,
                   strategy_name="ramr",
                   allocated_equity_usd=equity, risk_per_trade_pct=risk)


def run(channels, variant, start, end, label, verbose=True):
    sd, ed = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
    for ch in channels:
        spec = SYMBOL_SPECS[ch.symbol]
        bars = load_bars_for_strategy(ch.symbol, "ramr", "data")
        for tf in bars:
            bars[tf] = [b for b in bars[tf] if sd <= b.timestamp <= ed]
        simulate_with_variant(ch, spec, bars, variant)
    s = summarize_portfolio(channels, sum(c.allocated_equity_usd for c in channels))
    if verbose:
        yrs = (ed - sd).days / 365.25
        print(f"  {label:50s}  CAGR={s['cagr_pct']:>+6.2f}% ret={s['total_return_pct']:>+8.2f}% "
              f"MDD={s['max_drawdown_pct']:>5.1f}% Sh={s['sharpe']:>+5.2f} "
              f"trades={s['trades']:>3d}({s['trades']/yrs:>4.1f}/yr) "
              f"WR={s['win_rate']:>4.1f}% PF={s['profit_factor']:>4.2f}")
    return s, channels


# ════════════════════════════════════════════════════════════════════
# (A) Walk-forward OOS
# ════════════════════════════════════════════════════════════════════

def walk_forward_oos():
    print("\n" + "=" * 90)
    print(" (A) Walk-Forward Out-of-Sample 검증")
    print("=" * 90)
    print("  훈련 구간(IS)에서 파라미터 선택 → 테스트 구간(OOS)에서 검증")
    print("  → IS 좋고 OOS 나쁘면 과적합")
    print()

    # 후보 파라미터 그리드 (간소화)
    grid = [
        dict(bb_stddev=2.0, mr_rsi_long_max=10, mr_rsi_short_min=90),
        dict(bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85),  # v12
        dict(bb_stddev=1.8, mr_rsi_long_max=15, mr_rsi_short_min=85),
        dict(bb_stddev=1.5, mr_rsi_long_max=20, mr_rsi_short_min=80),
    ]
    base = dict(mr_with_trend_only=True, tc_enabled=True)

    splits = [
        ("Split-1", "2014-01-01", "2019-12-31", "2020-01-01", "2024-12-31"),
        ("Split-2", "2014-01-01", "2021-12-31", "2022-01-01", "2024-12-31"),
        ("Split-3", "2014-01-01", "2017-12-31", "2018-01-01", "2024-12-31"),
    ]

    for name, is_start, is_end, oos_start, oos_end in splits:
        print(f"\n [{name}] IS={is_start[:7]}~{is_end[:7]}  OOS={oos_start[:7]}~{oos_end[:7]}")
        print(" " + "-"*88)

        # In-Sample: each candidate
        is_results = []
        for params in grid:
            variant = {**base, **params}
            s, _ = run([make_ch("M6A")], variant, is_start, is_end, "", verbose=False)
            is_results.append((params, s))

        # Find best by Sharpe
        best_params, best_is = max(is_results, key=lambda x: x[1]['sharpe'])
        print(f"  IS 최선: bb={best_params['bb_stddev']}σ RSI={best_params['mr_rsi_long_max']}/"
              f"{best_params['mr_rsi_short_min']} → IS CAGR={best_is['cagr_pct']:+.2f}% "
              f"Sh={best_is['sharpe']:+.2f}")

        # Apply on OOS
        variant = {**base, **best_params}
        s_oos, _ = run([make_ch("M6A")], variant, oos_start, oos_end,
                       f"OOS 결과 (IS최선 적용)", verbose=True)
        print(f"  → OOS 평가: CAGR {best_is['cagr_pct']:+.2f}% (IS) → {s_oos['cagr_pct']:+.2f}% (OOS)")


# ════════════════════════════════════════════════════════════════════
# (B) 실거래 스트레스
# ════════════════════════════════════════════════════════════════════

def stress_test():
    print("\n" + "=" * 90)
    print(" (B) 실거래 스트레스 테스트 — M6A 단독 v12 / 2014-2024 / 1억")
    print("=" * 90)
    print("  거래 비용·실행 누락의 영향을 정량 측정")
    print()

    import portfolio_backtest as pb

    scenarios = [
        ("기준 (수수료 $5/c, 슬리피지 1틱)", 5.0, 1, 0.0),
        ("수수료 2배 ($10/c)", 10.0, 1, 0.0),
        ("슬리피지 3배 (3틱)", 5.0, 3, 0.0),
        ("거래 10% 누락", 5.0, 1, 0.10),
        ("거래 20% 누락", 5.0, 1, 0.20),
        ("최악 시나리오 (수수료2x + 슬리피지3x + 20% 누락)", 10.0, 3, 0.20),
    ]
    print(" " + "-"*88)

    orig_commission = pb.COMMISSION_USD_PER_CONTRACT
    orig_slippage = pb.SLIPPAGE_TICKS

    for label, commission, slippage, miss_pct in scenarios:
        pb.COMMISSION_USD_PER_CONTRACT = commission
        pb.SLIPPAGE_TICKS = slippage
        # 누락 시뮬레이션: 시드 고정 후 진입 전 확률적 스킵
        random.seed(42)
        if miss_pct > 0:
            from strategy.ramr import RAMRStrategy as _R
            orig_on_bar = _R.on_bar
            def filtered_on_bar(self, data):
                sig = orig_on_bar(self, data)
                from strategy.base_strategy import Signal as S
                if sig.signal in (S.LONG, S.SHORT) and random.random() < miss_pct:
                    self._reset_position()
                    return type(sig)(S.NONE)
                return sig
            _R.on_bar = filtered_on_bar

        s, _ = run([make_ch("M6A")], V12, "2014-01-01", "2024-12-31", label, verbose=True)

        if miss_pct > 0:
            _R.on_bar = orig_on_bar

    pb.COMMISSION_USD_PER_CONTRACT = orig_commission
    pb.SLIPPAGE_TICKS = orig_slippage


# ════════════════════════════════════════════════════════════════════
# (C) Monte Carlo
# ════════════════════════════════════════════════════════════════════

def monte_carlo(iterations=1000):
    print("\n" + "=" * 90)
    print(" (C) Monte Carlo 재조합 — 1000회 bootstrap")
    print("=" * 90)
    print("  실거래 trade pnl 순서를 무작위로 재배열 → 결과 분포 산출")
    print("  → 평균 결과가 운인지 진짜 알파인지 신뢰구간으로 평가")
    print()

    # 한 번 실행해서 trade 리스트 확보
    s, channels = run([make_ch("M6A")], V12, "2014-01-01", "2024-12-31", "", verbose=False)
    trades = channels[0].trades
    pnls = [t.pnl_usd for t in trades]
    n = len(pnls)
    print(f"  실거래 표본 크기: {n} trades  실제 CAGR={s['cagr_pct']:+.2f}%  MDD={s['max_drawdown_pct']:.1f}%")

    # Bootstrap CAGRs
    initial = TOTAL_USD
    cagrs = []
    mdds = []
    final_eq = []

    rng = random.Random(42)
    for _ in range(iterations):
        eq = initial
        peak = initial
        max_dd = 0.0
        # 재조합: trades 무작위 순서로 (with replacement)
        shuffled = [rng.choice(pnls) for _ in range(n)]
        for pnl in shuffled:
            # 비례 스케일: 1억 기준 사이즈로 만들어진 pnl을 현재 자본 비례로 재산정
            scaled_pnl = pnl * (eq / initial)
            eq += scaled_pnl
            peak = max(peak, eq)
            dd = (peak - eq) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        final_eq.append(eq)
        cagrs.append(((eq / initial) ** (1/11) - 1) * 100)
        mdds.append(max_dd * 100)

    cagrs.sort()
    mdds.sort()
    final_eq.sort()
    p5, p25, p50, p75, p95 = [cagrs[int(iterations * p / 100)] for p in (5, 25, 50, 75, 95)]
    md_p5, md_p50, md_p95 = [mdds[int(iterations * p / 100)] for p in (5, 50, 95)]
    eq_p5, eq_p50, eq_p95 = [final_eq[int(iterations * p / 100)] for p in (5, 50, 95)]
    mean_cagr = sum(cagrs) / len(cagrs)

    print(f"  CAGR 분포:")
    print(f"    5%  : {p5:>+6.2f}%   (최악 5%)")
    print(f"    25% : {p25:>+6.2f}%")
    print(f"    50% : {p50:>+6.2f}%   (중간값)")
    print(f"    75% : {p75:>+6.2f}%")
    print(f"    95% : {p95:>+6.2f}%   (최선 5%)")
    print(f"    평균: {mean_cagr:>+6.2f}%")
    print(f"  MDD 분포:")
    print(f"    5%  : {md_p5:>6.2f}%   50%: {md_p50:>6.2f}%   95%: {md_p95:>6.2f}%")
    print(f"  11년 후 자산 분포 (시작 1억):")
    print(f"    5%  : {eq_p5*USD_KRW/10000:>6.0f}만원   "
          f"50%: {eq_p50*USD_KRW/10000:>6.0f}만원   "
          f"95%: {eq_p95*USD_KRW/10000:>6.0f}만원")

    # 양수 CAGR 확률
    pos_pct = sum(1 for c in cagrs if c > 0) / len(cagrs) * 100
    print(f"  양수 수익 확률 (1000회 중): {pos_pct:.1f}%")


# ════════════════════════════════════════════════════════════════════
# (D) 구성요소 분해
# ════════════════════════════════════════════════════════════════════

def component_decomposition():
    print("\n" + "=" * 90)
    print(" (D) 전략 구성요소 분해 — M6A / 2014-2024 / 1억")
    print("=" * 90)
    print()

    variants = [
        ("v12 RAMR (MR + TC, with-trend)", dict(mr_with_trend_only=True, tc_enabled=True,
                                                  bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85)),
        ("MR-only (TC off)", dict(mr_with_trend_only=True, tc_enabled=False,
                                   bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85)),
        ("TC-only (MR off via impossible threshold)",
         dict(mr_with_trend_only=True, tc_enabled=True,
              bb_stddev=10.0, mr_rsi_long_max=0, mr_rsi_short_min=100)),
        ("Counter-trend MR (no filter, TC on)",
         dict(mr_with_trend_only=False, tc_enabled=True,
              bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85)),
    ]

    print(" " + "-"*88)
    for label, params in variants:
        s, chs = run([make_ch("M6A")], params, "2014-01-01", "2024-12-31", label, verbose=True)
        # mode-wise breakdown
        if chs[0].trades:
            mr_trades = [t for t in chs[0].trades if "MR" in t.reason]
            tc_trades = [t for t in chs[0].trades if "TC" in t.reason]
            mr_pnl = sum(t.pnl_usd for t in mr_trades)
            tc_pnl = sum(t.pnl_usd for t in tc_trades)
            if mr_trades or tc_trades:
                print(f"      ↳ MR: {len(mr_trades)}거래 PnL=${mr_pnl:+,.0f}  | "
                      f"TC: {len(tc_trades)}거래 PnL=${tc_pnl:+,.0f}")


# ════════════════════════════════════════════════════════════════════
# (E) 수익률 상승 전략 탐색
# ════════════════════════════════════════════════════════════════════

def upside_strategies():
    print("\n" + "=" * 90)
    print(" (E) M6A 단독 수익률 상승 후보 탐색")
    print("=" * 90)
    print()

    candidates = [
        ("v12 baseline",
         dict(mr_with_trend_only=True, tc_enabled=True,
              bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85)),
        ("v12 + 짧은 익절(3봉)",
         dict(mr_with_trend_only=True, tc_enabled=True,
              bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85, mr_max_bars=3)),
        ("v12 + 긴 익절(10봉)",
         dict(mr_with_trend_only=True, tc_enabled=True,
              bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85, mr_max_bars=10)),
        ("v12 + 좁은 손절(1.0×ATR)",
         dict(mr_with_trend_only=True, tc_enabled=True,
              bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85, mr_stop_atr=1.0)),
        ("v12 + 넓은 손절(2.0×ATR)",
         dict(mr_with_trend_only=True, tc_enabled=True,
              bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85, mr_stop_atr=2.0)),
        ("v12 + 더 좁은 BB(1.2σ)",
         dict(mr_with_trend_only=True, tc_enabled=True,
              bb_stddev=1.2, mr_rsi_long_max=15, mr_rsi_short_min=85)),
        ("v12 + RSI(3) 사용",
         dict(mr_with_trend_only=True, tc_enabled=True,
              bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85, rsi_short_period=3)),
        ("v12 + ADX<25 (확대)",
         dict(mr_with_trend_only=True, tc_enabled=True,
              bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85, adx_range_max=25)),
        ("v12 + 더 엄격(RSI 10/90)",
         dict(mr_with_trend_only=True, tc_enabled=True,
              bb_stddev=1.5, mr_rsi_long_max=10, mr_rsi_short_min=90)),
    ]

    print(" " + "-"*88)
    for label, params in candidates:
        run([make_ch("M6A")], params, "2014-01-01", "2024-12-31", label, verbose=True)

    # 리스크 강화도 같이
    print()
    print(" [리스크 강화: 자본 1억 / v12 / risk% 증가]")
    print(" " + "-"*88)
    for risk in (1.0, 2.0, 3.0, 4.0, 5.0):
        ch = make_ch("M6A", TOTAL_USD, risk=risk)
        run([ch], V12, "2014-01-01", "2024-12-31", f"risk = {risk:.1f}%", verbose=True)


# ════════════════════════════════════════════════════════════════════
# (F) 1H/4H 합성 데이터 검증
# ════════════════════════════════════════════════════════════════════

def intraday_synthetic_estimate():
    print("\n" + "=" * 90)
    print(" (F) 1H/4H 합성 데이터 CAGR 추정 (⚠ 합성 데이터 캐비어트)")
    print("=" * 90)
    print(" 일봉 OHLC에서 Brownian bridge로 합성한 1H 봉 사용.")
    print(" 진짜 intraday 미세구조를 재현하지 못하므로 결과는 RANGE 추정용.")
    print(" 평균회귀는 노이즈가 많은 합성 데이터에서 오히려 false signal에 취약.")
    print(" → 진짜 1H 데이터 확보 후 재검증 필수.")
    print()

    # RAMR을 4H로 사용. 1H 데이터를 4H로 집계.
    # 문제: 현재 RAMR은 1D 전용. 4H/1H용으로 별도 simulate 필요.
    # 간소화: 1H 데이터 → 4H 집계 후 1D처럼 RAMR 실행. 그러면 1년치 == 6×252 = 1512 봉
    # 이는 일봉(252봉)의 6배. 거래 횟수 약 6배 가능.

    from utils.data_handler import DataHandler, OHLCVBar
    from datetime import timedelta as TD
    import portfolio_backtest as pb
    from strategy.base_strategy import Signal

    def run_4h_synthetic(start, end, label):
        bars_1h = load_bars_for_strategy("M6A", "multi_tf_aggressive", "data")["1H"]
        sd, ed = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
        bars_1h = [b for b in bars_1h if sd <= b.timestamp <= ed]
        # 4H 집계
        bars_4h = aggregate_bars(bars_1h, "4H")

        # simulate as 1D-equivalent
        ch = make_ch("M6A", TOTAL_USD)
        spec = SYMBOL_SPECS["M6A"]
        # 4H를 1D처럼 취급 (lazy approach)
        bars_per_tf = {"1D": bars_4h}
        simulate_with_variant(ch, spec, bars_per_tf, V12)
        s = summarize_portfolio([ch], TOTAL_USD)
        yrs = (ed - sd).days / 365.25
        print(f"  {label:40s}  CAGR={s['cagr_pct']:>+6.2f}% ret={s['total_return_pct']:>+8.2f}% "
              f"MDD={s['max_drawdown_pct']:>5.1f}% Sh={s['sharpe']:>+5.2f} "
              f"trades={s['trades']:>4d}({s['trades']/yrs:>5.1f}/yr) "
              f"WR={s['win_rate']:>4.1f}% PF={s['profit_factor']:>4.2f}")

    print(" [v12 RAMR을 합성 4H봉 1D-equiv로 처리]")
    print(" " + "-"*88)
    run_4h_synthetic("2014-01-01", "2024-12-31", "11년 누적 (4H 합성)")
    run_4h_synthetic("2020-01-01", "2024-12-31", "2020-2024 (4H 합성)")
    run_4h_synthetic("2023-01-01", "2024-12-31", "2023-2024 (4H 합성)")

    print()
    print(" ⚠ 위 결과는 일봉 → 1H Brownian bridge 합성 → 4H 집계 데이터 기반")
    print(" ⚠ 실제 CME 1H/4H 실측 데이터와 다를 가능성 매우 큼 (특히 거래 빈도)")
    print(" ⚠ CAGR 12% 기대치는 실측 데이터로 재검증 필요")


# ════════════════════════════════════════════════════════════════════

def main():
    print("=" * 90)
    print(" RAMR 종합 검증 패키지")
    print(" 1억원 / M6A 단독 / v12 / 2% risk / 복리")
    print("=" * 90)

    walk_forward_oos()
    stress_test()
    monte_carlo(iterations=1000)
    component_decomposition()
    upside_strategies()
    intraday_synthetic_estimate()

    print()
    print("=" * 90)
    print(" 종합 검증 완료")
    print("=" * 90)


if __name__ == "__main__":
    main()
