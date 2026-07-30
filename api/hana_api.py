"""
하나증권 Open API (1Q Pro) — Python COM 래퍼

기반 스펙:
  - 하나증권 Open API 개발자가이드 v3.1 (2025-06-09)
  - ProgID: HFCOMMAGENT.HFCommAgentCtrl.1
  - 32비트 Python + 32비트 OCX + Windows 필수

두 계층 구조:
  - HanaOpenAPI    : 저수준 COM 래퍼(CommInit, RequestTran, RegisterReal ...)
  - HanaFuturesClient : 해외선물 매매 상위 API (send_order, get_positions ...)

주요 흐름:
  1) api = HanaOpenAPI(); api.connect(open_api_root)
  2) api.login_cloud_cert(user_id, mode=OVERSEAS_MOCK)   # 해외모의
  3) client = HanaFuturesClient(api, account_no, account_pw)
  4) client.subscribe_tick("MGCQ26")   # V10 실시간 구독
  5) client.send_order("MGCQ26", "BUY", 1, prc_type="MARKET")
"""

import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from utils.logger import setup_logger

logger = setup_logger("hana_api")

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

HANA_PROGID = "HFCOMMAGENT.HFCommAgentCtrl.1"

# SetLoginMode(nOption=0, nMode)
LOGIN_MODE_DOMESTIC_OVERSEAS_LIVE = 0   # 국내/해외 실거래
LOGIN_MODE_DOMESTIC_MOCK          = 1   # 국내 모의
LOGIN_MODE_OVERSEAS_MOCK          = 2   # 해외 모의
LOGIN_MODE_DOMESTIC_LIVE          = 3   # 국내 실거래

# GetCommRecvOptionValue(nOptionType)
OPT_TR_CODE       = 0
OPT_PREV_NEXT_CODE = 1
OPT_PREV_NEXT_KEY = 2
OPT_MSG_CODE      = 3
OPT_MSG           = 4
OPT_SUB_MSG_CODE  = 5
OPT_SUB_MSG       = 6
OPT_ERROR         = 7
OPT_SCREEN_NO     = 8

# 해외선물 서비스 코드
TR_ACCOUNT_LIST   = "HHTACCNM01"    # 계좌목록조회
TR_ORDER_NEW      = "OTS5901U01"    # 해외선물 일반주문(매수/매도)
TR_ORDER_MODIFY   = "OTS5901U02"    # 정정주문
TR_ORDER_CANCEL   = "OTS5901U03"    # 취소주문
TR_POSITIONS      = "OTS5919Q41"    # 미결제약정현황(포지션)
TR_FILLED         = "OTS5911Q52"    # 체결내역
TR_UNFILLED       = "OTS5911Q41"    # 미체결내역
TR_ORDERS_ALL     = "OTS5921Q41"    # 전체(체결+미체결)
TR_DEPOSIT        = "OTS5943Q01"    # 예수금·증거금·통화별잔고

REAL_TICK       = "V10"    # 해외선물 체결 (시세)
REAL_ORDERBOOK  = "V11"    # 해외선물 호가
REAL_EXEC       = "EF1"    # 주문체결 통보
REAL_OPEN_POS   = "EF2"    # 미결제 통보
REAL_UNFILLED   = "EF4"    # 미체결(정정/취소) 통보


# ── 데이터 클래스 ─────────────────────────────────────────────────────────────

@dataclass
class OrderResult:
    success: bool
    order_no: str = ""
    msg: str = ""

    def __repr__(self):
        return f"OrderResult(success={self.success}, order_no={self.order_no}, msg={self.msg})"


@dataclass
class Position:
    symbol: str            # PRDT_CD
    side: str              # "LONG"(B) / "SHORT"(S)
    qty: int               # USTL_CTRC_QNT (미결제약정수량)
    avg_price: float       # TRDE_AVR_UNPR
    current_price: float   # ODRV_NOW_PRC
    eval_pnl: float        # ODRV_EVL_PFLS_AMT (평가손익, 통화)
    currency: str = ""     # CRRY_CD

    def __repr__(self):
        return (f"Position({self.symbol} {self.side} {self.qty}계약 "
                f"평단={self.avg_price:.5f} 평가손익={self.eval_pnl:+.2f} {self.currency})")


@dataclass
class AccountInfo:
    total_asset_value: float      # TOT_ACC_ASST_VALU_AMT (총계정자산가치)
    cash_balance: float           # THDT_CSH_BLCE (당일현금잔액)
    order_available: float        # ODRV_ORDR_PSBL_AMT (주문가능금액)
    maintenance_margin: float     # ODRV_MNTN_WMY (유지증거금)
    withdrawable: float           # ODRV_WDRW_PSBL_AMT
    currency: str = "USD"


# ── 저수준 COM 래퍼 ───────────────────────────────────────────────────────────

class HanaOpenAPI:
    """
    HFCOMMAGENT.HFCommAgentCtrl.1 저수준 COM 래퍼.

    Tran 조회 패턴 (동기 request):
        rows = api.request_tran(
            tr_code="OTS5919Q41",
            in_records=[
                ("OTS5919Q41_in",       {"ODRV_SELL_BUY_DCD": ""}),
                ("OTS5919Q41_in_sub01", {"CTNO": ctno, "APNO": apno, "PWD": pw_enc}),
            ],
            out_record="OTS5919Q41_out_sub01",
            fields=["PRDT_CD", "ODRV_SELL_BUY_DCD", "USTL_CTRC_QNT", ...],
        )

    실시간 구독:
        api.register_real("V10", "MGCQ26")
        api.on_real_data = lambda name, key: ...   # OnGetRealData 시 호출
    """

    def __init__(self, progid: Optional[str] = None):
        self.progid = progid or HANA_PROGID
        self._api = None
        self._connected = False

        # 사용자 콜백
        self.on_login: Optional[Callable[[bool], None]] = None
        self.on_real_data: Optional[Callable[[str, str], None]] = None
        self.on_agent_event: Optional[Callable[[int, int, str], None]] = None

        # 동기 Tran 요청 관리
        self._pending_specs: Dict[int, dict] = {}   # rq_id → {out_record, fields, tr_code}
        self._pending_events: Dict[int, threading.Event] = {}
        self._tran_results: Dict[int, List[Dict[str, str]]] = {}
        self._tran_errors: Dict[int, str] = {}

    # ── 연결 / 리소스 로드 ────────────────────────────────────────────────────

    def connect(self, openapi_root: str) -> bool:
        """
        COM 컨트롤 생성 → CommInit → 리소스 파일 자동 로드.

        openapi_root: 하나 OpenAPI 설치 루트 (예: "C:\\1QOpenAPI\\1Q OpenAPI")
                      아래에 TranRes/, RealRes/ 폴더가 있어야 함.
        """
        if not _COM_AVAILABLE:
            logger.error("COM API 사용 불가 (Windows + pywin32 확인).")
            return False

        try:
            pythoncom.CoInitialize()
            logger.info(f"COM Dispatch: {self.progid}")
            self._api = win32com.client.DispatchWithEvents(self.progid, _HanaEventHandler)
            self._api._parent = self
            logger.info("COM 컨트롤 생성 성공")
        except Exception as e:
            logger.error(f"COM Dispatch 실패: {e}. ProgID 또는 32비트 Python 확인.")
            return False

        try:
            self._api.SetOffAgentMessageBox(1)   # 팝업 방지
        except Exception:
            pass

        ret = self._api.CommInit()
        if ret != 0:
            logger.error(f"CommInit 실패 (ret={ret}): {self._last_err()}")
            return False
        logger.info("CommInit 성공")

        self._load_all_resources(openapi_root)
        return True

    def _load_all_resources(self, openapi_root: str):
        """openapi_root/TranRes, openapi_root/RealRes 아래 *.res 전부 로드."""
        for sub, loader in [("TranRes", self._api.LoadTranResource),
                            ("RealRes", self._api.LoadRealResource)]:
            dpath = os.path.join(openapi_root, sub)
            if not os.path.isdir(dpath):
                logger.warning(f"리소스 폴더 없음: {dpath}")
                continue
            n = 0
            for fname in os.listdir(dpath):
                if fname.lower().endswith(".res"):
                    fpath = os.path.join(dpath, fname)
                    ret = loader(fpath)
                    if ret == 1:
                        n += 1
                    else:
                        logger.warning(f"{sub} 로드 실패: {fname}")
            logger.info(f"{sub} {n}개 리소스 로드")

    def disconnect(self):
        if self._api:
            try:
                self._api.CommTerminate(1)
            except Exception:
                pass
        self._connected = False
        logger.info("API 연결 해제")

    # ── 로그인 ─────────────────────────────────────────────────────────────────

    def set_login_mode(self, mode: int):
        """mode: 0=국내/해외실거래, 1=국내모의, 2=해외모의, 3=국내실거래"""
        self._api.SetLoginMode(0, mode)
        logger.info(f"SetLoginMode(0, {mode})")

    def login(self, user_id: str, password: str, cert_pw: str) -> bool:
        """공동인증서 로그인. 반환: 1=성공, 0=실패."""
        ret = self._api.CommLogin(user_id, password, cert_pw)
        ok = (ret == 1)
        self._connected = ok
        if ok:
            logger.info("공동인증서 로그인 성공")
        else:
            logger.error(f"공동인증서 로그인 실패: {self._last_err()}")
        if self.on_login:
            self.on_login(ok)
        return ok

    def login_cloud_cert(self, user_id: str) -> bool:
        """클라우드인증서 로그인. 반환: 1=성공, 0=실패."""
        ret = self._api.CommCloudCert(user_id)
        ok = (ret == 1)
        self._connected = ok
        if ok:
            logger.info("클라우드인증서 로그인 성공")
        else:
            logger.error(f"클라우드인증서 로그인 실패: {self._last_err()}")
        if self.on_login:
            self.on_login(ok)
        return ok

    def logout(self, user_id: str) -> bool:
        return self._api.CommLogout(user_id) == 0

    def is_logged_in(self) -> bool:
        try:
            return self._api.GetLoginState() == 1
        except Exception:
            return False

    def is_connected(self) -> bool:
        try:
            return self._api.CommGetConnectState() == 1
        except Exception:
            return False

    def get_account_count(self) -> int:
        return int(self._api.GetUserAccCnt())

    def get_account_no(self, index: int = 0) -> str:
        return self._api.GetUserAccNo(index).strip()

    def encrypt(self, plaintext: str) -> str:
        """계좌비밀번호 등 암호화. 하나 API는 PWD 필드에 암호문 전달."""
        return self._api.GetEncrpyt(plaintext)

    # ── Tran 조회 (동기) ──────────────────────────────────────────────────────

    def request_tran(self,
                     tr_code: str,
                     in_records: List[tuple],       # [(rec_name, {ITEM: value}), ...]
                     out_record: str,
                     fields: List[str],
                     tran_type: str = "Q",
                     screen_no: str = "9999",
                     request_count: int = 20,
                     prev_or_next: str = "0",
                     prev_next_key: str = "",
                     is_benefit: str = "Y",
                     timeout: float = 10.0
                     ) -> Optional[List[Dict[str, str]]]:
        """
        Tran 조회/주문 요청 후 응답 이벤트를 대기해서 결과 반환.

        tran_type: "Q"=조회, "U"=Update(주문)
        반환: 응답 row 리스트 (각 row는 {필드명: 값} 딕셔너리), 실패 시 None
        """
        rq_id = self._api.CreateRequestID()
        if rq_id <= 0:
            logger.error(f"CreateRequestID 실패 (rq_id={rq_id})")
            return None

        # 입력값 셋
        for rec_name, inputs in in_records:
            for item, value in inputs.items():
                self._api.SetTranInputData(rq_id, tr_code, rec_name, item, str(value))

        # 응답 처리 스펙 등록
        event = threading.Event()
        self._pending_specs[rq_id] = {
            "tr_code": tr_code, "out_record": out_record, "fields": fields,
        }
        self._pending_events[rq_id] = event

        ret = self._api.RequestTran(rq_id, tr_code, is_benefit, prev_or_next,
                                     prev_next_key, screen_no, tran_type,
                                     request_count)
        if ret <= 0:
            logger.error(f"RequestTran 실패 ({tr_code}, ret={ret}): {self._last_err()}")
            self._cleanup_request(rq_id)
            return None

        if not event.wait(timeout=timeout):
            logger.error(f"RequestTran 타임아웃 ({tr_code}, {timeout}s)")
            self._cleanup_request(rq_id)
            return None

        rows = self._tran_results.pop(rq_id, None)
        err = self._tran_errors.pop(rq_id, "")
        self._cleanup_request(rq_id)

        if err and err != "0":
            logger.error(f"Tran 오류 ({tr_code}): {err}")
        return rows

    def _cleanup_request(self, rq_id: int):
        self._pending_specs.pop(rq_id, None)
        self._pending_events.pop(rq_id, None)
        try:
            self._api.ReleaseRqId(rq_id)
        except Exception:
            pass

    # ── 실시간 등록/해제 ──────────────────────────────────────────────────────

    def register_real(self, real_name: str, real_key: str) -> bool:
        """
        real_name: 실시간 서비스 코드 (V10 등)
        real_key : 구분키 (해외선물 종목코드 등)
        """
        ret = self._api.RegisterReal(real_name, real_key)
        ok = (ret == 0)
        if ok:
            logger.info(f"실시간 등록: {real_name}/{real_key}")
        else:
            logger.warning(f"실시간 등록 실패 ({real_name}/{real_key}): {self._last_err()}")
        return ok

    def unregister_real(self, real_name: str, real_key: str) -> bool:
        return self._api.UnRegisterReal(real_name, real_key) == 1

    def all_unregister_real(self) -> bool:
        return self._api.AllUnRegisterReal() == 1

    def get_real_output(self, real_name: str, item_name: str) -> str:
        """OnGetRealData 콜백 안에서만 호출."""
        try:
            return self._api.GetRealOutputData(real_name, item_name).strip()
        except Exception as e:
            logger.warning(f"GetRealOutputData 오류 {real_name}/{item_name}: {e}")
            return ""

    # ── 이벤트 핸들러 (EventHandler → 부모) ────────────────────────────────────

    def _on_tran_data(self, rq_id: int, pBlock, nBlockLength):
        spec = self._pending_specs.get(rq_id)
        if not spec:
            logger.debug(f"미등록 rq_id 응답 무시: {rq_id}")
            return

        tr_code = spec["tr_code"]
        out_record = spec["out_record"]
        fields = spec["fields"]

        error = self._get_opt(OPT_ERROR)
        msg = self._get_opt(OPT_MSG)

        rows: List[Dict[str, str]] = []
        try:
            count = self._api.GetTranOutputRowCnt(tr_code, out_record)
            for i in range(count):
                row = {}
                for f in fields:
                    v = self._api.GetTranOutputData(tr_code, out_record, f, i)
                    row[f] = (v or "").strip()
                rows.append(row)
        except Exception as e:
            logger.error(f"Tran 응답 파싱 오류({tr_code}/{out_record}): {e}", exc_info=True)

        self._tran_results[rq_id] = rows
        self._tran_errors[rq_id] = error
        if msg:
            logger.debug(f"Tran 메시지 [{tr_code}] {msg}")

        event = self._pending_events.get(rq_id)
        if event:
            event.set()

    def _on_real_data(self, real_name: str, real_key: str, pBlock, nBlockLength):
        if self.on_real_data:
            try:
                self.on_real_data(real_name, real_key)
            except Exception as e:
                logger.error(f"on_real_data 콜백 오류: {e}", exc_info=True)

    def _on_agent_event(self, event_type: int, param: int, str_param: str):
        # 100번대: 통신 이벤트, 150번대: 공지 이벤트
        logger.info(f"AgentEvent type={event_type} param={param} strParam={str_param}")
        if self.on_agent_event:
            try:
                self.on_agent_event(event_type, param, str_param)
            except Exception as e:
                logger.error(f"on_agent_event 콜백 오류: {e}", exc_info=True)

    # ── 부가 유틸 ─────────────────────────────────────────────────────────────

    def _get_opt(self, opt_type: int) -> str:
        try:
            return (self._api.GetCommRecvOptionValue(opt_type) or "").strip()
        except Exception:
            return ""

    def _last_err(self) -> str:
        try:
            return self._api.GetLastErrMsg() or ""
        except Exception:
            return ""


# ── COM 이벤트 핸들러 ─────────────────────────────────────────────────────────

class _HanaEventHandler:
    """
    DispatchWithEvents 로 연결되는 이벤트 핸들러.
    각 이벤트를 _parent(HanaOpenAPI) 로 위임.
    """
    _parent: Optional[HanaOpenAPI] = None

    def OnGetTranData(self, nRequestId, pBlock, nBlockLength):
        if self._parent:
            self._parent._on_tran_data(nRequestId, pBlock, nBlockLength)

    def OnGetFidData(self, nRequestId, pBlock, nBlockLength):
        # FID 조회는 현재 봇에서 미사용 (필요 시 확장)
        pass

    def OnGetRealData(self, strRealName, strRealKey, pBlock, nBlockLength):
        if self._parent:
            self._parent._on_real_data(strRealName, strRealKey, pBlock, nBlockLength)

    def OnAgentEventHandler(self, nEventType, nParam, strParam):
        if self._parent:
            self._parent._on_agent_event(nEventType, nParam, strParam)


# ── 상위: 해외선물 매매 클라이언트 ────────────────────────────────────────────

def _safe_float(s: str, default: float = 0.0) -> float:
    try:
        return float(s.replace(",", "").strip()) if s else default
    except (ValueError, AttributeError):
        return default


def _safe_int(s: str, default: int = 0) -> int:
    try:
        return int(_safe_float(s, default))
    except (ValueError, TypeError):
        return default


class HanaFuturesClient:
    """
    해외선물 매매 상위 클라이언트.
    HanaOpenAPI 저수준 래퍼 위에서 semantic method (send_order/get_positions 등) 제공.

    계좌번호 형식: "12345678-01" 또는 "12345678-220" 등
      - 종합계좌대체번호(CTNO) 8자리(또는 9자리)
      - 계좌상품번호(APNO) 뒤 2~3자리
    """

    def __init__(self, api: HanaOpenAPI, account_no: str, account_pw: str,
                 user_id: str = ""):
        self.api = api
        self.account_no = account_no
        self.account_pw = account_pw
        self.user_id = user_id

        # 계좌번호 파싱
        self.ctno, self.apno = self._parse_account(account_no)
        # 비밀번호 암호화 (API 요구사항)
        self._pw_enc: str = ""   # login 이후에 encrypt 가능

        # 사용자 콜백 (tick/체결)
        self.on_tick: Optional[Callable[[str, float, int, datetime], None]] = None
        self.on_order_execution: Optional[Callable[[dict], None]] = None

        # 라우팅
        self.api.on_real_data = self._route_real_data
        self._subscribed_ticks: set = set()

    @staticmethod
    def _parse_account(account_no: str) -> "tuple[str, str]":
        parts = account_no.replace(" ", "").split("-")
        if len(parts) == 2:
            ctno, apno = parts
        else:
            ctno, apno = account_no[:-3], account_no[-3:]
        return ctno.zfill(9), apno.zfill(3)

    def prepare(self):
        """로그인 이후 호출 — 비밀번호 암호화 준비."""
        self._pw_enc = self.api.encrypt(self.account_pw)
        logger.info(f"[해외선물 클라이언트] 계좌 준비 ctno={self.ctno} apno={self.apno}")

    # ── 계좌·잔고·포지션 ──────────────────────────────────────────────────────

    def get_account_list(self) -> List[Dict[str, str]]:
        return self.api.request_tran(
            tr_code=TR_ACCOUNT_LIST,
            in_records=[
                ("HHTACCNM01_InRec1", {
                    "func": "1", "usid": self.user_id, "errc": "",
                    "emsg": "", "nrec": "0",
                }),
            ],
            out_record="HHTACCNM01_out_sub01",
            fields=["accn", "sub_accn", "acnm", "acal", "achk"],
        ) or []

    def get_positions(self) -> List[Position]:
        rows = self.api.request_tran(
            tr_code=TR_POSITIONS,
            in_records=[
                ("OTS5919Q41_in", {"ODRV_SELL_BUY_DCD": ""}),   # 빈값=전체
                ("OTS5919Q41_in_sub01", {
                    "CTNO": self.ctno, "APNO": self.apno, "PWD": self._pw_enc,
                }),
            ],
            out_record="OTS5919Q41_out_sub01",
            fields=["PRDT_CD", "ODRV_SELL_BUY_DCD", "USTL_CTRC_QNT",
                    "TRDE_AVR_UNPR", "ODRV_NOW_PRC", "ODRV_EVL_PFLS_AMT",
                    "CRRY_CD"],
        ) or []

        positions = []
        for r in rows:
            qty = _safe_int(r.get("USTL_CTRC_QNT"))
            if qty == 0:
                continue
            side_cd = r.get("ODRV_SELL_BUY_DCD", "")
            side = "LONG" if side_cd == "B" else "SHORT" if side_cd == "S" else side_cd
            positions.append(Position(
                symbol=r.get("PRDT_CD", ""),
                side=side, qty=abs(qty),
                avg_price=_safe_float(r.get("TRDE_AVR_UNPR")),
                current_price=_safe_float(r.get("ODRV_NOW_PRC")),
                eval_pnl=_safe_float(r.get("ODRV_EVL_PFLS_AMT")),
                currency=r.get("CRRY_CD", ""),
            ))
        return positions

    def get_deposit(self) -> List[Dict[str, str]]:
        """예수금·증거금 상세 (통화별 원시 데이터)."""
        return self.api.request_tran(
            tr_code=TR_DEPOSIT,
            in_records=[
                ("OTS5943Q01_in", {
                    "BSN_DT": datetime.now().strftime("%Y%m%d"),
                    "CTNO": self.ctno, "APNO": self.apno, "PWD": self._pw_enc,
                }),
            ],
            out_record="OTS5943Q01_out_sub01",
            fields=["CRRY_CD", "THDT_CSH_BLCE_CTNS", "ODRV_ORDR_PSBL_AMT_CTNS",
                    "ODRV_MNTN_WMY_CTNS", "TOT_ACC_ASST_VALU_AMT_CTNS",
                    "ODRV_WDRW_PSBL_AMT_CTNS"],
        ) or []

    def get_account_info(self, currency: str = "USD") -> Optional[AccountInfo]:
        for r in self.get_deposit():
            if r.get("CRRY_CD", "").upper() == currency.upper():
                return AccountInfo(
                    total_asset_value=_safe_float(r.get("TOT_ACC_ASST_VALU_AMT_CTNS")),
                    cash_balance=_safe_float(r.get("THDT_CSH_BLCE_CTNS")),
                    order_available=_safe_float(r.get("ODRV_ORDR_PSBL_AMT_CTNS")),
                    maintenance_margin=_safe_float(r.get("ODRV_MNTN_WMY_CTNS")),
                    withdrawable=_safe_float(r.get("ODRV_WDRW_PSBL_AMT_CTNS")),
                    currency=currency,
                )
        return None

    # ── 주문 ──────────────────────────────────────────────────────────────────

    def send_order(self, symbol: str, side: str, qty: int,
                   price: Optional[float] = None, prc_type: str = "MARKET",
                   stop_loss: Optional[float] = None) -> OrderResult:
        """
        해외선물 신규 주문.

        Args:
            symbol   : 주문 종목코드 (예: "MGCQ26")
            side     : "BUY" 또는 "SELL"
            qty      : 계약 수
            price    : 지정가 (MARKET 이면 무시)
            prc_type : "MARKET" / "LIMIT" / "STOP" / "STOP_LIMIT"
            stop_loss: STOP 계열 주문 시 발동가

        Returns:
            OrderResult(success, order_no, msg)
        """
        # 필드값 매핑 — EF1 실시간 통보의 매핑 문서 기준
        sell_buy = {"BUY": "B", "SELL": "S"}.get(side.upper())
        if sell_buy is None:
            return OrderResult(False, msg=f"잘못된 side: {side}")

        prc_cnd = {
            "LIMIT":      "1",   # 지정가
            "MARKET":     "2",   # 시장가
            "STOP":       "3",   # STOP MARKET
            "STOP_LIMIT": "4",   # STOP LIMIT
        }.get(prc_type.upper())
        if prc_cnd is None:
            return OrderResult(False, msg=f"잘못된 prc_type: {prc_type}")

        prc_str = "0" if prc_type.upper() == "MARKET" else f"{price or 0:.10f}".rstrip("0").rstrip(".")
        stop_str = f"{stop_loss or 0:.10f}".rstrip("0").rstrip(".") if stop_loss else "0"

        rows = self.api.request_tran(
            tr_code=TR_ORDER_NEW,
            in_records=[
                ("OTS5901U01_in", {
                    "CTNO": self.ctno, "APNO": self.apno, "PWD": self._pw_enc,
                    "PRDT_CD": symbol,
                    "SELL_BUY_DCD": sell_buy,
                    "PRC_CND_DCD": prc_cnd,
                    "ODRV_ORDR_PRC": prc_str,
                    "ORDR_QNT": str(qty),
                    "STLS_APPN_PRC": stop_str,
                    "ORDR_HND_DCD": "1",     # 신규
                    "ORDR_DCD": "1",         # 신규주문
                    "ETC_ORDR_DCD": "",
                    "CNCS_CND_DCD": "1",     # 체결조건 FAS 기본
                    "CLR_PST_NO": "",
                    "ORDR_EXPR_DT": "",
                }),
            ],
            out_record="OTS5901U01_out",
            fields=["ODRV_ODNO"],
            tran_type="U",       # 주문은 Update
            is_benefit="Y",
        )
        if not rows:
            return OrderResult(False, msg="주문 응답 없음(타임아웃/에러)")
        order_no = rows[0].get("ODRV_ODNO", "").strip()
        return OrderResult(success=bool(order_no), order_no=order_no,
                           msg="접수" if order_no else "주문번호 없음")

    def cancel_order(self, orig_order_no: str, symbol: str, qty: int) -> OrderResult:
        rows = self.api.request_tran(
            tr_code=TR_ORDER_CANCEL,
            in_records=[
                ("OTS5901U03_in", {
                    "CTNO": self.ctno, "APNO": self.apno, "PWD": self._pw_enc,
                    "PRDT_CD": symbol, "ORDR_QNT": str(qty),
                    "OR_ODRV_ODNO": orig_order_no,
                }),
            ],
            out_record="OTS5901U03_out",
            fields=["ODRV_ODNO"],
            tran_type="U",
        )
        if not rows:
            return OrderResult(False, msg="취소 응답 없음")
        return OrderResult(True, order_no=rows[0].get("ODRV_ODNO", ""), msg="취소 접수")

    # ── 실시간 시세 / 체결통보 ────────────────────────────────────────────────

    def subscribe_tick(self, symbol: str) -> bool:
        if symbol in self._subscribed_ticks:
            return True
        ok = self.api.register_real(REAL_TICK, symbol)
        if ok:
            self._subscribed_ticks.add(symbol)
        return ok

    def unsubscribe_tick(self, symbol: str) -> bool:
        ok = self.api.unregister_real(REAL_TICK, symbol)
        self._subscribed_ticks.discard(symbol)
        return ok

    def subscribe_execution(self) -> bool:
        """주문 체결 통보(EF1) 구독. real_key 는 사용자ID."""
        return self.api.register_real(REAL_EXEC, self.user_id or self.ctno)

    def _route_real_data(self, real_name: str, real_key: str):
        if real_name == REAL_TICK:
            self._emit_tick(real_key)
        elif real_name == REAL_EXEC:
            self._emit_execution(real_key)
        else:
            logger.debug(f"실시간 미처리 이벤트 {real_name}/{real_key}")

    def _emit_tick(self, symbol: str):
        price = _safe_float(self.api.get_real_output(REAL_TICK, "TRDPRC_1"))
        vol = _safe_int(self.api.get_real_output(REAL_TICK, "TRDVOL_1"))
        ts_str = self.api.get_real_output(REAL_TICK, "KTRADE_TIME")
        ts = self._parse_ktime(ts_str) or datetime.now()
        if price > 0 and self.on_tick:
            self.on_tick(symbol, price, vol, ts)

    def _emit_execution(self, key: str):
        if not self.on_order_execution:
            return
        # EF1 주요 필드 추출
        data = {
            "notify_type": self.api.get_real_output(REAL_EXEC, "rltm_dpch_dcd"),
            "prcs_type":   self.api.get_real_output(REAL_EXEC, "rltm_dpch_prcs_dcd"),
            "prdt_cd":     self.api.get_real_output(REAL_EXEC, "prdt_cd"),
            "ordr_type":   self.api.get_real_output(REAL_EXEC, "odrv_ordr_tp_dcd"),
            "order_no":    self.api.get_real_output(REAL_EXEC, "odrv_odno"),
            "side":        self.api.get_real_output(REAL_EXEC, "odrv_sell_buy_dcd"),
            "price":       _safe_float(self.api.get_real_output(REAL_EXEC, "odrv_cncs_prc_ctns")),
            "qty":         _safe_int(self.api.get_real_output(REAL_EXEC, "cncs_qnt_ctns")),
            "remain_qty":  _safe_int(self.api.get_real_output(REAL_EXEC, "ordr_rmn_qnt_ctns")),
        }
        try:
            self.on_order_execution(data)
        except Exception as e:
            logger.error(f"on_order_execution 콜백 오류: {e}", exc_info=True)

    @staticmethod
    def _parse_ktime(hhmmss: str) -> Optional[datetime]:
        if not hhmmss or len(hhmmss) < 6:
            return None
        try:
            now = datetime.now()
            return now.replace(hour=int(hhmmss[:2]), minute=int(hhmmss[2:4]),
                               second=int(hhmmss[4:6]), microsecond=0)
        except (ValueError, TypeError):
            return None
