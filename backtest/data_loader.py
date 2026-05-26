"""
백테스트용 OHLCV 데이터 로더.

지원:
  - load_csv(path): CSV → List[OHLCVBar]
      필수 컬럼: date(또는 timestamp), open, high, low, close, volume(선택)
      날짜 형식: YYYY-MM-DD, YYYYMMDD, ISO8601 자동 인식
  - generate_synthetic(...): 재현 가능한 합성 데이터 (실데이터 없이 엔진 점검용)

실제 검증은 data/ 디렉터리에 종목별 CSV(예: data/MGC_1D.csv)를 넣고
run_backtest.py 로 실행하세요.
"""

from datetime import datetime, timedelta
from typing import List, Optional
import csv
import os
import numpy as np

from utils.data_handler import OHLCVBar


def _parse_date(s: str) -> datetime:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # ISO8601 마지막 시도
    return datetime.fromisoformat(s)


def load_csv(path: str) -> List[OHLCVBar]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"데이터 파일 없음: {path}")

    bars: List[OHLCVBar] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = {c.lower(): c for c in (reader.fieldnames or [])}
        date_key = cols.get("date") or cols.get("timestamp") or cols.get("time")
        if not date_key:
            raise ValueError("CSV에 date/timestamp 컬럼이 필요합니다")

        def col(row, name):
            return row[cols[name]]

        for row in reader:
            try:
                ts = _parse_date(row[date_key])
                vol = float(col(row, "volume")) if "volume" in cols else 0.0
                bars.append(OHLCVBar(
                    ts,
                    float(col(row, "open")),
                    float(col(row, "high")),
                    float(col(row, "low")),
                    float(col(row, "close")),
                    vol,
                ))
            except (ValueError, KeyError):
                continue

    bars.sort(key=lambda b: b.timestamp)
    return bars


def generate_synthetic(n: int = 1500, start_price: float = 100.0,
                       daily_vol: float = 0.01, drift: float = 0.0002,
                       seed: Optional[int] = 42,
                       tick_size: float = 0.25) -> List[OHLCVBar]:
    """
    GBM 기반 합성 일봉 생성 (추세+노이즈). 엔진 동작 점검 전용.
    실제 성과 검증에는 사용하지 마세요.
    """
    rng = np.random.default_rng(seed)
    bars: List[OHLCVBar] = []
    price = start_price
    start = datetime(2018, 1, 1)

    for i in range(n):
        ret = drift + daily_vol * rng.standard_normal()
        new_price = max(price * (1 + ret), tick_size)
        intrabar = abs(daily_vol * price) * (0.5 + rng.random())
        high = max(price, new_price) + intrabar * rng.random()
        low = min(price, new_price) - intrabar * rng.random()
        bars.append(OHLCVBar(
            start + timedelta(days=i),
            round(price / tick_size) * tick_size,
            round(high / tick_size) * tick_size,
            round(max(low, tick_size) / tick_size) * tick_size,
            round(new_price / tick_size) * tick_size,
            float(rng.integers(1000, 50000)),
        ))
        price = new_price

    return bars
