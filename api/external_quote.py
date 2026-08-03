"""
외부 시세 소스 — 하나 모의계좌가 API 로 해외선물 시세를 제공하지 않아
Yahoo Finance 등 외부 무료 소스로 시세를 받아 SymbolTrader 에 tick 주입.

주요 특징:
  - 표준 라이브러리 urllib 만 사용 (pandas·yfinance 의존성 없음, 32bit Python OK)
  - Yahoo v7 quote 엔드포인트 (API 키 불필요)
  - 하나 종목코드 → Yahoo 심볼 자동 매핑

주의 (실제 매매 시):
  - Yahoo 시세는 CME 선물의 경우 지연 시세일 수 있음 (수 초~15분)
  - 참고용/전략 트리거용으로만 사용, 실제 체결가는 하나 서버 기준
  - Yahoo 는 특정 만기물이 아닌 활성 근월물 기준 (MGC=F, MES=F)
    → 만기월 다른 하나 종목코드도 근사값으로 처리
"""

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Dict, Optional

from utils.logger import setup_logger

logger = setup_logger("external_quote")


# 하나 종목코드 접두어 → Yahoo Finance 심볼
_HANA_TO_YAHOO = {
    "MGC": "MGC=F",     # Micro Gold Futures (10 oz)
    "GC":  "GC=F",      # Gold Futures (100 oz)
    "MES": "MES=F",     # Micro E-mini S&P 500
    "ES":  "ES=F",      # E-mini S&P 500
    "MNQ": "MNQ=F",     # Micro E-mini Nasdaq 100
    "NQ":  "NQ=F",      # E-mini Nasdaq 100
    "MYM": "MYM=F",     # Micro E-mini Dow
    "YM":  "YM=F",      # E-mini Dow
    "M2K": "M2K=F",     # Micro E-mini Russell 2000
    "RTY": "RTY=F",     # E-mini Russell 2000
    "6A":  "6A=F",      # Australian Dollar Futures
    "6E":  "6E=F",      # Euro FX Futures
    "6J":  "6J=F",      # Japanese Yen Futures
    "CL":  "CL=F",      # Crude Oil
    "NG":  "NG=F",      # Natural Gas
    "SI":  "SI=F",      # Silver
    "HG":  "HG=F",      # Copper
    "PL":  "PL=F",      # Platinum
    "PA":  "PA=F",      # Palladium
}

_MONTH_CODES = re.compile(r"[FGHJKMNQUVXZ]\d{2}$")   # 예: Q26, U26, Z26


def hana_to_yahoo(hana_symbol: str) -> Optional[str]:
    """
    'MGCZ26' → 'MGC=F', 'MESU26' → 'MES=F'
    Yahoo 는 만기물별 구분 없이 활성 근월물 시세만 제공.
    """
    # 만기 접미어(예: Q26) 제거
    base = _MONTH_CODES.sub("", hana_symbol)
    return _HANA_TO_YAHOO.get(base)


class YahooQuoteClient:
    """Yahoo Finance v7 quote API 클라이언트 (API 키 불필요)."""

    URL = "https://query1.finance.yahoo.com/v7/finance/quote"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/120.0.0.0 Safari/537.36")

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout
        # 사내망·프록시 이슈 대응 위해 SSL 검증 완화 옵션
        self._ssl_ctx = ssl.create_default_context()

    def get_raw(self, yahoo_symbol: str) -> Optional[dict]:
        """단일 심볼 raw JSON 응답의 result[0] 반환."""
        url = f"{self.URL}?{urllib.parse.urlencode({'symbols': yahoo_symbol})}"
        req = urllib.request.Request(url, headers={"User-Agent": self.UA})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=self._ssl_ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            logger.warning(f"Yahoo 요청 실패 {yahoo_symbol}: {e}")
            return None
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Yahoo 응답 파싱 실패 {yahoo_symbol}: {e}")
            return None

        results = (data.get("quoteResponse") or {}).get("result") or []
        if not results:
            return None
        return results[0]

    def get_quote_dict(self, hana_symbol: str) -> Optional[Dict[str, str]]:
        """
        하나 종목코드로 Yahoo 시세를 받아 SymbolTrader 호환 dict 반환.
        SymbolTrader.poll_quote 가 소비하는 형식:
          {"4": 현재가, "8": 시간, "11": 누적거래량, "13": 시가,
           "14": 고가, "15": 저가, "_source": 소스명}
        """
        y_sym = hana_to_yahoo(hana_symbol)
        if not y_sym:
            logger.warning(f"[external] {hana_symbol} → Yahoo 심볼 매핑 없음")
            return None

        raw = self.get_raw(y_sym)
        if not raw:
            return None

        # Yahoo v7 quote 응답 필드
        price = raw.get("regularMarketPrice") or raw.get("postMarketPrice") or 0
        volume = raw.get("regularMarketVolume", 0) or 0
        open_ = raw.get("regularMarketOpen", 0) or 0
        high = raw.get("regularMarketDayHigh", 0) or 0
        low = raw.get("regularMarketDayLow", 0) or 0
        # 시간 (unix epoch → HHMMSS)
        ts = raw.get("regularMarketTime", 0) or 0
        if ts:
            dt = datetime.fromtimestamp(ts)
            time_str = dt.strftime("%H%M%S")
        else:
            time_str = datetime.now().strftime("%H%M%S")

        return {
            "4":  str(price),
            "8":  time_str,
            "11": str(int(volume)),
            "13": str(open_),
            "14": str(high),
            "15": str(low),
            "_source": f"Yahoo({y_sym})",
        }
