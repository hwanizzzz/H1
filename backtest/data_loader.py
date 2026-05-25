"""
백테스트용 OHLCV 데이터 로더.

  - load_csv:          실제 일봉 CSV 로드 (date,open,high,low,close,volume)
  - generate_synthetic: 합성 가격 데이터 생성 (엔진 점검 / API 없는 환경용)

주의: 합성 데이터는 엔진 동작 검증용일 뿐 전략 성과의 근거가 될 수 없습니다.
실전 검증은 반드시 하나증권 API로 받은 실제 일봉 CSV로 수행하세요.
"""

import csv
from datetime import datetime, timedelta
from typing import List

import numpy as np

from utils.data_handler import OHLCVBar


def load_csv(path: str) -> List[OHLCVBar]:
    """일봉 CSV 로드. 컬럼: date(YYYYMMDD 또는 YYYY-MM-DD), open, high, low, close, volume."""
    bars: List[OHLCVBar] = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = (row.get("date") or row.get("timestamp") or "").strip()
            ts = _parse_date(raw)
            bars.append(OHLCVBar(
                ts,
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row.get("volume", 0) or 0),
            ))
    bars.sort(key=lambda b: b.timestamp)
    return bars


def _parse_date(raw: str) -> datetime:
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"날짜 형식을 인식할 수 없음: {raw}")


def generate_synthetic(n: int = 750, start_price: float = 0.6500,
                       tick_size: float = 0.0001, seed: int = 42) -> List[OHLCVBar]:
    """추세 구간과 횡보 구간이 섞인 합성 일봉을 생성합니다 (엔진 점검용)."""
    rng = np.random.default_rng(seed)
    price = start_price
    bars: List[OHLCVBar] = []
    start = datetime(2022, 1, 3)

    # 추세/횡보가 번갈아 나타나도록 드리프트 레짐 구성
    drift = 0.0
    daily_vol = start_price * 0.008
    for i in range(n):
        if i % 90 == 0:
            drift = rng.choice([0.0012, -0.0012, 0.0, 0.0008, -0.0008]) * start_price
        open_ = price
        close = open_ + drift + rng.normal(0, daily_vol)
        close = max(close, tick_size * 10)
        high = max(open_, close) + abs(rng.normal(0, daily_vol * 0.5))
        low = min(open_, close) - abs(rng.normal(0, daily_vol * 0.5))
        vol = float(rng.integers(5000, 50000))
        # 틱 사이즈로 라운딩
        rnd = lambda x: round(x / tick_size) * tick_size
        bars.append(OHLCVBar(start + timedelta(days=i),
                             rnd(open_), rnd(high), rnd(low), rnd(close), vol))
        price = close
    return bars
