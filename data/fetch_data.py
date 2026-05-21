"""
Fetch 6A/6E/6B/6C daily OHLC (2023-2024) from public ECB FX dataset on GitHub.

Source dataset (Federal Reserve H.10 republished by datahub):
  https://raw.githubusercontent.com/datasets/exchange-rates/master/data/daily.csv

Format: USD-base quotes (e.g., "Australia 1.4859" = 1 USD = 1.4859 AUD).
We invert to direct quotes (AUD/USD, EUR/USD, GBP/USD, CAD/USD) to match
CME futures convention.

The dataset only provides daily mid-rates (no OHLC). For backtest purposes
we approximate intraday range using ATR-style synthesis:
  - open  = previous close
  - close = today's published rate
  - high/low = open or close ± 0.4 × |today - yesterday| (proxy for daily range)

This is good enough for daily-timeframe strategies (adaptive_trend) but does
NOT model intraday microstructure. For multi-TF strategies we additionally
generate 1H bars via a Brownian bridge anchored to daily OHLC.
"""
import csv
import io
import os
import sys
import math
import random
import urllib.request
from datetime import datetime, timedelta

URL = "https://raw.githubusercontent.com/datasets/exchange-rates/master/data/daily.csv"
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

SYMBOL_MAP = {
    "6A": "Australia",
    "6E": "Euro",
    "6B": "United Kingdom",
    "6C": "Canada",
}

START_DATE = "2022-12-15"   # buffer for EMA200 warmup
END_DATE   = "2024-12-31"


def fetch_raw():
    print("Downloading ECB FX dataset...")
    with urllib.request.urlopen(URL, timeout=30) as r:
        text = r.read().decode("utf-8")
    print(f"  {len(text):,} bytes")
    return text


def parse_per_country(text):
    """Return dict[country] -> list[(date_str, rate_float)] sorted by date."""
    reader = csv.reader(io.StringIO(text))
    next(reader)
    out = {}
    for row in reader:
        date_s, country, rate_s = row
        if date_s < START_DATE or date_s > END_DATE:
            continue
        if country not in SYMBOL_MAP.values():
            continue
        try:
            rate = float(rate_s)
        except ValueError:
            continue
        out.setdefault(country, []).append((date_s, rate))
    for c in out:
        out[c].sort(key=lambda x: x[0])
    return out


def synth_daily_ohlc(date_str, prev_close, today_rate):
    """Convert mid-rate point series into pseudo-OHLC.

    AUD/USD volatility regime: daily ATR is roughly 0.6 * |close-prev_close|
    plus a noise floor (~0.3% of price). We use the larger of the two as
    the intraday range, then place high/low symmetrically.
    """
    move = abs(today_rate - prev_close)
    noise_floor = 0.0030 * today_rate   # 30 bps
    daily_range = max(move * 1.4, noise_floor)

    open_p = prev_close
    close_p = today_rate

    body_mid = (open_p + close_p) / 2.0
    half_range = daily_range / 2.0

    high = body_mid + half_range
    low = body_mid - half_range
    # ensure OHLC validity
    high = max(high, open_p, close_p)
    low = min(low, open_p, close_p)
    return open_p, high, low, close_p


def write_daily_csv(symbol, country, series, out_path):
    """series = [(date_str, rate), ...] in chronological order.
    USD-base rates are inverted to direct quotes.
    """
    rows = []
    prev_close = None
    for date_s, rate in series:
        # invert USD-base to direct quote (X/USD)
        direct = 1.0 / rate
        if prev_close is None:
            o = h = l = c = direct
        else:
            o, h, l, c = synth_daily_ohlc(date_s, prev_close, direct)
        rows.append((date_s, o, h, l, c))
        prev_close = direct

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("date,open,high,low,close,volume\n")
        for d, o, h, l, c in rows:
            f.write(f"{d},{o:.6f},{h:.6f},{l:.6f},{c:.6f},1000\n")
    print(f"  {symbol} ({country}) -> {out_path} ({len(rows)} rows; {rows[0][0]} ~ {rows[-1][0]})")


def brownian_bridge_1h(date_str, o, h, l, c, hours_per_day=24, seed=0):
    """Generate synthetic 1H OHLC from daily OHLC via Brownian bridge.

    Anchors first hour open=o, last hour close=c, hits high/low at random
    intermediate hours.
    """
    rng = random.Random(seed)
    n = hours_per_day
    base_ts = datetime.strptime(date_str, "%Y-%m-%d")
    # generate n random log-returns then scale so cumulative path goes o->c
    # while also touching h and l.
    if n <= 2:
        return [(base_ts, o, max(o, c), min(o, c), c, 50)]

    # 1) random walk increments
    increments = [rng.gauss(0, 1) for _ in range(n)]
    # adjust to end at log(c/o)
    target = math.log(c / o) if o > 0 else 0.0
    s = sum(increments)
    if abs(s) < 1e-12:
        increments = [target / n] * n
    else:
        scale = target / s
        increments = [x * scale + target / n * 0.0 for x in increments]
        # re-balance
        actual = sum(increments)
        increments = [x + (target - actual) / n for x in increments]

    # 2) compute path
    path = [math.log(o)]
    for inc in increments:
        path.append(path[-1] + inc)
    prices = [math.exp(p) for p in path]
    # force exact endpoints
    prices[0] = o
    prices[-1] = c

    # 3) scale path so it touches h and l
    cur_max = max(prices)
    cur_min = min(prices)
    if cur_max > cur_min:
        target_max = h
        target_min = l
        # stretch around midpoint
        cur_mid = (cur_max + cur_min) / 2.0
        tgt_mid = (target_max + target_min) / 2.0
        cur_amp = (cur_max - cur_min) / 2.0
        tgt_amp = (target_max - target_min) / 2.0
        if cur_amp > 1e-12:
            factor = tgt_amp / cur_amp
            prices = [tgt_mid + (p - cur_mid) * factor for p in prices]
        prices[0] = o
        prices[-1] = c

    bars = []
    for i in range(n):
        o_h = prices[i]
        c_h = prices[i + 1]
        # within-hour range as fraction of daily range
        wiggle = (h - l) * 0.06 * rng.random()
        h_h = max(o_h, c_h) + wiggle
        l_h = min(o_h, c_h) - wiggle
        ts = base_ts + timedelta(hours=i)
        bars.append((ts, o_h, h_h, l_h, c_h, 50))
    return bars


def write_1h_csv(symbol, country, series, out_path, seed_base=42):
    """Generate synthetic 1H bars from daily mid-rates.

    Quality caveats:
      - Real intraday volatility is concentrated in session overlaps
        (London/NY), but this synthesis is uniform. Strategies relying
        heavily on intraday momentum patterns will be biased.
      - Daily breakouts will be approximately preserved; intraday Donchian
        breakouts within a day will be noisier than reality.
    """
    rows = []
    prev_close = None
    seed = seed_base
    for date_s, rate in series:
        direct = 1.0 / rate
        if prev_close is None:
            o = h = l = c = direct
        else:
            o, h, l, c = synth_daily_ohlc(date_s, prev_close, direct)
        seed += 1
        bars = brownian_bridge_1h(date_s, o, h, l, c, hours_per_day=24, seed=seed)
        rows.extend(bars)
        prev_close = direct

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("date,open,high,low,close,volume\n")
        for ts, o, h, l, c, v in rows:
            f.write(f"{ts:%Y-%m-%d %H:%M:%S},{o:.6f},{h:.6f},{l:.6f},{c:.6f},{v}\n")
    print(f"  {symbol} 1H -> {out_path} ({len(rows)} bars)")


def main():
    text = fetch_raw()
    by_country = parse_per_country(text)
    print(f"Parsed {sum(len(v) for v in by_country.values())} rows across {len(by_country)} countries")

    for symbol, country in SYMBOL_MAP.items():
        series = by_country.get(country)
        if not series:
            print(f"!!! {symbol} ({country}) data missing")
            continue
        # daily
        write_daily_csv(symbol, country,
                        series, os.path.join(OUT_DIR, f"{symbol}_daily.csv"))
        # 1H synthesis
        write_1h_csv(symbol, country,
                     series, os.path.join(OUT_DIR, f"{symbol}_1h.csv"))


if __name__ == "__main__":
    main()
