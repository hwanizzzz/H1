"""
백테스터 — CME 호주달러 선물(6A) 전략 검증 도구

실행 방법:
  # 1) CSV 파일로 백테스트 (date,open,high,low,close,volume 헤더)
  python backtest.py --csv data/6A_daily.csv --strategy adaptive_trend

  # 2) 하나증권 API에서 일봉 받아 백테스트 (HTS 로그인 상태 필요, Windows)
  python backtest.py --api --bars 1000 --strategy adaptive_trend

CSV 데이터 출처 예시:
  - investing.com → AUD/USD Futures 과거 데이터 다운로드
  - Yahoo Finance ticker: 6A=F
  - 하나증권 HTS 차트 → 데이터 내보내기

산출 지표:
  - 총수익률 / CAGR
  - 최대낙폭(MDD)
  - 샤프지수
  - 승률 / 손익비 / 평균 R-multiple
  - 거래 횟수 / 월별 손익
"""

import argparse
import csv
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from strategy.base_strategy import Signal
from strategy.adaptive_trend import AdaptiveTrendStrategy
from strategy.donchian_breakout import DonchianBreakoutStrategy
from strategy.ma_crossover import MACrossoverStrategy
from utils.data_handler import DataHandler, OHLCVBar
from risk.risk_manager import AUD_TICK_SIZE, AUD_TICK_VALUE


# ── 파라미터 ──────────────────────────────────────────────────────────────────

INITIAL_EQUITY_USD = 10_000.0
RISK_PER_TRADE_PCT = 1.0
SLIPPAGE_TICKS = 1
COMMISSION_USD_PER_CONTRACT = 5.0


@dataclass
class Trade:
    side: str
    entry_date: datetime
    entry_price: float
    exit_date: Optional[datetime] = None
    exit_price: float = 0.0
    contracts: int = 0
    pnl_usd: float = 0.0
    r_multiple: float = 0.0
    reason: str = ""


def build_strategy(name: str):
    if name == "adaptive_trend":
        return AdaptiveTrendStrategy()
    if name == "donchian_breakout":
        return DonchianBreakoutStrategy()
    if name == "ma_crossover":
        return MACrossoverStrategy()
    raise ValueError(f"알 수 없는 전략: {name}")


def load_csv(path: str) -> List[OHLCVBar]:
    bars: List[OHLCVBar] = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = _parse_date(row.get("date") or row.get("Date") or row.get("datetime"))
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
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m-%d-%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"날짜 파싱 실패: {s}")


# ── 백테스트 엔진 ─────────────────────────────────────────────────────────────

def run_backtest(bars: List[OHLCVBar], strategy_name: str,
                 initial_equity: float = INITIAL_EQUITY_USD,
                 risk_pct: float = RISK_PER_TRADE_PCT) -> dict:
    strat = build_strategy(strategy_name)
    data = DataHandler("BACKTEST", max_bars=len(bars) + 10)

    equity = initial_equity
    peak_equity = initial_equity
    max_dd = 0.0
    equity_curve = []
    trades: List[Trade] = []

    open_trade: Optional[Trade] = None

    for bar in bars:
        data.add_bar(bar)
        if data.bar_count() < strat.get_min_bars_required():
            equity_curve.append((bar.timestamp, equity))
            continue

        signal = strat.on_bar(data)

        # 다음 봉 시가 체결 가정 → 오늘은 신호만 받고, 다음 루프에서 체결
        # 단순화를 위해 종가 체결 + 슬리피지 보정으로 처리
        slip = SLIPPAGE_TICKS * AUD_TICK_SIZE

        if open_trade is None:
            if signal.signal in (Signal.LONG, Signal.SHORT):
                entry = bar.close + slip if signal.signal == Signal.LONG else bar.close - slip
                stop_dist = abs(entry - signal.stop_loss)
                if stop_dist < AUD_TICK_SIZE:
                    continue
                # 포지션 사이징
                risk_usd = equity * (risk_pct / 100.0)
                ticks = stop_dist / AUD_TICK_SIZE
                loss_per_contract = ticks * AUD_TICK_VALUE
                contracts = max(int(risk_usd / loss_per_contract), 0)
                if contracts == 0:
                    continue
                open_trade = Trade(
                    side="LONG" if signal.signal == Signal.LONG else "SHORT",
                    entry_date=bar.timestamp,
                    entry_price=entry,
                    contracts=contracts,
                    reason=signal.reason,
                )
        else:
            if signal.signal == Signal.EXIT:
                exit_price = bar.close - slip if open_trade.side == "LONG" else bar.close + slip
                diff = (exit_price - open_trade.entry_price) if open_trade.side == "LONG" \
                       else (open_trade.entry_price - exit_price)
                ticks = diff / AUD_TICK_SIZE
                pnl = ticks * AUD_TICK_VALUE * open_trade.contracts \
                      - 2 * COMMISSION_USD_PER_CONTRACT * open_trade.contracts
                open_trade.exit_date = bar.timestamp
                open_trade.exit_price = exit_price
                open_trade.pnl_usd = pnl
                # R-multiple
                init_risk = abs(open_trade.entry_price - signal.stop_loss) if signal.stop_loss else 0
                # 청산 시점에는 stop_loss=0이므로, 진입 시 risk를 별도 추적 필요 — 단순화
                trades.append(open_trade)
                equity += pnl
                peak_equity = max(peak_equity, equity)
                drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
                max_dd = max(max_dd, drawdown)
                open_trade = None

        equity_curve.append((bar.timestamp, equity))

    return _summarize(trades, equity_curve, initial_equity, max_dd, bars)


def _summarize(trades: List[Trade], equity_curve, initial_equity: float,
               max_dd: float, bars: List[OHLCVBar]) -> dict:
    if not trades:
        return {"trades": 0, "msg": "거래 없음"}

    total_pnl = sum(t.pnl_usd for t in trades)
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    win_rate = len(wins) / len(trades) * 100

    avg_win = sum(t.pnl_usd for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl_usd for t in losses) / len(losses) if losses else 0
    profit_factor = (sum(t.pnl_usd for t in wins) / abs(sum(t.pnl_usd for t in losses))
                     if losses and sum(t.pnl_usd for t in losses) != 0 else float("inf"))

    final_equity = initial_equity + total_pnl
    total_return = (final_equity / initial_equity - 1) * 100

    # CAGR
    if bars:
        days = (bars[-1].timestamp - bars[0].timestamp).days
        years = days / 365.25 if days > 0 else 1
        cagr = ((final_equity / initial_equity) ** (1 / years) - 1) * 100 if years > 0 else 0
    else:
        cagr = 0

    # 샤프 (간이): 일 수익률 표준편차 기반
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

    return {
        "trades": len(trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_win_usd": avg_win,
        "avg_loss_usd": avg_loss,
        "total_return_pct": total_return,
        "cagr_pct": cagr,
        "max_drawdown_pct": max_dd * 100,
        "sharpe": sharpe,
        "final_equity_usd": final_equity,
    }


def print_summary(result: dict, strategy_name: str):
    print("\n" + "=" * 60)
    print(f" 백테스트 결과: {strategy_name}")
    print("=" * 60)
    if result.get("trades", 0) == 0:
        print(" 거래 없음 (데이터 부족 또는 진입 조건 미충족)")
        return
    print(f" 거래 횟수    : {result['trades']}")
    print(f" 승률         : {result['win_rate']:.1f}%")
    print(f" 손익비(PF)   : {result['profit_factor']:.2f}")
    print(f" 평균 수익    : ${result['avg_win_usd']:,.0f}")
    print(f" 평균 손실    : ${result['avg_loss_usd']:,.0f}")
    print(f" 총수익률     : {result['total_return_pct']:+.1f}%")
    print(f" CAGR         : {result['cagr_pct']:+.1f}%")
    print(f" 최대낙폭     : -{result['max_drawdown_pct']:.1f}%")
    print(f" 샤프지수     : {result['sharpe']:.2f}")
    print(f" 최종 자산    : ${result['final_equity_usd']:,.0f}")
    print("=" * 60)


# ── 엔트리포인트 ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="6A 선물 전략 백테스트")
    parser.add_argument("--csv", help="OHLCV CSV 파일 경로")
    parser.add_argument("--api", action="store_true", help="하나증권 API에서 데이터 수신 (Windows)")
    parser.add_argument("--bars", type=int, default=1000, help="API 사용 시 봉 개수")
    parser.add_argument("--strategy", default="adaptive_trend",
                        choices=["adaptive_trend", "donchian_breakout", "ma_crossover"])
    parser.add_argument("--equity", type=float, default=INITIAL_EQUITY_USD,
                        help="초기 자산 USD")
    parser.add_argument("--risk", type=float, default=RISK_PER_TRADE_PCT,
                        help="1회 거래 리스크 (%%)")
    args = parser.parse_args()

    if not args.csv and not args.api:
        parser.error("--csv 또는 --api 중 하나를 지정하세요.")

    if args.csv:
        bars = load_csv(args.csv)
        print(f"CSV에서 {len(bars)}봉 로드: {bars[0].timestamp.date()} ~ {bars[-1].timestamp.date()}")
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
        raw = api.get_daily_bars(symbol_full, args.bars)
        bars = [OHLCVBar(datetime.strptime(b["date"], "%Y%m%d"),
                         b["open"], b["high"], b["low"], b["close"], b["volume"])
                for b in raw]
        api.disconnect()
        print(f"API에서 {len(bars)}봉 수신")

    if len(bars) < 250:
        print("⚠ 데이터가 250봉 미만입니다. EMA200 기반 전략은 결과가 부정확할 수 있습니다.")

    result = run_backtest(bars, args.strategy, args.equity, args.risk)
    print_summary(result, args.strategy)


if __name__ == "__main__":
    main()
