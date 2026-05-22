"""
하나증권 1Q Open API 분봉/일봉 데이터 추출기 — Windows 전용

목적:
  CME 해외선물(6A, 6E, 6B, 6C 및 micro M6A/M6E 등)의 1H/4H/1D 봉 데이터를
  하나증권 1Q API로 끌어와 백테스트용 CSV 파일로 저장.

실행 환경:
  - Windows 10/11 (필수, COM API 의존)
  - Python 3.10~3.12 (32-bit 권장 — 일부 증권사 COM은 32-bit만 지원)
  - 하나증권 1QHTS 설치 + 로그인 상태
  - 1QHTS 화면 0789에서 "해외파생 API" 신청 승인
  - pip install pywin32 pyyaml

사용법:
  1) 1QHTS 로그인 (모의투자 또는 실계좌)
  2) config/config.yaml 의 api.account_no/password 입력 확인
  3) 본 스크립트 실행:
       python tools/extract_hana_bars.py --symbols 6A,6E --timeframes 1H,4H,1D \
                                          --count 900 --pages 50
  4) data/6A_1h.csv, data/6E_1h.csv 등이 생성됨
  5) 생성된 CSV를 그대로 백테스트에 사용:
       python ramr_backtest.py --equity-krw 100000000

주의:
  - 하나증권 분봉 API는 일반적으로 **최근 6~12개월치 데이터만 보관**합니다.
    11년치 1H 데이터가 필요하면 FirstRateData($50~$100/yr) 등 별도 소스 활용.
  - count는 1회 호출당 최대 900봉 권장 (서버 부하 보호).
  - pages는 페이지네이션 횟수. 1H × 900 × pages → 시간 범위 = pages × 37.5일.
  - 호출 간 sleep 0.3초 이상 권장 (Rate-limit 방지).

종목 코드 가이드 (CME 해외선물):
  6A/M6A — Aussie Dollar
  6E/M6E — Euro
  6B/M6B — British Pound
  6C/M6C — Canadian Dollar
  만기월 접미사: H(3월) M(6월) U(9월) Z(12월)
  예: 6AM26 = 2026년 6월 만기 호주달러
  최신 만기물(=front-month)을 자동 결정하거나 직접 지정:
    --symbols 6AM26,6EM26
  또는 연속종목 코드 (증권사가 지원하면):
    --symbols 6A.CME
"""
import argparse
import csv
import os
import sys
import time
import yaml
from datetime import datetime, timedelta
from typing import List, Dict, Optional

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.hana_api import HanaAPI, TIMEFRAME_TO_MINUTES, TR_OVERSEAS_FUTURES_MIN, TR_OVERSEAS_FUTURES_CHART
from utils.logger import setup_logger

logger = setup_logger("extract_hana_bars")

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "config.yaml")


# ── 만기월 자동 결정 ─────────────────────────────────────────────────────────

def auto_contract_month(today: Optional[datetime] = None) -> str:
    """현재 시점에서 가장 거래량 많은 만기월 접미사 + 연도 2자리 반환.
    분기 만기물 H/M/U/Z 중 다음 만기를 선택.
    """
    today = today or datetime.now()
    # 분기 만기월 코드 매핑
    quarters = [(3, "H"), (6, "M"), (9, "U"), (12, "Z")]
    # 현재 월 이후의 첫 분기 만기를 선택 (만기일 직전 1개월은 다음 분기로 롤)
    for q_month, q_code in quarters:
        if today.month <= q_month - 1:   # 만기 1개월 전부터 다음 분기 사용
            return f"{q_code}{today.year % 100:02d}"
    # 12월 만기 이후 → 다음 해 3월
    return f"H{(today.year + 1) % 100:02d}"


def resolve_symbol(base: str) -> str:
    """심볼이 만기월 접미사를 포함하지 않으면 자동 추가.
    예: '6A' → '6AM26', '6AM26' → '6AM26' (그대로)
    """
    if len(base) >= 4 and base[-3] in "HMUZ":
        return base   # 이미 만기월 포함
    return base + auto_contract_month()


# ── 페이지네이션 추출 ─────────────────────────────────────────────────────────

def extract_paginated(api: HanaAPI, symbol: str, timeframe: str,
                      count_per_page: int, max_pages: int,
                      end_date: Optional[datetime] = None,
                      sleep_sec: float = 0.4) -> List[Dict]:
    """
    하나 API 분봉 TR을 페이지네이션으로 끌어와 전체 봉을 수집.

    하나증권 분봉 TR(OFHDFUT2)은 prev_next=2를 사용한 연속조회 또는
    기준일자(SetInputValue) 변경으로 페이징합니다. 본 함수는 두 방식을 모두 시도.

    Args:
        symbol: 종목코드 (만기월 포함, 예: '6AM26')
        timeframe: '1M','5M','15M','30M','1H','2H','4H','1D'
        count_per_page: 1회 호출당 봉 수 (900 권장)
        max_pages: 최대 페이지 수 (1H × 900 × N → N × 37.5일 분량)
        end_date: 마지막 날짜 (None이면 현재). 그 이전 데이터를 거꾸로 수집.
    """
    all_bars: List[Dict] = []
    current_end = end_date or datetime.now()

    for page in range(max_pages):
        logger.info(f"  [{symbol} {timeframe}] page {page+1}/{max_pages} "
                    f"기준일={current_end:%Y-%m-%d} 요청...")

        # 첫 페이지는 end_date=None(최신), 이후 페이지는 직전 batch의 oldest 시각
        end_date_arg = None if page == 0 else current_end
        bars = api.get_bars(symbol, timeframe, count=count_per_page, end_date=end_date_arg)

        if not bars:
            logger.warning(f"  page {page+1}: 응답 없음. 종료.")
            break

        # 가장 오래된 봉 시각을 다음 페이지의 기준일자로 사용
        oldest_ts = min(b["timestamp"] for b in bars if "timestamp" in b)
        all_bars.extend(bars)

        # 중복 제거를 위해 다음 호출은 oldest_ts 직전까지
        new_end = oldest_ts - timedelta(seconds=1)
        if new_end >= current_end:
            logger.warning(f"  page {page+1}: 진행 없음 (서버가 동일 데이터 반환). 종료.")
            break
        current_end = new_end

        time.sleep(sleep_sec)

    # 중복 제거 + 시간순 정렬
    seen = set()
    unique = []
    for b in all_bars:
        ts = b.get("timestamp")
        if ts is None or ts in seen:
            continue
        seen.add(ts)
        unique.append(b)
    unique.sort(key=lambda b: b["timestamp"])

    logger.info(f"  [{symbol} {timeframe}] 총 {len(unique)} 고유 봉 수집 "
                f"({unique[0]['timestamp']:%Y-%m-%d %H:%M} ~ "
                f"{unique[-1]['timestamp']:%Y-%m-%d %H:%M})")
    return unique


# ── CSV 저장 (백테스트 호환 포맷) ─────────────────────────────────────────────

def save_csv(bars: List[Dict], output_path: str, timeframe: str):
    """백테스트가 읽는 CSV 포맷:
      date,open,high,low,close,volume
    - 1D: date는 'YYYY-MM-DD'
    - 1H 등 분봉: date는 'YYYY-MM-DD HH:MM:SS'
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    is_intraday = timeframe.upper() != "1D"

    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for b in bars:
            ts = b["timestamp"]
            date_str = ts.strftime("%Y-%m-%d %H:%M:%S") if is_intraday else ts.strftime("%Y-%m-%d")
            writer.writerow([
                date_str,
                f"{b['open']:.6f}",
                f"{b['high']:.6f}",
                f"{b['low']:.6f}",
                f"{b['close']:.6f}",
                int(b.get('volume', 0)),
            ])
    logger.info(f"  → 저장: {output_path} ({len(bars)} 봉)")


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="하나증권 분봉/일봉 데이터 추출기")
    parser.add_argument("--symbols", default="6A,6E",
                        help="콤마 구분 (예: '6A,6E' 또는 '6AM26,6EM26')")
    parser.add_argument("--timeframes", default="1H,4H,1D",
                        help="콤마 구분 (1H,4H,1D 등)")
    parser.add_argument("--count", type=int, default=900,
                        help="페이지당 봉 수 (기본 900)")
    parser.add_argument("--pages", type=int, default=20,
                        help="페이지네이션 최대 횟수 (기본 20 → 1H×900×20 = ~750일)")
    parser.add_argument("--sleep", type=float, default=0.4,
                        help="호출 간 대기 (초, 기본 0.4)")
    parser.add_argument("--config", default=CONFIG_PATH,
                        help=f"설정 파일 (기본 {CONFIG_PATH})")
    args = parser.parse_args()

    # config 로드
    if not os.path.exists(args.config):
        logger.error(f"설정 파일 없음: {args.config}")
        sys.exit(1)
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    api_cfg = cfg.get("api", {})
    api = HanaAPI(api_cfg["account_no"], api_cfg["account_pw"], api_cfg.get("is_mock", True))

    logger.info(f"하나증권 API 연결 시도 ({'모의' if api.is_mock else '실계좌'})...")
    if not api.connect(timeout=30):
        logger.error("API 연결 실패. 1QHTS가 실행 중이고 로그인되어 있는지 확인하세요.")
        sys.exit(1)

    symbols = [s.strip() for s in args.symbols.split(",")]
    timeframes = [tf.strip().upper() for tf in args.timeframes.split(",")]

    logger.info(f"추출 대상: {symbols} × {timeframes}")
    logger.info(f"페이지당 {args.count} 봉 × 최대 {args.pages} 페이지")

    for sym in symbols:
        full_sym = resolve_symbol(sym)
        logger.info(f"\n=== {sym} (=> {full_sym}) ===")

        for tf in timeframes:
            try:
                bars = extract_paginated(
                    api, full_sym, tf,
                    count_per_page=args.count,
                    max_pages=args.pages,
                    sleep_sec=args.sleep,
                )
                if not bars:
                    logger.warning(f"  {tf}: 데이터 없음")
                    continue

                # 출력 파일명: 백테스트가 읽는 패턴
                tf_suffix = "daily" if tf == "1D" else tf.lower()
                output_path = os.path.join(DATA_DIR, f"{sym}_{tf_suffix}.csv")
                save_csv(bars, output_path, tf)
            except Exception as e:
                logger.error(f"  {tf}: 실패 - {e}")
                continue

            # TF 사이에도 잠시 대기
            time.sleep(args.sleep * 2)

    api.disconnect()
    logger.info("\n=== 추출 완료 ===")
    logger.info(f"data/ 디렉토리 확인 후 git commit 또는 압축해서 전달해주세요.")


if __name__ == "__main__":
    main()
