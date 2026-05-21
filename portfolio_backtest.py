"""
포트폴리오 백테스트 — 4종목(6A/6E/6B/6C) × 2전략 = 최대 8채널 동시 시뮬레이션

각 채널을 독립적인 미니 시뮬레이션으로 돌린 뒤, 일자별 equity 곡선을 합산해
통합 포트폴리오 성과를 산출합니다.

데이터:
  - 일봉 (`adaptive_trend`): ECB 일일 reference rate (실측)
  - 1H봉 (`multi_tf_aggressive`): 일봉에서 합성 (Brownian bridge)
    -> 결과는 보조 지표로만 활용. 실제 intraday breakout 빈도/순서를 재현하지 못함.

종목 스펙: CME 공식 tick size / tick value
"""
import argparse
import csv
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Tuple

from strategy.base_strategy import Signal
from strategy.adaptive_trend import AdaptiveTrendStrategy
from strategy.multi_tf_aggressive import MultiTFAggressiveStrategy
from utils.data_handler import DataHandler, OHLCVBar, TIMEFRAME_SECONDS


# ── 종목 스펙 ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SymbolSpec:
    code: str
    name: str
    tick_size: float
    tick_value: float

SYMBOL_SPECS: Dict[str, SymbolSpec] = {
    "6A": SymbolSpec("6A", "AUD/USD", 0.0001,  10.00),
    "6E": SymbolSpec("6E", "EUR/USD", 0.00005,  6.25),
    "6B": SymbolSpec("6B", "GBP/USD", 0.0001,   6.25),
    "6C": SymbolSpec("6C", "CAD/USD", 0.0001,  10.00),
}


# ── 백테스트 파라미터 ─────────────────────────────────────────────────────────

SLIPPAGE_TICKS = 1
COMMISSION_USD_PER_CONTRACT = 5.0
PYRAMID_DECAY = [1.0, 0.75, 0.5, 0.5]
USD_KRW_RATE = 1400.0


@dataclass
class Trade:
    channel_id: str
    symbol: str
    side: str
    entry_date: datetime
    entry_price: float
    exit_date: Optional[datetime] = None
    exit_price: float = 0.0
    contracts: int = 0
    pnl_usd: float = 0.0
    reason: str = ""
    units: int = 1


@dataclass
class Channel:
    channel_id: str
    symbol: str
    strategy_name: str
    allocated_equity_usd: float
    risk_per_trade_pct: float
    input_tf: str = "1D"
    required_tfs: List[str] = field(default_factory=lambda: ["1D"])
    # mutable state during sim
    equity_usd: float = 0.0
    peak_equity: float = 0.0
    max_dd: float = 0.0
    open_trade: Optional[Trade] = None
    risk_unit_usd: float = 0.0
    base_contracts: int = 0
    units_held: int = 0
    daily_pnl: float = 0.0
    last_day: Optional[date] = None
    halt_until: Optional[datetime] = None
    consec_losses: int = 0
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[Tuple[datetime, float]] = field(default_factory=list)


def build_strategy(name: str):
    if name == "adaptive_trend":
        s = AdaptiveTrendStrategy()
        return s, ["1D"], "1D"
    if name == "multi_tf_aggressive":
        s = MultiTFAggressiveStrategy()
        return s, ["4H", "1H"], "1H"
    raise ValueError(f"unknown strategy: {name}")


# ── CSV 로더 (기존 backtest.py와 호환) ───────────────────────────────────────

def _parse_date(s: str) -> datetime:
    s = s.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"date parse failed: {s}")


def load_csv(path: str) -> List[OHLCVBar]:
    bars = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = _parse_date(row.get("date") or row.get("Date"))
            bars.append(OHLCVBar(
                timestamp=ts,
                open_=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row.get("volume", 0)),
            ))
    bars.sort(key=lambda b: b.timestamp)
    return bars


def aggregate_bars(bars: List[OHLCVBar], target_tf: str) -> List[OHLCVBar]:
    if not bars:
        return []
    target_seconds = TIMEFRAME_SECONDS[target_tf.upper()]
    out, bucket, bucket_open_epoch = [], None, -1
    for b in bars:
        epoch = int(b.timestamp.timestamp())
        bucket_epoch = (epoch // target_seconds) * target_seconds
        if bucket is None or bucket_epoch != bucket_open_epoch:
            if bucket is not None:
                out.append(bucket)
            bucket = OHLCVBar(
                timestamp=datetime.utcfromtimestamp(bucket_epoch),
                open_=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume,
            )
            bucket_open_epoch = bucket_epoch
        else:
            bucket.high = max(bucket.high, b.high)
            bucket.low = min(bucket.low, b.low)
            bucket.close = b.close
            bucket.volume += b.volume
    if bucket is not None:
        out.append(bucket)
    return out


# ── 단일 채널 시뮬레이션 ──────────────────────────────────────────────────────

def simulate_channel(
    ch: Channel,
    spec: SymbolSpec,
    bars_per_tf: Dict[str, List[OHLCVBar]],
    global_circuit_breaker_pct: float = 5.0,
    per_channel_circuit_breaker_pct: float = 8.0,
):
    """채널 단위 시뮬레이션. 결과는 ch.* 에 누적."""
    strat, required_tfs, input_tf = build_strategy(ch.strategy_name)
    ch.required_tfs = required_tfs

    # DataHandler per TF — cap window for O(n) indicator speed
    data_dict: Dict[str, DataHandler] = {}
    window_cap = {"1D": 400, "4H": 600, "1H": 600}
    for tf in required_tfs:
        cap = window_cap.get(tf.upper(), 500)
        data_dict[tf] = DataHandler(ch.symbol, max_bars=cap, timeframe=tf)

    main_tf = required_tfs[0]
    smallest_tf = min(required_tfs, key=lambda t: TIMEFRAME_SECONDS[t.upper()])
    smallest_bars = bars_per_tf[smallest_tf]

    tf_indices = {tf: 0 for tf in required_tfs}
    ch.equity_usd = ch.allocated_equity_usd
    ch.peak_equity = ch.equity_usd
    risk_pct = ch.risk_per_trade_pct

    for sb in smallest_bars:
        data_dict[smallest_tf].add_bar(sb)
        # higher TFs catch up
        for tf in required_tfs:
            if tf == smallest_tf:
                continue
            tfb = bars_per_tf[tf]
            while tf_indices[tf] < len(tfb) and tfb[tf_indices[tf]].timestamp <= sb.timestamp:
                if tf_indices[tf] == 0 or tfb[tf_indices[tf]].timestamp != tfb[tf_indices[tf] - 1].timestamp:
                    data_dict[tf].add_bar(tfb[tf_indices[tf]])
                tf_indices[tf] += 1

        cur_day = sb.timestamp.date()
        if ch.last_day is not None and cur_day != ch.last_day:
            ch.daily_pnl = 0.0
        ch.last_day = cur_day

        if ch.halt_until and sb.timestamp < ch.halt_until:
            ch.equity_curve.append((sb.timestamp, ch.equity_usd))
            continue
        if data_dict[main_tf].bar_count() < strat.get_min_bars_required():
            ch.equity_curve.append((sb.timestamp, ch.equity_usd))
            continue

        signal = strat.on_bar(data_dict)
        slip = SLIPPAGE_TICKS * spec.tick_size

        # ── New entry ──
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
            ch.risk_unit_usd = loss_per_contract * contracts
            ch.base_contracts = contracts
            ch.units_held = 1

        # ── Pyramid add ──
        elif ch.open_trade is not None and signal.signal == Signal.SCALE_IN:
            if ch.units_held < len(PYRAMID_DECAY):
                ch.units_held += 1
                add_qty = max(1, int(ch.base_contracts * PYRAMID_DECAY[ch.units_held - 1]))
                add_price = sb.close + slip if ch.open_trade.side == "LONG" else sb.close - slip
                old_total = ch.open_trade.contracts
                new_total = old_total + add_qty
                ch.open_trade.entry_price = (
                    ch.open_trade.entry_price * old_total + add_price * add_qty
                ) / new_total
                ch.open_trade.contracts = new_total
                ch.open_trade.units = ch.units_held

        # ── Partial TP ──
        elif ch.open_trade is not None and signal.signal == Signal.PARTIAL_TP:
            close_qty = max(1, int(ch.open_trade.contracts * signal.partial_close_pct))
            if close_qty < ch.open_trade.contracts:
                exit_price = sb.close - slip if ch.open_trade.side == "LONG" else sb.close + slip
                diff = (exit_price - ch.open_trade.entry_price) if ch.open_trade.side == "LONG" \
                       else (ch.open_trade.entry_price - exit_price)
                ticks = diff / spec.tick_size
                pnl = (ticks * spec.tick_value * close_qty
                       - 2 * COMMISSION_USD_PER_CONTRACT * close_qty)
                ch.equity_usd += pnl
                ch.daily_pnl += pnl
                ch.open_trade.contracts -= close_qty
                ch.open_trade.pnl_usd += pnl
                ch.peak_equity = max(ch.peak_equity, ch.equity_usd)
                dd = (ch.peak_equity - ch.equity_usd) / ch.peak_equity if ch.peak_equity > 0 else 0
                ch.max_dd = max(ch.max_dd, dd)

        # ── Full exit ──
        elif ch.open_trade is not None and signal.signal == Signal.EXIT:
            exit_price = sb.close - slip if ch.open_trade.side == "LONG" else sb.close + slip
            diff = (exit_price - ch.open_trade.entry_price) if ch.open_trade.side == "LONG" \
                   else (ch.open_trade.entry_price - exit_price)
            ticks = diff / spec.tick_size
            pnl = (ticks * spec.tick_value * ch.open_trade.contracts
                   - 2 * COMMISSION_USD_PER_CONTRACT * ch.open_trade.contracts)
            ch.open_trade.exit_date = sb.timestamp
            ch.open_trade.exit_price = exit_price
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
            ch.units_held = 0
            ch.base_contracts = 0

        # ── Channel-level circuit breaker ──
        if ch.daily_pnl < -ch.equity_usd * (per_channel_circuit_breaker_pct / 100.0):
            ch.halt_until = sb.timestamp + timedelta(hours=24)
            if ch.open_trade is not None:
                exit_price = sb.close
                diff = (exit_price - ch.open_trade.entry_price) if ch.open_trade.side == "LONG" \
                       else (ch.open_trade.entry_price - exit_price)
                pnl = (diff / spec.tick_size) * spec.tick_value * ch.open_trade.contracts
                ch.open_trade.exit_date = sb.timestamp
                ch.open_trade.exit_price = exit_price
                ch.open_trade.pnl_usd += pnl
                ch.open_trade.reason += " | channel CB"
                ch.trades.append(ch.open_trade)
                ch.equity_usd += pnl
                ch.open_trade = None
        if ch.consec_losses >= 7:
            ch.halt_until = sb.timestamp + timedelta(hours=24)
            ch.consec_losses = 0

        ch.equity_curve.append((sb.timestamp, ch.equity_usd))


# ── 포트폴리오 합산 ───────────────────────────────────────────────────────────

def merge_equity_curves(channels: List[Channel], initial_total: float) -> List[Tuple[datetime, float]]:
    """채널별 equity 곡선을 같은 시간축에 정렬하여 합산.
    각 채널이 자체 시간축을 가질 수 있으므로 'last-known' 보간 사용.
    """
    all_ts = set()
    for ch in channels:
        for ts, _ in ch.equity_curve:
            all_ts.add(ts)
    sorted_ts = sorted(all_ts)

    # 각 채널의 시작 자산 = allocated_equity_usd
    last_eq = {ch.channel_id: ch.allocated_equity_usd for ch in channels}
    idx = {ch.channel_id: 0 for ch in channels}
    combined = []
    for ts in sorted_ts:
        for ch in channels:
            while idx[ch.channel_id] < len(ch.equity_curve) and ch.equity_curve[idx[ch.channel_id]][0] <= ts:
                last_eq[ch.channel_id] = ch.equity_curve[idx[ch.channel_id]][1]
                idx[ch.channel_id] += 1
        total = sum(last_eq.values())
        combined.append((ts, total))
    return combined


def summarize_portfolio(channels: List[Channel], initial_total_usd: float):
    combined = merge_equity_curves(channels, initial_total_usd)
    if not combined:
        return None
    final_equity = combined[-1][1]
    total_return = (final_equity / initial_total_usd - 1) * 100

    # MDD on combined curve
    peak = initial_total_usd
    max_dd = 0.0
    for _, eq in combined:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # CAGR
    days = (combined[-1][0] - combined[0][0]).days
    years = days / 365.25 if days > 0 else 1
    cagr = ((final_equity / initial_total_usd) ** (1 / years) - 1) * 100 if years > 0 else 0

    # Sharpe (daily returns on combined curve, sampled at end-of-day)
    by_day: Dict[date, float] = {}
    for ts, eq in combined:
        by_day[ts.date()] = eq
    sorted_days = sorted(by_day.keys())
    rets = []
    prev = initial_total_usd
    for d in sorted_days:
        eq = by_day[d]
        rets.append((eq - prev) / prev if prev > 0 else 0)
        prev = eq
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        std = math.sqrt(var)
        sharpe = (mean / std * math.sqrt(252)) if std > 0 else 0
    else:
        sharpe = 0

    # Trades aggregate
    all_trades = [t for ch in channels for t in ch.trades]
    wins = [t for t in all_trades if t.pnl_usd > 0]
    losses = [t for t in all_trades if t.pnl_usd <= 0]
    win_rate = len(wins) / len(all_trades) * 100 if all_trades else 0
    profit_factor = (
        sum(t.pnl_usd for t in wins) / abs(sum(t.pnl_usd for t in losses))
        if losses and sum(t.pnl_usd for t in losses) != 0 else float("inf")
    )
    months = max(1, days / 30.4)
    trades_per_month = len(all_trades) / months

    return {
        "initial_equity_usd": initial_total_usd,
        "final_equity_usd": final_equity,
        "total_return_pct": total_return,
        "cagr_pct": cagr,
        "max_drawdown_pct": max_dd * 100,
        "sharpe": sharpe,
        "trades": len(all_trades),
        "trades_per_month": trades_per_month,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "wins": len(wins),
        "losses": len(losses),
        "days": days,
        "combined_curve": combined,
    }


# ── 채널 빌더 ────────────────────────────────────────────────────────────────

def build_default_channels(total_equity_usd: float,
                           symbols=("6A", "6E", "6B", "6C"),
                           strategies=("multi_tf_aggressive", "adaptive_trend"),
                           ) -> List[Channel]:
    n = len(symbols) * len(strategies)
    if n == 0:
        return []
    per = total_equity_usd / n
    channels = []
    risk_map = {"multi_tf_aggressive": 3.0, "adaptive_trend": 2.0}
    for sym in symbols:
        for strat in strategies:
            channels.append(Channel(
                channel_id=f"{sym}-{strat}",
                symbol=sym,
                strategy_name=strat,
                allocated_equity_usd=per,
                risk_per_trade_pct=risk_map.get(strat, 1.0),
            ))
    return channels


def load_bars_for_strategy(symbol: str, strategy_name: str, data_dir: str) -> Dict[str, List[OHLCVBar]]:
    """전략이 요구하는 모든 TF에 대해 봉 데이터 로드 (필요 시 합성)."""
    _, required_tfs, input_tf = build_strategy(strategy_name)
    if input_tf == "1D":
        path = os.path.join(data_dir, f"{symbol}_daily.csv")
    elif input_tf == "1H":
        path = os.path.join(data_dir, f"{symbol}_1h.csv")
    else:
        raise ValueError(f"unsupported input_tf {input_tf}")

    bars = load_csv(path)
    input_seconds = TIMEFRAME_SECONDS[input_tf]
    out = {}
    for tf in required_tfs:
        tf_seconds = TIMEFRAME_SECONDS[tf.upper()]
        if tf_seconds == input_seconds:
            out[tf] = bars
        elif tf_seconds > input_seconds:
            out[tf] = aggregate_bars(bars, tf)
        else:
            raise ValueError(f"{tf} smaller than input {input_tf}")
    return out


# ── 메인 ─────────────────────────────────────────────────────────────────────

def silence_strategy_logs():
    """Set strategy/util logger levels to WARNING after they've been initialized."""
    for name in ("multi_tf_aggressive", "adaptive_trend", "data_handler",
                 "risk_manager", "donchian_breakout", "ma_crossover"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)
        for h in lg.handlers:
            h.setLevel(logging.WARNING)


def main():
    silence_strategy_logs()
    parser = argparse.ArgumentParser()
    parser.add_argument("--equity-krw", type=float, default=150_000_000,
                        help="총 자본 (원)")
    parser.add_argument("--usd-krw", type=float, default=USD_KRW_RATE)
    parser.add_argument("--symbols", default="6A,6E,6B,6C")
    parser.add_argument("--strategies", default="multi_tf_aggressive,adaptive_trend")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--start", default=None, help="YYYY-MM-DD (선택)")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD (선택)")
    args = parser.parse_args()

    total_equity_usd = args.equity_krw / args.usd_krw
    symbols = tuple(args.symbols.split(","))
    strategies = tuple(args.strategies.split(","))
    channels = build_default_channels(total_equity_usd, symbols, strategies)

    print("=" * 70)
    print(f" PORTFOLIO BACKTEST")
    print(f"  total equity : {args.equity_krw/10000:,.0f}만원 (≈ ${total_equity_usd:,.0f})")
    print(f"  symbols      : {symbols}")
    print(f"  strategies   : {strategies}")
    print(f"  channels     : {len(channels)} (allocation per channel ≈ ${total_equity_usd/len(channels):,.0f})")
    print("=" * 70)

    print()
    print(f" {'CHANNEL':32s} {'TRADES':>7s} {'WIN%':>6s} {'PnL$':>10s} {'PnL%':>7s} {'MDD%':>6s} {'PF':>5s}")
    print(f" {'-'*32} {'-'*7} {'-'*6} {'-'*10} {'-'*7} {'-'*6} {'-'*5}")

    for ch in channels:
        spec = SYMBOL_SPECS[ch.symbol]
        bars_per_tf = load_bars_for_strategy(ch.symbol, ch.strategy_name, args.data_dir)
        if args.start or args.end:
            start_dt = datetime.strptime(args.start, "%Y-%m-%d") if args.start else datetime.min
            end_dt = datetime.strptime(args.end, "%Y-%m-%d") if args.end else datetime.max
            for tf in bars_per_tf:
                bars_per_tf[tf] = [b for b in bars_per_tf[tf] if start_dt <= b.timestamp <= end_dt]
        simulate_channel(ch, spec, bars_per_tf)
        n_trades = len(ch.trades)
        pnl = ch.equity_usd - ch.allocated_equity_usd
        pnl_pct = pnl / ch.allocated_equity_usd * 100
        wins = [t for t in ch.trades if t.pnl_usd > 0]
        losses = [t for t in ch.trades if t.pnl_usd <= 0]
        wr = len(wins) / n_trades * 100 if n_trades else 0
        win_sum = sum(t.pnl_usd for t in wins)
        loss_sum = abs(sum(t.pnl_usd for t in losses))
        pf = win_sum / loss_sum if loss_sum > 0 else float('inf')
        pf_str = f"{pf:.2f}" if pf != float('inf') else "INF"
        print(f" {ch.channel_id:32s} {n_trades:>7d} {wr:>5.1f}% ${pnl:>+8,.0f} {pnl_pct:>+6.1f}% {ch.max_dd*100:>5.1f}% {pf_str:>5s}")

    summary = summarize_portfolio(channels, total_equity_usd)
    print()
    print("=" * 70)
    print(" PORTFOLIO SUMMARY")
    print("=" * 70)
    print(f" 초기 자산     : ${summary['initial_equity_usd']:>12,.0f}  ({args.equity_krw/10000:,.0f}만원)")
    print(f" 최종 자산     : ${summary['final_equity_usd']:>12,.0f}  ({summary['final_equity_usd']*args.usd_krw/10000:,.0f}만원)")
    print(f" 총 수익률     : {summary['total_return_pct']:>+11.1f}%")
    print(f" CAGR          : {summary['cagr_pct']:>+11.1f}%")
    print(f" 최대 낙폭     : {summary['max_drawdown_pct']:>11.1f}%")
    print(f" Sharpe        : {summary['sharpe']:>11.2f}")
    print(f" 총 거래       : {summary['trades']:>11d}  (월 {summary['trades_per_month']:.1f}회)")
    print(f" 승률          : {summary['win_rate']:>11.1f}%")
    print(f" Profit Factor : {summary['profit_factor']:>11.2f}")
    print("=" * 70)

    return summary, channels


if __name__ == "__main__":
    main()
