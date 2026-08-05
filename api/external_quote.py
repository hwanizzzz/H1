"""
외부 시세 소스 — 하나 모의계좌가 API 로 해외선물 시세를 제공하지 않을 때
Yahoo Finance / stooq.com 등 무료 소스로 시세를 받아 pseudo-tick 주입.

여러 백업 소스를 순차 시도 (Yahoo 가 401 등 실패해도 stooq 로 폴백):
  1) Yahoo v8 chart endpoint (쿠키 없이 대부분 접근 가능)
  2) Yahoo v7 quote endpoint (구식, 최근 401 자주)
  3) stooq.com CSV (폴란드 무료 시세, 단순)
"""

import csv
import io
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
    "MGC": "MGC=F", "GC":  "GC=F",
    "MES": "MES=F", "ES":  "ES=F",
    "MNQ": "MNQ=F", "NQ":  "NQ=F",
    "MYM": "MYM=F", "YM":  "YM=F",
    "M2K": "M2K=F", "RTY": "RTY=F",
    "6A":  "6A=F",  "6E":  "6E=F",  "6J":  "6J=F",
    "CL":  "CL=F",  "NG":  "NG=F",
    "SI":  "SI=F",  "HG":  "HG=F",
    "PL":  "PL=F",  "PA":  "PA=F",
}

# 하나 종목코드 접두어 → stooq 심볼 (소문자 + .f 접미어)
_HANA_TO_STOOQ = {
    "MGC": "mgc.f", "GC":  "gc.f",
    "MES": "mes.f", "ES":  "es.f",
    "MNQ": "mnq.f", "NQ":  "nq.f",
    "MYM": "mym.f", "YM":  "ym.f",
    "M2K": "m2k.f", "RTY": "rty.f",
    "CL":  "cl.f",  "NG":  "ng.f",
    "SI":  "si.f",  "HG":  "hg.f",
    "PL":  "pl.f",  "PA":  "pa.f",
}

_MONTH_CODES = re.compile(r"[FGHJKMNQUVXZ]\d{2}$")


def base_symbol(hana_symbol: str) -> str:
    """MGCZ26 → MGC 등 만기 접미어 제거."""
    return _MONTH_CODES.sub("", hana_symbol)


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")


def _http_get(url: str, timeout: float = 5.0) -> Optional[str]:
    """간단 HTTP GET. 실패 시 None. 401/403 등 상태코드도 실패로."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
    })
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        logger.warning(f"HTTP {e.code} {url[:80]}")
        return None
    except (urllib.error.URLError, Exception) as e:
        logger.warning(f"URL 오류 {url[:80]}: {e}")
        return None


class YahooQuoteClient:
    """Yahoo Finance 다중 엔드포인트 + stooq 폴백."""

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    # ── 소스별 조회 ──────────────────────────────────────────────────────────

    def _yahoo_v8_chart(self, hana_symbol: str) -> Optional[Dict[str, str]]:
        y_sym = _HANA_TO_YAHOO.get(base_symbol(hana_symbol))
        if not y_sym:
            return None
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{urllib.parse.quote(y_sym)}?range=1d&interval=1m")
        body = _http_get(url, self.timeout)
        if not body:
            return None
        try:
            data = json.loads(body)
            result = data.get("chart", {}).get("result") or []
            if not result:
                return None
            r = result[0]
            meta = r.get("meta", {})
            price = meta.get("regularMarketPrice") or meta.get("previousClose") or 0
            volume = 0
            open_ = meta.get("chartPreviousClose", 0) or 0
            # 최근 1분봉 통계 (있으면)
            quote = (r.get("indicators", {}).get("quote") or [{}])[0]
            if quote.get("high"):
                high_arr = [h for h in quote["high"] if h is not None]
                low_arr = [l for l in quote["low"] if l is not None]
                open_arr = [o for o in quote["open"] if o is not None]
                vol_arr = [v for v in quote.get("volume", []) if v is not None]
                high = max(high_arr) if high_arr else 0
                low = min(low_arr) if low_arr else 0
                open_ = open_arr[0] if open_arr else open_
                volume = sum(vol_arr) if vol_arr else 0
            else:
                high = meta.get("regularMarketDayHigh", 0) or 0
                low = meta.get("regularMarketDayLow", 0) or 0
            ts = meta.get("regularMarketTime", 0) or 0
            time_str = (datetime.fromtimestamp(ts).strftime("%H%M%S")
                        if ts else datetime.now().strftime("%H%M%S"))
            return {
                "4":  str(price),
                "8":  time_str,
                "11": str(int(volume)),
                "13": str(open_),
                "14": str(high),
                "15": str(low),
                "_source": f"Yahoo_v8({y_sym})",
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning(f"Yahoo v8 파싱 오류: {e}")
            return None

    def _stooq_csv(self, hana_symbol: str) -> Optional[Dict[str, str]]:
        s_sym = _HANA_TO_STOOQ.get(base_symbol(hana_symbol))
        if not s_sym:
            return None
        url = (f"https://stooq.com/q/l/?s={s_sym}"
               f"&f=sd2t2ohlcv&h&e=csv")
        body = _http_get(url, self.timeout)
        if not body:
            return None
        try:
            reader = csv.DictReader(io.StringIO(body))
            for row in reader:
                # 컬럼: Symbol, Date, Time, Open, High, Low, Close, Volume
                close_ = row.get("Close", "0") or "0"
                if close_ in ("N/D", "", "0"):
                    continue
                time_str = (row.get("Time", "") or "").replace(":", "")[:6]
                return {
                    "4":  close_,
                    "8":  time_str or datetime.now().strftime("%H%M%S"),
                    "11": row.get("Volume", "0") or "0",
                    "13": row.get("Open", "0") or "0",
                    "14": row.get("High", "0") or "0",
                    "15": row.get("Low", "0") or "0",
                    "_source": f"stooq({s_sym})",
                }
        except Exception as e:
            logger.warning(f"stooq 파싱 오류: {e}")
        return None

    # ── 공용 진입점 ───────────────────────────────────────────────────────────

    def get_quote_dict(self, hana_symbol: str) -> Optional[Dict[str, str]]:
        """여러 소스 순차 시도. 첫 성공 반환."""
        for name, fn in [("Yahoo v8", self._yahoo_v8_chart),
                          ("stooq",    self._stooq_csv)]:
            try:
                q = fn(hana_symbol)
                if q and (q.get("4") or "").replace(".", "").replace(",", "").strip():
                    return q
            except Exception as e:
                logger.warning(f"{name} 예외 {hana_symbol}: {e}")
        logger.warning(f"[external] {hana_symbol} 모든 외부 소스 실패")
        return None

    # 하위호환 (예전 이름)
    def get_raw(self, y_sym: str):
        return None
