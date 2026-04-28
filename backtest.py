"""
백테스터 — CME 호주달러 선물(6A) 전략 검증 도구

지원 전략:
  - adaptive_trend       : 안정형 (일봉)
  - multi_tf_aggressive  : Tier 3 어그레시브 (4H + 1H 멀티TF + 피라미딩)
  - donchian_breakout    : 터틀 변형
  - ma_crossover         : MA 크로스오버

실행 방법:
  # 1) 일봉 CSV로 단일 TF 전략 백테스트
  python backtest.py --csv data/6A_daily.csv --strategy adaptive_trend

  # 2) 1H 분봉 CSV로 멀티TF 백테스트 (4H 자동 합성)
  python backtest.py --csv data/6A_1h.csv --tf 1H --strategy multi_tf_aggressive

  # 3) API에서 데이터 받아 백테스트 (Windows + HTS)
  python backtest.py --api --bars 1000 --strategy adaptive_trend

데이터 소스 가이드:
  - 일봉: investing.com / Yahoo Finance ticker "6A=F"
  - 분봉: TradingView export, FirstRateData, 또는 Hana 분봉 API
"""

import argparse
import csv
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from strategy.base_strategy import Signal
from strategy.adaptive_trend import AdaptiveTrendStrategy
from strategy.donchian_breakout import DonchianBreakoutStrategy
from strategy.ma_crossover import MACrossoverStrategy
from strategy.multi_tf_aggressive import MultiTFAggressiveStrategy
from utils.data_handler import DataHandler, OHLCVBar, TIMEFRAME_SECONDS
from risk.risk_manager import AUD_TICK_SIZE, AUD_TICK_VALUE


# ── 파라미터 ──────────────────────────────────────────────────────────────────

INITIAL_EQUITY_USD = 21_500.0           # 3,000만원 ≈ $21,500 (USD/KRW 1,400)
RISK_PER_TRADE_PCT = 1.0
SLIPPAGE_TICKS = 1
COMMISSION_USD_PER_CONTRACT = 5.0
PYRAMID_DECAY = [1.0, 0.75, 0.5, 0.5]   # 1단~4단 사이즈 감쇠


@dataclass
class Trade:
    side: str
    entry_date: datetime
    entry_price: float
    exit_date: Optional[datetime] = None
    exit_price: float = 0.0
    contracts: int = 0
    pnl_usd: float = 0.0
    reason: str = ""
    units: int = 1                       # 피라미드 단위 수
    entries: List = field(default_factory=list)  # [(price, contracts), ...]


def build_strategy(name: str):
    if name == "adaptive_trend":
        return AdaptiveTrendStrategy()
    if name == "donchian_breakout":
        return DonchianBreakoutStrategy()
    if name == "ma_crossover":
        return MACrossoverStrategy()
    if name == "multi_tf_aggressive":
        return MultiTFAggressiveStrategy()
    raise ValueError(f"알 수 없는 전략: {name}")


def load_csv(path: str) -> List[OHLCVBar]:
    bars: List[OHLCVBar] = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = _parse_date(row.get("date") or row.get("Date") or row.get("datetime") or row.get("timestamp"))
                bars.append(OHLCVBar(
                    timestamp=ts,
                    open_=float(row.get("open") or row.get("Open")),
                    high=float(row.get("high") or row.get("High")),
                    low=float(row.get("low") or row.get("Low")),
                    close=float(row.get("close") or row.get("Close")),
                    volume=float(row.get("volume") or row.get("Volume") or 0),
                ))
            except Exception as e:
                print(f"파싱 실패 (스킵): {row} ({e})", file=sys.stderr)
    bars.sort(key=lambda b: b.timestamp)
    return bars


def _parse_date(s: str) -> datetime:
    s = s.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%d", "%Y%m%d", "%Y%m%d%H%M%S",
                "%m-%d-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"날짜 파싱 실패: {s}")


def aggregate_bars(bars: List[OHLCVBar], target_tf: str) -> List[OHLCVBar]:
    """저TF 봉을 고TF로 합성. 예: 1H 입력 → 4H 출력.
    target_tf의 epoch 경계로 봉을 묶어 OHLC를 계산합니다.
    """
    if not bars:
        return []
    target_seconds = TIMEFRAME_SECONDS[target_tf.upper()]
    out: List[OHLCVBar] = []
    bucket: Optional[OHLCVBar] = None
    bucket_open_epoch: int = -1

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


# ── 백테스트 엔진 ─────────────────────────────────────────────────────────────

def run_backtest(
    bars: List[OHLCVBar],
    strategy_name: str,
    initial_equity: float = INITIAL_EQUITY_USD,
    risk_pct: float = RISK_PER_TRADE_PCT,
    input_tf: str = "1D",
) -> dict:
    """
    Args:
        bars: 입력 봉 데이터 (input_tf 단위)
        input_tf: 입력 데이터의 타임프레임 (1D, 1H 등)
    """
    strat = build_strategy(strategy_name)
    required_tfs = strat.get_required_timeframes()

    # ── 전략이 요구하는 모든 TF에 대해 DataHandler 준비 ──────────────────
    data_dict: Dict[str, DataHandler] = {}
    bars_per_tf: Dict[str, List[OHLCVBar]] = {}

    input_seconds = TIMEFRAME_SECONDS[input_tf.upper()]
    for tf in required_tfs:
        tf_seconds = TIMEFRAME_SECONDS[tf.upper()]
        data_dict[tf] = DataHandler("BACKTEST", max_bars=len(bars) + 100, timeframe=tf)
        if tf_seconds == input_seconds:
            bars_per_tf[tf] = bars
        elif tf_seconds > input_seconds:
            bars_per_tf[tf] = aggregate_bars(bars, tf)
        else:
            raise ValueError(
                f"입력 TF({input_tf})가 요구 TF({tf})보다 큽니다. "
                f"멀티TF 백테스트는 가장 작은 TF의 데이터를 입력해야 합니다."
            )

    # ── 통일된 시간축으로 진행 (가장 작은 TF 기준) ─────────────────────
    main_tf = required_tfs[0]  # 첫 TF가 가장 큰 것 (예: 4H)
    smallest_tf = min(required_tfs, key=lambda t: TIMEFRAME_SECONDS[t.upper()])
    smallest_bars = bars_per_tf[smallest_tf]

    # 각 TF별 인덱스 추적
    tf_indices = {tf: 0 for tf in required_tfs}

    equity = initial_equity
    peak_equity = initial_equity
    max_dd = 0.0
    daily_pnl = 0.0
    last_day = None
    halt_until = None
    consec_losses = 0

    equity_curve: List[tuple] = []
    trades: List[Trade] = []
    open_trade: Optional[Trade] = None
    risk_unit_usd: float = 0.0          # 1R USD
    base_contracts: int = 0
    units_held: int = 0

    for sb in smallest_bars:
        # 가장 작은 TF는 매 봉 추가
        data_dict[smallest_tf].add_bar(sb)

        # 더 큰 TF는 봉 경계가 끝났을 때만 추가
        for tf in required_tfs:
            if tf == smallest_tf:
                continue
            tfb = bars_per_tf[tf]
            while tf_indices[tf] < len(tfb) and tfb[tf_indices[tf]].timestamp <= sb.timestamp:
                if tf_indices[tf] == 0 or tfb[tf_indices[tf]].timestamp != tfb[tf_indices[tf] - 1].timestamp:
                    data_dict[tf].add_bar(tfb[tf_indices[tf]])
                tf_indices[tf] += 1

        # 일자 변경 체크 → 일일 손익 리셋
        cur_day = sb.timestamp.date()
        if last_day is not None and cur_day != last_day:
            daily_pnl = 0.0
        last_day = cur_day

        # 회로차단기 정지 중이면 스킵
        if halt_until and sb.timestamp < halt_until:
            equity_curve.append((sb.timestamp, equity))
            continue

        # 데이터 부족
        if data_dict[main_tf].bar_count() < strat.get_min_bars_required():
            equity_curve.append((sb.timestamp, equity))
            continue

        # 전략 호출
        signal = strat.on_bar(data_dict)
        slip = SLIPPAGE_TICKS * AUD_TICK_SIZE

        # ── 신규 진입 ──────────────────────────────────────────────
        if open_trade is None and signal.signal in (Signal.LONG, Signal.SHORT):
            entry = sb.close + slip if signal.signal == Signal.LONG else sb.close - slip
            stop_dist = abs(entry - signal.stop_loss)
            if stop_dist < AUD_TICK_SIZE:
                equity_curve.append((sb.timestamp, equity))
                continue
            risk_usd = equity * (risk_pct / 100.0)
            ticks = stop_dist / AUD_TICK_SIZE
            loss_per_contract = ticks * AUD_TICK_VALUE
            contracts = max(int(risk_usd / loss_per_contract), 0)
            if contracts == 0:
                equity_curve.append((sb.timestamp, equity))
                continue
            open_trade = Trade(
                side="LONG" if signal.signal == Signal.LONG else "SHORT",
                entry_date=sb.timestamp,
                entry_price=entry,
                contracts=contracts,
                reason=signal.reason,
                units=1,
                entries=[(entry, contracts)],
            )
            risk_unit_usd = loss_per_contract * contracts
            base_contracts = contracts
            units_held = 1

        # ── 피라미드 추가 ──────────────────────────────────────────
        elif open_trade is not None and signal.signal == Signal.SCALE_IN:
            if units_held < len(PYRAMID_DECAY):
                next_unit = units_held + 1
                add_qty = max(1, int(base_contracts * PYRAMID_DECAY[next_unit - 1]))
                add_price = sb.close + slip if open_trade.side == "LONG" else sb.close - slip
                # 평균 진입가 업데이트
                old_total = open_trade.contracts
                new_total = old_total + add_qty
                open_trade.entry_price = (
                    open_trade.entry_price * old_total + add_price * add_qty
                ) / new_total
                open_trade.contracts = new_total
                open_trade.units = next_unit
                open_trade.entries.append((add_price, add_qty))
                units_held = next_unit

        # ── 부분 익절 ──────────────────────────────────────────────
        elif open_trade is not None and signal.signal == Signal.PARTIAL_TP:
            close_qty = max(1, int(open_trade.contracts * signal.partial_close_pct))
            if close_qty < open_trade.contracts:
                exit_price = sb.close - slip if open_trade.side == "LONG" else sb.close + slip
                diff = (exit_price - open_trade.entry_price) if open_trade.side == "LONG" \
                       else (open_trade.entry_price - exit_price)
                ticks = diff / AUD_TICK_SIZE
                pnl = (ticks * AUD_TICK_VALUE * close_qty
                       - 2 * COMMISSION_USD_PER_CONTRACT * close_qty)
                equity += pnl
                daily_pnl += pnl
                open_trade.contracts -= close_qty
                open_trade.pnl_usd += pnl
                peak_equity = max(peak_equity, equity)
                drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
                max_dd = max(max_dd, drawdown)

        # ── 전량 청산 ──────────────────────────────────────────────
        elif open_trade is not None and signal.signal == Signal.EXIT:
            exit_price = sb.close - slip if open_trade.side == "LONG" else sb.close + slip
            diff = (exit_price - open_trade.entry_price) if open_trade.side == "LONG" \
                   else (open_trade.entry_price - exit_price)
            ticks = diff / AUD_TICK_SIZE
            pnl = (ticks * AUD_TICK_VALUE * open_trade.contracts
                   - 2 * COMMISSION_USD_PER_CONTRACT * open_trade.contracts)
            open_trade.exit_date = sb.timestamp
            open_trade.exit_price = exit_price
            open_trade.pnl_usd += pnl
            trades.append(open_trade)
            equity += pnl
            daily_pnl += pnl
            if open_trade.pnl_usd < 0:
                consec_losses += 1
            else:
                consec_losses = 0
            peak_equity = max(peak_equity, equity)
            drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
            max_dd = max(max_dd, drawdown)
            open_trade = None
            units_held = 0
            base_contracts = 0

        # ── 회로차단기 (일일 -6%, 5연패) ─────────────────────────
        if daily_pnl < -equity * 0.06:
            halt_until = sb.timestamp + timedelta(hours=24)
            if open_trade is not None:
                # 강제 청산
                exit_price = sb.close
                diff = (exit_price - open_trade.entry_price) if open_trade.side == "LONG" \
                       else (open_trade.entry_price - exit_price)
                pnl = (diff / AUD_TICK_SIZE) * AUD_TICK_VALUE * open_trade.contracts
                open_trade.exit_date = sb.timestamp
                open_trade.exit_price = exit_price
                open_trade.pnl_usd += pnl
                open_trade.reason += " | 회로차단기 강제청산"
                trades.append(open_trade)
                equity += pnl
                open_trade = None
        if consec_losses >= 5:
            halt_until = sb.timestamp + timedelta(hours=24)
            consec_losses = 0

        equity_curve.append((sb.timestamp, equity))

    return _summarize(trades, equity_curve, initial_equity, max_dd, smallest_bars)


def _summarize(trades, equity_curve, initial_equity, max_dd, bars):
    if not trades:
        return {"trades": 0, "msg": "거래 없음"}

    total_pnl = sum(t.pnl_usd for t in trades)
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    win_rate = len(wins) / len(trades) * 100

    avg_win = sum(t.pnl_usd for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl_usd for t in losses) / len(losses) if losses else 0
    profit_factor = (
        sum(t.pnl_usd for t in wins) / abs(sum(t.pnl_usd for t in losses))
        if losses and sum(t.pnl_usd for t in losses) != 0 else float("inf")
    )

    final_equity = initial_equity + total_pnl
    total_return = (final_equity / initial_equity - 1) * 100

    if bars:
        days = (bars[-1].timestamp - bars[0].timestamp).days
        years = days / 365.25 if days > 0 else 1
        cagr = ((final_equity / initial_equity) ** (1 / years) - 1) * 100 if years > 0 else 0
        # 월 평균 거래 횟수
        months = max(1, days / 30.4)
        trades_per_month = len(trades) / months
    else:
        cagr = 0
        trades_per_month = 0

    daily_rets = []
    prev_eq = initial_equity
    for _, eq in equity_curve:
        daily_rets.append((eq - prev_eq) / prev_eq if prev_eq > 0 else 0)
        prev_eq = eq
    if len(daily_rets) > 1:
        mean = sum(daily_rets) / len(daily_rets)
        var = sum((r - mean) ** 2 for r in daily_rets) / (len(daily_rets) - 1)
        std = math.sqrt(var)
        sharpe = (mean / std * math.sqrt(252)) if std > 0 else 0
    else:
        sharpe = 0

    # 피라미드 트레이드 비율
    pyramid_trades = sum(1 for t in trades if t.units > 1)
    avg_units = sum(t.units for t in trades) / len(trades)

    return {
        "trades": len(trades),
        "trades_per_month": trades_per_month,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_win_usd": avg_win,
        "avg_loss_usd": avg_loss,
        "total_return_pct": total_return,
        "cagr_pct": cagr,
        "max_drawdown_pct": max_dd * 100,
        "sharpe": sharpe,
        "final_equity_usd": final_equity,
        "pyramid_trades": pyramid_trades,
        "avg_units": avg_units,
    }


def print_summary(result: dict, strategy_name: str):
    print("\n" + "=" * 60)
    print(f" 백테스트 결과: {strategy_name}")
    print("=" * 60)
    if result.get("trades", 0) == 0:
        print(" 거래 없음 (데이터 부족 또는 진입 조건 미충족)")
        return
    print(f" 거래 횟수    : {result['trades']} (월 평균 {result['trades_per_month']:.1f}회)")
    print(f" 승률         : {result['win_rate']:.1f}%")
    print(f" 손익비(PF)   : {result['profit_factor']:.2f}")
    print(f" 평균 수익    : ${result['avg_win_usd']:,.0f}")
    print(f" 평균 손실    : ${result['avg_loss_usd']:,.0f}")
    print(f" 총수익률     : {result['total_return_pct']:+.1f}%")
    print(f" CAGR         : {result['cagr_pct']:+.1f}%")
    print(f" 최대낙폭     : -{result['max_drawdown_pct']:.1f}%")
    print(f" 샤프지수     : {result['sharpe']:.2f}")
    print(f" 최종 자산    : ${result['final_equity_usd']:,.0f}")
    if result.get('pyramid_trades', 0) > 0:
        print(f" 피라미드     : {result['pyramid_trades']}회 (평균 {result['avg_units']:.1f}단)")
    print("=" * 60)


# ── 엔트리포인트 ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="6A 선물 전략 백테스트")
    parser.add_argument("--csv", help="OHLCV CSV 파일 경로")
    parser.add_argument("--api", action="store_true", help="하나증권 API에서 데이터 수신 (Windows)")
    parser.add_argument("--bars", type=int, default=1000, help="API 사용 시 봉 개수")
    parser.add_argument("--tf", default="1D", help="입력 데이터의 타임프레임 (1D, 1H 등)")
    parser.add_argument("--strategy", default="adaptive_trend",
                        choices=["adaptive_trend", "donchian_breakout", "ma_crossover",
                                 "multi_tf_aggressive"])
    parser.add_argument("--equity", type=float, default=INITIAL_EQUITY_USD,
                        help="초기 자산 USD (기본 $21,500 ≈ 3,000만원)")
    parser.add_argument("--risk", type=float, default=RISK_PER_TRADE_PCT,
                        help="1회 거래 리스크 (%%)")
    args = parser.parse_args()

    if not args.csv and not args.api:
        parser.error("--csv 또는 --api 중 하나를 지정하세요.")

    if args.csv:
        bars = load_csv(args.csv)
        print(f"CSV에서 {len(bars)}봉 ({args.tf}) 로드: "
              f"{bars[0].timestamp} ~ {bars[-1].timestamp}")
    else:
        import yaml
        from api.hana_api import HanaAPI
        cfg = yaml.safe_load(open("config/config.yaml", "r", encoding="utf-8"))
        api = HanaAPI(cfg["api"]["account_no"], cfg["api"]["account_pw"], cfg["api"]["is_mock"])
        if not api.connect():
            print("API 연결 실패")
            sys.exit(1)
        symbol_full = cfg["symbol"]["code"]
        if cfg["symbol"].get("contract_month"):
            symbol_full += cfg["symbol"]["contract_month"]
        raw = api.get_bars(symbol_full, args.tf, args.bars)
        bars = []
        for b in raw:
            ts = b.get("timestamp") or datetime.strptime(b["date"], "%Y%m%d")
            bars.append(OHLCVBar(ts, b["open"], b["high"], b["low"],
                                 b["close"], b["volume"]))
        api.disconnect()
        print(f"API에서 {len(bars)}봉({args.tf}) 수신")

    if len(bars) < 250:
        print("⚠ 데이터가 250봉 미만입니다. EMA200 기반 전략은 결과가 부정확할 수 있습니다.")

    result = run_backtest(bars, args.strategy, args.equity, args.risk, input_tf=args.tf)
    print_summary(result, args.strategy)


if __name__ == "__main__":
    main()
