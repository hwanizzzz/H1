"""
RAMR v2 — 파라미터 튜닝 & 10년 검증

조정 검토:
  v1 baseline: BB 2.0σ + RSI(2) 10/90, ADX < 20 RANGE, ADX > 25 TREND
  v2 loose   : BB 1.8σ + RSI(2) 15/85, RANGE 더 자주
  v3 MR-only : TC 모드 비활성, 평균회귀만 (M6E TC 손실 회피)
  v4 wide    : MR 보유 7봉, ADX < 22

기간:
  2014-2024 (11년) 전체 + 2014-2019, 2020-2024 분할
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


# Patched strategy builder allowing parameter overrides
def build_ramr_variant(**kwargs):
    return RAMRStrategy(**kwargs)


# Override portfolio_backtest.build_strategy temporarily by injecting
# a custom strategy instance into the channel BEFORE simulate.
def simulate_with_variant(ch, spec, bars_per_tf, variant_kwargs, per_cb=4.0):
    """simulate_channel과 동일하지만 RAMR 파라미터 변형 사용."""
    import portfolio_backtest as pb
    from strategy.base_strategy import Signal
    from utils.data_handler import DataHandler, TIMEFRAME_SECONDS
    from datetime import timedelta

    strat = build_ramr_variant(**variant_kwargs)
    required_tfs = ["1D"]
    smallest_tf = "1D"

    data_dict = {"1D": DataHandler(ch.symbol, max_bars=400, timeframe="1D")}
    smallest_bars = bars_per_tf["1D"]
    risk_pct = ch.risk_per_trade_pct
    ch.equity_usd = ch.allocated_equity_usd
    ch.peak_equity = ch.equity_usd

    SLIPPAGE_TICKS = 1
    COMMISSION = 5.0
    from portfolio_backtest import Trade

    for sb in smallest_bars:
        data_dict["1D"].add_bar(sb)

        cur_day = sb.timestamp.date()
        if ch.last_day is not None and cur_day != ch.last_day:
            ch.daily_pnl = 0.0
        ch.last_day = cur_day

        if ch.halt_until and sb.timestamp < ch.halt_until:
            ch.equity_curve.append((sb.timestamp, ch.equity_usd))
            continue
        if data_dict["1D"].bar_count() < strat.get_min_bars_required():
            ch.equity_curve.append((sb.timestamp, ch.equity_usd))
            continue

        signal = strat.on_bar(data_dict)
        slip = SLIPPAGE_TICKS * spec.tick_size

        if ch.open_trade is None and signal.signal in (Signal.LONG, Signal.SHORT):
            entry = sb.close + slip if signal.signal == Signal.LONG else sb.close - slip
            stop_dist = abs(entry - signal.stop_loss)
            if stop_dist < spec.tick_size:
                ch.equity_curve.append((sb.timestamp, ch.equity_usd))
                continue
            risk_usd = ch.equity_usd * (risk_pct / 100.0)
            ticks = stop_dist / spec.tick_size
            loss_per_contract = ticks * spec.tick_value
            contracts = max(int(risk_usd / loss_per_contract), 0)
            if contracts == 0:
                ch.equity_curve.append((sb.timestamp, ch.equity_usd))
                continue
            ch.open_trade = Trade(
                channel_id=ch.channel_id, symbol=ch.symbol,
                side="LONG" if signal.signal == Signal.LONG else "SHORT",
                entry_date=sb.timestamp, entry_price=entry,
                contracts=contracts, reason=signal.reason, units=1,
            )
        elif ch.open_trade is not None and signal.signal == Signal.PARTIAL_TP:
            close_qty = max(1, int(ch.open_trade.contracts * signal.partial_close_pct))
            if close_qty < ch.open_trade.contracts:
                exit_p = sb.close - slip if ch.open_trade.side == "LONG" else sb.close + slip
                diff = (exit_p - ch.open_trade.entry_price) if ch.open_trade.side == "LONG" \
                       else (ch.open_trade.entry_price - exit_p)
                ticks = diff / spec.tick_size
                pnl = ticks * spec.tick_value * close_qty - 2 * COMMISSION * close_qty
                ch.equity_usd += pnl
                ch.daily_pnl += pnl
                ch.open_trade.contracts -= close_qty
                ch.open_trade.pnl_usd += pnl
                ch.peak_equity = max(ch.peak_equity, ch.equity_usd)
                dd = (ch.peak_equity - ch.equity_usd) / ch.peak_equity if ch.peak_equity > 0 else 0
                ch.max_dd = max(ch.max_dd, dd)
        elif ch.open_trade is not None and signal.signal == Signal.EXIT:
            exit_p = sb.close - slip if ch.open_trade.side == "LONG" else sb.close + slip
            diff = (exit_p - ch.open_trade.entry_price) if ch.open_trade.side == "LONG" \
                   else (ch.open_trade.entry_price - exit_p)
            ticks = diff / spec.tick_size
            pnl = ticks * spec.tick_value * ch.open_trade.contracts \
                  - 2 * COMMISSION * ch.open_trade.contracts
            ch.open_trade.exit_date = sb.timestamp
            ch.open_trade.exit_price = exit_p
            ch.open_trade.pnl_usd += pnl
            ch.trades.append(ch.open_trade)
            ch.equity_usd += pnl
            ch.daily_pnl += pnl
            if ch.open_trade.pnl_usd < 0:
                ch.consec_losses += 1
            else:
                ch.consec_losses = 0
            ch.peak_equity = max(ch.peak_equity, ch.equity_usd)
            dd = (ch.peak_equity - ch.equity_usd) / ch.peak_equity if ch.peak_equity > 0 else 0
            ch.max_dd = max(ch.max_dd, dd)
            ch.open_trade = None

        if ch.daily_pnl < -ch.equity_usd * (per_cb / 100.0):
            ch.halt_until = sb.timestamp + timedelta(hours=24)
            if ch.open_trade is not None:
                exit_p = sb.close
                diff = (exit_p - ch.open_trade.entry_price) if ch.open_trade.side == "LONG" \
                       else (ch.open_trade.entry_price - exit_p)
                pnl = (diff / spec.tick_size) * spec.tick_value * ch.open_trade.contracts
                ch.open_trade.exit_date = sb.timestamp
                ch.open_trade.exit_price = exit_p
                ch.open_trade.pnl_usd += pnl
                ch.trades.append(ch.open_trade)
                ch.equity_usd += pnl
                ch.open_trade = None
        if ch.consec_losses >= 5:
            ch.halt_until = sb.timestamp + timedelta(hours=24)
            ch.consec_losses = 0

        ch.equity_curve.append((sb.timestamp, ch.equity_usd))


def make_ch(symbol, risk=2.0):
    return Channel(channel_id=f"{symbol}-RAMR", symbol=symbol,
                   strategy_name="ramr",
                   allocated_equity_usd=PER, risk_per_trade_pct=risk)


def run(variant_kwargs, start, end, label):
    sd, ed = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
    channels = [make_ch("M6E"), make_ch("M6A")]
    for ch in channels:
        spec = SYMBOL_SPECS[ch.symbol]
        bars = load_bars_for_strategy(ch.symbol, "ramr", "data")
        for tf in bars:
            bars[tf] = [b for b in bars[tf] if sd <= b.timestamp <= ed]
        simulate_with_variant(ch, spec, bars, variant_kwargs)
    s = summarize_portfolio(channels, TOTAL_USD)
    final_krw = s['final_equity_usd'] * USD_KRW
    yrs = (ed - sd).days / 365.25
    print(f"  {label:48s}  CAGR={s['cagr_pct']:>+6.2f}% "
          f"ret={s['total_return_pct']:>+7.2f}% MDD={s['max_drawdown_pct']:>5.1f}% "
          f"Sh={s['sharpe']:>+5.2f} trades={s['trades']:>4d}({s['trades']/yrs:>4.1f}/yr) "
          f"WR={s['win_rate']:>4.1f}% PF={s['profit_factor']:>4.2f}  "
          f"final={final_krw/10000:>7.0f}만원")
    return s


def main():
    print("=" * 140)
    print(" RAMR v2 — 파라미터 튜닝 + 장기 검증 (10년)")
    print(" 1억원 / M6E+M6A / 2% risk / 복리 / 실측 ECB 일봉")
    print("=" * 140)

    variants = {
        "v7 BB2.0 RSI10/90": dict(mr_with_trend_only=True, tc_enabled=True),
        "v12 BB1.5 RSI15/85": dict(mr_with_trend_only=True, tc_enabled=True,
                                    bb_stddev=1.5, mr_rsi_long_max=15, mr_rsi_short_min=85),
        "v13 BB1.5 RSI20/80": dict(mr_with_trend_only=True, tc_enabled=True,
                                    bb_stddev=1.5, mr_rsi_long_max=20, mr_rsi_short_min=80),
        "v14 BB1.2 RSI15/85": dict(mr_with_trend_only=True, tc_enabled=True,
                                    bb_stddev=1.2, mr_rsi_long_max=15, mr_rsi_short_min=85),
        "v15 BB1.5 RSI25/75 hold7": dict(mr_with_trend_only=True, tc_enabled=True,
                                          bb_stddev=1.5, mr_rsi_long_max=25, mr_rsi_short_min=75,
                                          mr_max_bars=7),
    }

    # 2023-2024 (이미 확인된 구간)
    print("\n [2023-2024]")
    print(" " + "-"*138)
    for label, kw in variants.items():
        run(kw, "2023-01-01", "2024-12-31", label)

    # 2020-2022 (변동성 큰 구간: 코로나 + 인플레이션)
    print("\n [2020-2022 (코로나·인플레)]")
    print(" " + "-"*138)
    for label, kw in variants.items():
        run(kw, "2020-01-01", "2022-12-31", label)

    # 2014-2019 (안정/하락 USD)
    print("\n [2014-2019]")
    print(" " + "-"*138)
    for label, kw in variants.items():
        run(kw, "2014-01-01", "2019-12-31", label)

    # 전체 11년 누적
    print("\n [2014-2024 전체 11년]")
    print(" " + "-"*138)
    for label, kw in variants.items():
        run(kw, "2014-01-01", "2024-12-31", label)

    print("=" * 140)


if __name__ == "__main__":
    main()
