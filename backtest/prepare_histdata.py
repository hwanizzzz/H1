"""
HistData.com 1분봉(M1) ASCII 파일들을 묶어 30분/60분/일봉으로 변환.

HistData M1 형식 (세미콜론 구분, 헤더 없음):
  YYYYMMDD HHMMSS;OPEN;HIGH;LOW;CLOSE;VOLUME
  - 타임스탬프는 미국 동부시간(EST, 서머타임 미적용) 기준
  - FX라 VOLUME은 0 (의미 없음)

사용 예:
  python -m backtest.prepare_histdata --src data/histdata_raw --out data --symbol AUDUSD
  → data/AUDUSD_30M.csv, AUDUSD_60M.csv, AUDUSD_1D.csv 생성
"""

import argparse
import glob
import os

import pandas as pd


def load_m1(src_dir: str) -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(src_dir, "DAT_ASCII_*_M1_*.csv")))
    if not files:
        raise SystemExit(f"M1 csv 파일을 찾지 못함: {src_dir}/DAT_ASCII_*_M1_*.csv")
    frames = []
    for fp in files:
        df = pd.read_csv(fp, sep=";", header=None,
                         names=["dt", "open", "high", "low", "close", "volume"])
        frames.append(df)
        print(f"  로드: {os.path.basename(fp)} ({len(df):,}행)")
    out = pd.concat(frames, ignore_index=True)
    out["dt"] = pd.to_datetime(out["dt"], format="%Y%m%d %H%M%S")
    out = out.drop_duplicates(subset="dt").sort_values("dt").set_index("dt")
    return out


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = df.resample(rule).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["open"])
    return agg


def main():
    p = argparse.ArgumentParser(description="HistData M1 → 30M/60M/1D 변환")
    p.add_argument("--src", default="data/histdata_raw")
    p.add_argument("--out", default="data")
    p.add_argument("--symbol", default="AUDUSD")
    args = p.parse_args()

    print("M1 파일 로드 중...")
    m1 = load_m1(args.src)
    print(f"합계 {len(m1):,}분봉 | 기간 {m1.index[0]} ~ {m1.index[-1]}")

    for label, rule in [("30M", "30min"), ("60M", "60min"), ("1D", "1D")]:
        bars = resample(m1, rule)
        path = os.path.join(args.out, f"{args.symbol}_{label}.csv")
        bars.reset_index().rename(columns={"dt": "date"}).to_csv(path, index=False)
        print(f"  저장: {path} ({len(bars):,}봉)")


if __name__ == "__main__":
    main()
