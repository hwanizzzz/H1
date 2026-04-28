"""
하나증권 1Q Open API 래퍼

요구사항:
  - Windows OS (COM 기반 API)
  - 하나증권 HTS(1QHTS) 설치 및 로그인
  - pywin32 (pip install pywin32)

API 등록 경로:
  1QHTS → 화면번호 0789 → API 등록 → 해외파생 신청
"""

import sys
import time
import threading
from datetime import datetime
from typing import Callable, Optional, Dict, List
from utils.logger import setup_logger

logger = setup_logger("hana_api")

# Windows COM 환경 체크
_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    try:
        import win32com.client
        import pythoncom
        _COM_AVAILABLE = True
    except ImportError:
        _COM_AVAILABLE = False
        logger.warning("pywin32 미설치. pip install pywin32 후 재시도하세요.")
else:
    _COM_AVAILABLE = False
    logger.warning("Windows 환경이 아닙니다. API 기능이 제한됩니다.")


# ── 상수 ─────────────────────────────────────────────────────────────────────

# 하나증권 1Q Open API COM ProgID (실제 설치 후 레지스트리에서 확인)
HANA_PROGID = "H1OPENAPI.H1OPENAPICtrl.1"

# 주문 유형
ORDER_BUY = 1    # 매수
ORDER_SELL = 2   # 매도

# 주문 방식
ORDER_MARKET = "03"   # 시장가
ORDER_LIMIT  = "00"   # 지정가

# 거래소 코드 (해외선물)
EXCHANGE_CME = "CME"

# TR 코드 (하나증권 1Q API)
TR_OVERSEAS_FUTURES_CHART = "OFHDFUT0"   # 해외선물 일봉 데이터
TR_OVERSEAS_FUTURES_MIN   = "OFHDFUT2"   # 해외선물 분봉 데이터 (1H/4H/30M 등)
TR_OVERSEAS_FUTURES_TICK  = "OFHDFUT1"   # 해외선물 틱 데이터
TR_ACCOUNT_BALANCE        = "OFHACNT0"   # 해외선물 잔고 조회
TR_POSITION               = "OFHPOSI0"   # 포지션 조회

# 타임프레임 → 분 단위 환산 (Hana 분봉 TR의 "주기" 입력값)
TIMEFRAME_TO_MINUTES = {
    "1M": 1, "5M": 5, "15M": 15, "30M": 30,
    "1H": 60, "2H": 120, "4H": 240,
}


# ── 데이터 클래스 ─────────────────────────────────────────────────────────────

class OrderResult:
    def __init__(self, success: bool, order_no: str = "", msg: str = ""):
        self.success = success
        self.order_no = order_no
        self.msg = msg

    def __repr__(self):
        return f"OrderResult(success={self.success}, order_no={self.order_no}, msg={self.msg})"


class Position:
    def __init__(self, symbol: str, qty: int, avg_price: float,
                 side: str, unrealized_pnl: float = 0.0):
        self.symbol = symbol
        self.qty = qty           # 보유 계약 수 (양수)
        self.avg_price = avg_price
        self.side = side         # "LONG" or "SHORT"
        self.unrealized_pnl = unrealized_pnl

    def __repr__(self):
        return (f"Position({self.symbol} {self.side} {self.qty}계약 "
                f"평단={self.avg_price:.5f} 평가손익={self.unrealized_pnl:,.0f}원)")


class AccountInfo:
    def __init__(self, total_equity: float, available_margin: float,
                 used_margin: float, realized_pnl: float):
        self.total_equity = total_equity
        self.available_margin = available_margin
        self.used_margin = used_margin
        self.realized_pnl = realized_pnl


# ── 메인 API 클래스 ───────────────────────────────────────────────────────────

class HanaAPI:
    """
    하나증권 1Q Open API COM 인터페이스 래퍼.

    이벤트 콜백:
      on_login(is_success: bool)
      on_bar_data(symbol: str, bars: list[dict])
      on_tick(symbol: str, price: float, volume: int, timestamp: datetime)
      on_order_filled(order_no: str, symbol: str, qty: int, price: float, side: str)
    """

    def __init__(self, account_no: str, account_pw: str, is_mock: bool = True):
        self.account_no = account_no
        self.account_pw = account_pw
        self.is_mock = is_mock

        self._api = None
        self._connected = False
        self._login_event = threading.Event()

        # 콜백
        self.on_login: Optional[Callable[[bool], None]] = None
        self.on_bar_data: Optional[Callable[[str, List[dict]], None]] = None
        self.on_tick: Optional[Callable[[str, float, int, datetime], None]] = None
        self.on_order_filled: Optional[Callable[[str, str, int, float, str], None]] = None

        # TR 응답 저장 (동기 요청용)
        self._tr_result: Dict = {}
        self._tr_event = threading.Event()

    # ── 연결 / 로그인 ─────────────────────────────────────────────────────────

    def connect(self, timeout: int = 30) -> bool:
        """API 초기화 및 HTS 연결"""
        if not _COM_AVAILABLE:
            logger.error("COM API를 사용할 수 없습니다. Windows + pywin32 환경을 확인하세요.")
            return False

        try:
            pythoncom.CoInitialize()
            self._api = win32com.client.Dispatch(HANA_PROGID)

            # 이벤트 핸들러 연결
            self._api = win32com.client.WithEvents(self._api, _HanaEventHandler)
            self._api._handler._parent = self

            logger.info(f"하나증권 1Q API 초기화 완료 ({'모의투자' if self.is_mock else '실계좌'})")

            # 자동 로그인 (HTS가 이미 로그인된 상태여야 함)
            ret = self._api.CommConnect()
            if ret == 0:
                logger.info("API 서버 연결 요청 전송")
            else:
                logger.error(f"연결 요청 실패: {ret}")
                return False

            # 로그인 완료 대기
            if self._login_event.wait(timeout=timeout):
                return self._connected
            else:
                logger.error(f"로그인 타임아웃 ({timeout}초)")
                return False

        except Exception as e:
            logger.error(f"API 연결 오류: {e}")
            return False

    def disconnect(self):
        if self._api:
            try:
                self._api.CommTerminate()
            except Exception:
                pass
        self._connected = False
        logger.info("API 연결 해제")

    def is_connected(self) -> bool:
        return self._connected

    # ── 시세 데이터 조회 ─────────────────────────────────────────────────────

    def get_daily_bars(self, symbol: str, count: int = 200) -> List[dict]:
        """
        해외선물 일봉 데이터 조회.
        반환: [{"date": "20250101", "open": 0.65, "high": 0.66, "low": 0.64, "close": 0.655, "volume": 1234}, ...]
        """
        if not self._connected:
            logger.error("API 미연결 상태")
            return []

        self._tr_event.clear()
        self._api.SetInputValue("종목코드", symbol)
        self._api.SetInputValue("조회수", str(count))
        self._api.CommRqData(TR_OVERSEAS_FUTURES_CHART, TR_OVERSEAS_FUTURES_CHART,
                             0, self._get_screen_no())

        if self._tr_event.wait(timeout=10):
            return self._tr_result.get("bars", [])
        else:
            logger.error("일봉 데이터 조회 타임아웃")
            return []

    def get_bars(self, symbol: str, timeframe: str = "1D", count: int = 200) -> List[dict]:
        """
        해외선물 봉 데이터 조회 (일봉 + 분봉 통합 인터페이스).

        Args:
            symbol:    종목코드 (예: "6AH26")
            timeframe: "1D" 일봉 / "1M","5M","15M","30M","1H","2H","4H" 분봉
            count:     조회 봉 수

        반환: [{"timestamp": datetime, "open":..., "high":..., "low":..., "close":..., "volume":...}, ...]
        """
        timeframe = timeframe.upper()

        # 일봉은 기존 메서드 재사용 후 timestamp 정규화
        if timeframe == "1D":
            raw = self.get_daily_bars(symbol, count)
            normalized = []
            for b in raw:
                try:
                    ts = datetime.strptime(b["date"], "%Y%m%d")
                except Exception:
                    continue
                normalized.append({
                    "timestamp": ts,
                    "open": b["open"], "high": b["high"],
                    "low": b["low"], "close": b["close"],
                    "volume": b["volume"],
                })
            return normalized

        # 분봉
        if timeframe not in TIMEFRAME_TO_MINUTES:
            logger.error(f"지원하지 않는 timeframe: {timeframe}")
            return []

        if not self._connected:
            logger.error("API 미연결 상태")
            return []

        minutes = TIMEFRAME_TO_MINUTES[timeframe]
        self._tr_event.clear()
        self._api.SetInputValue("종목코드", symbol)
        self._api.SetInputValue("주기", str(minutes))
        self._api.SetInputValue("조회수", str(count))
        self._api.CommRqData(TR_OVERSEAS_FUTURES_MIN, TR_OVERSEAS_FUTURES_MIN,
                             0, self._get_screen_no())

        if self._tr_event.wait(timeout=15):
            return self._tr_result.get("bars", [])
        else:
            logger.error(f"{timeframe} 분봉 데이터 조회 타임아웃")
            return []

    def subscribe_realtime(self, symbol: str):
        """실시간 체결 데이터 구독"""
        if not self._connected:
            return
        screen_no = self._get_screen_no()
        self._api.SetRealReg(screen_no, symbol, "10;15;20", "0")
        logger.info(f"실시간 구독 등록: {symbol}")

    def unsubscribe_realtime(self, symbol: str):
        screen_no = self._get_screen_no()
        self._api.SetRealRemove(screen_no, symbol)
        logger.info(f"실시간 구독 해제: {symbol}")

    # ── 주문 ─────────────────────────────────────────────────────────────────

    def send_order(self, symbol: str, side: int, qty: int,
                   price: float = 0.0, order_type: str = ORDER_MARKET) -> OrderResult:
        """
        해외선물 주문 전송.

        Args:
            symbol:     종목코드 (예: "6AH25")
            side:       ORDER_BUY(1) or ORDER_SELL(2)
            qty:        계약 수 (양수)
            price:      지정가 주문 시 가격 (시장가는 0)
            order_type: ORDER_MARKET("03") or ORDER_LIMIT("00")

        Returns:
            OrderResult
        """
        if not self._connected:
            return OrderResult(False, msg="API 미연결")

        if qty <= 0:
            return OrderResult(False, msg=f"잘못된 수량: {qty}")

        side_str = "매수" if side == ORDER_BUY else "매도"
        logger.info(f"주문 전송: {symbol} {side_str} {qty}계약 "
                    f"{'시장가' if order_type == ORDER_MARKET else f'지정가 {price:.5f}'}")

        try:
            ret = self._api.SendOrder(
                "해외선물주문",          # 화면명 (임의)
                self._get_screen_no(),   # 화면번호
                self.account_no,         # 계좌번호
                side,                    # 주문구분 (1:매수, 2:매도)
                symbol,                  # 종목코드
                qty,                     # 주문수량
                price,                   # 주문가격 (시장가=0)
                order_type,              # 거래구분
                ""                       # 원주문번호 (신규=공백)
            )
            if ret == 0:
                logger.info(f"주문 접수 성공")
                return OrderResult(True, msg="주문 접수")
            else:
                logger.error(f"주문 실패 코드: {ret}")
                return OrderResult(False, msg=f"주문 실패 (코드: {ret})")
        except Exception as e:
            logger.error(f"주문 전송 예외: {e}")
            return OrderResult(False, msg=str(e))

    def send_market_buy(self, symbol: str, qty: int) -> OrderResult:
        return self.send_order(symbol, ORDER_BUY, qty, order_type=ORDER_MARKET)

    def send_market_sell(self, symbol: str, qty: int) -> OrderResult:
        return self.send_order(symbol, ORDER_SELL, qty, order_type=ORDER_MARKET)

    def send_limit_buy(self, symbol: str, qty: int, price: float) -> OrderResult:
        return self.send_order(symbol, ORDER_BUY, qty, price, ORDER_LIMIT)

    def send_limit_sell(self, symbol: str, qty: int, price: float) -> OrderResult:
        return self.send_order(symbol, ORDER_SELL, qty, price, ORDER_LIMIT)

    # ── 계좌/포지션 조회 ─────────────────────────────────────────────────────

    def get_account_info(self) -> Optional[AccountInfo]:
        if not self._connected:
            return None

        self._tr_event.clear()
        self._api.SetInputValue("계좌번호", self.account_no)
        self._api.SetInputValue("비밀번호", self.account_pw)
        self._api.CommRqData(TR_ACCOUNT_BALANCE, TR_ACCOUNT_BALANCE,
                             0, self._get_screen_no())

        if self._tr_event.wait(timeout=10):
            return self._tr_result.get("account_info")
        return None

    def get_positions(self) -> List[Position]:
        if not self._connected:
            return []

        self._tr_event.clear()
        self._api.SetInputValue("계좌번호", self.account_no)
        self._api.SetInputValue("비밀번호", self.account_pw)
        self._api.CommRqData(TR_POSITION, TR_POSITION, 0, self._get_screen_no())

        if self._tr_event.wait(timeout=10):
            return self._tr_result.get("positions", [])
        return []

    def get_position(self, symbol: str) -> Optional[Position]:
        positions = self.get_positions()
        for p in positions:
            if p.symbol == symbol:
                return p
        return None

    # ── 내부 유틸 ─────────────────────────────────────────────────────────────

    _screen_counter = 1000

    def _get_screen_no(self) -> str:
        HanaAPI._screen_counter += 1
        if HanaAPI._screen_counter > 9999:
            HanaAPI._screen_counter = 1001
        return str(HanaAPI._screen_counter)

    def _on_login(self, err_code: int):
        if err_code == 0:
            self._connected = True
            logger.info("로그인 성공")
            if self.on_login:
                self.on_login(True)
        else:
            self._connected = False
            logger.error(f"로그인 실패 (코드: {err_code})")
            if self.on_login:
                self.on_login(False)
        self._login_event.set()

    def _on_receive_tr_data(self, screen_no: str, rq_name: str, tr_code: str,
                             record_name: str, prev_next: str):
        """TR 데이터 수신 이벤트 처리"""
        try:
            if tr_code == TR_OVERSEAS_FUTURES_CHART:
                bars = self._parse_daily_bars(tr_code)
                self._tr_result = {"bars": bars}
                self._tr_event.set()

            elif tr_code == TR_OVERSEAS_FUTURES_MIN:
                bars = self._parse_minute_bars(tr_code)
                self._tr_result = {"bars": bars}
                self._tr_event.set()

            elif tr_code == TR_ACCOUNT_BALANCE:
                info = self._parse_account_info(tr_code)
                self._tr_result = {"account_info": info}
                self._tr_event.set()

            elif tr_code == TR_POSITION:
                positions = self._parse_positions(tr_code)
                self._tr_result = {"positions": positions}
                self._tr_event.set()

        except Exception as e:
            logger.error(f"TR 데이터 파싱 오류: {e}")
            self._tr_event.set()

    def _on_receive_real_data(self, real_key: str, real_type: str, real_data: str):
        """실시간 체결 데이터 처리"""
        try:
            if real_type == "해외선물체결":
                price = float(self._api.GetCommRealData(real_key, 10))   # 현재가
                volume = int(self._api.GetCommRealData(real_key, 15))    # 체결량
                ts = datetime.now()
                if self.on_tick:
                    self.on_tick(real_key, price, volume, ts)
        except Exception as e:
            logger.error(f"실시간 데이터 파싱 오류: {e}")

    def _on_receive_chejan_data(self, gubun: str, item_cnt: int, fid_list: str):
        """주문/체결 이벤트 처리 (gubun: '0'=주문, '1'=잔고)"""
        try:
            if gubun == "1":
                order_no = self._api.GetChejanData(9203)
                symbol   = self._api.GetChejanData(9001)
                qty      = int(self._api.GetChejanData(911))
                price    = float(self._api.GetChejanData(910))
                side     = self._api.GetChejanData(907)
                side_str = "BUY" if "매수" in side else "SELL"
                logger.info(f"체결: {symbol} {side_str} {qty}계약 @ {price:.5f} (주문번호: {order_no})")
                if self.on_order_filled:
                    self.on_order_filled(order_no, symbol, qty, price, side_str)
        except Exception as e:
            logger.error(f"체결 데이터 파싱 오류: {e}")

    def _parse_daily_bars(self, tr_code: str) -> List[dict]:
        bars = []
        try:
            count = self._api.GetRepeatCnt(tr_code, "일봉데이터")
            for i in range(count):
                bars.append({
                    "date":   self._api.GetCommData(tr_code, "일봉데이터", i, "일자").strip(),
                    "open":   float(self._api.GetCommData(tr_code, "일봉데이터", i, "시가")),
                    "high":   float(self._api.GetCommData(tr_code, "일봉데이터", i, "고가")),
                    "low":    float(self._api.GetCommData(tr_code, "일봉데이터", i, "저가")),
                    "close":  float(self._api.GetCommData(tr_code, "일봉데이터", i, "현재가")),
                    "volume": int(self._api.GetCommData(tr_code, "일봉데이터", i, "거래량")),
                })
        except Exception as e:
            logger.error(f"일봉 파싱 오류: {e}")
        return bars

    def _parse_minute_bars(self, tr_code: str) -> List[dict]:
        """분봉 TR 응답 파싱.
        반환 형식: [{"timestamp": datetime, "open":..., ...}]
        Hana 분봉 TR이 "체결시간"을 "YYYYMMDDHHMMSS"로 반환한다고 가정.
        """
        bars = []
        try:
            count = self._api.GetRepeatCnt(tr_code, "분봉데이터")
            for i in range(count):
                ts_str = self._api.GetCommData(tr_code, "분봉데이터", i, "체결시간").strip()
                try:
                    ts = datetime.strptime(ts_str[:14], "%Y%m%d%H%M%S")
                except Exception:
                    ts = datetime.strptime(ts_str[:8], "%Y%m%d")
                bars.append({
                    "timestamp": ts,
                    "open":      float(self._api.GetCommData(tr_code, "분봉데이터", i, "시가")),
                    "high":      float(self._api.GetCommData(tr_code, "분봉데이터", i, "고가")),
                    "low":       float(self._api.GetCommData(tr_code, "분봉데이터", i, "저가")),
                    "close":     float(self._api.GetCommData(tr_code, "분봉데이터", i, "현재가")),
                    "volume":    int(self._api.GetCommData(tr_code, "분봉데이터", i, "거래량")),
                })
        except Exception as e:
            logger.error(f"분봉 파싱 오류: {e}")
        return bars

    def _parse_account_info(self, tr_code: str) -> AccountInfo:
        return AccountInfo(
            total_equity=float(self._api.GetCommData(tr_code, "잔고", 0, "평가자산").replace(",", "")),
            available_margin=float(self._api.GetCommData(tr_code, "잔고", 0, "가용증거금").replace(",", "")),
            used_margin=float(self._api.GetCommData(tr_code, "잔고", 0, "사용증거금").replace(",", "")),
            realized_pnl=float(self._api.GetCommData(tr_code, "잔고", 0, "실현손익").replace(",", "")),
        )

    def _parse_positions(self, tr_code: str) -> List[Position]:
        positions = []
        try:
            count = self._api.GetRepeatCnt(tr_code, "포지션")
            for i in range(count):
                qty = int(self._api.GetCommData(tr_code, "포지션", i, "보유수량"))
                if qty == 0:
                    continue
                positions.append(Position(
                    symbol=self._api.GetCommData(tr_code, "포지션", i, "종목코드").strip(),
                    qty=abs(qty),
                    avg_price=float(self._api.GetCommData(tr_code, "포지션", i, "평균단가")),
                    side="LONG" if qty > 0 else "SHORT",
                    unrealized_pnl=float(
                        self._api.GetCommData(tr_code, "포지션", i, "평가손익").replace(",", "")
                    ),
                ))
        except Exception as e:
            logger.error(f"포지션 파싱 오류: {e}")
        return positions


# ── COM 이벤트 핸들러 ─────────────────────────────────────────────────────────

class _HanaEventHandler:
    """pywin32 WithEvents 이벤트 핸들러 클래스"""

    def __init__(self):
        self._parent: Optional[HanaAPI] = None

    def OnEventConnect(self, err_code: int):
        if self._parent:
            self._parent._on_login(err_code)

    def OnReceiveTrData(self, screen_no, rq_name, tr_code, record_name, prev_next,
                        data_len, err_code, msg, sple_msg):
        if self._parent:
            self._parent._on_receive_tr_data(screen_no, rq_name, tr_code, record_name, prev_next)

    def OnReceiveRealData(self, real_key, real_type, real_data):
        if self._parent:
            self._parent._on_receive_real_data(real_key, real_type, real_data)

    def OnReceiveChejanData(self, gubun, item_cnt, fid_list):
        if self._parent:
            self._parent._on_receive_chejan_data(gubun, item_cnt, fid_list)

    def OnReceiveMsg(self, screen_no, rq_name, tr_code, msg):
        logger.info(f"API 메시지 [{tr_code}]: {msg}")
