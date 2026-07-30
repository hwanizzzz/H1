"""
하나증권 Open API (1Q Pro) — Python 래퍼 (PyQt5 QAxWidget 기반)

배경:
  HFCOMMAGENT.HFCommAgentCtrl.1 은 "윈도우가 있는 ActiveX 컨트롤" 이라
  win32com.client.Dispatch 만으로는 초기화 되지 않고 모든 메소드가
  E_UNEXPECTED(-2147418113) 로 실패한다. PyQt5 의 QAxWidget 이
  숨김 창 + Qt 메시지 펌프를 자동 제공해 OCX 를 정상 호스팅한다.

계층:
  HanaOpenAPI       : QAxWidget 기반 저수준 COM 래퍼
  HanaFuturesClient : 해외선물 매매 상위 API (send_order/get_positions ...)

주요 흐름:
  app = ensure_qapp()
  api = HanaOpenAPI()
  api.connect_server(openapi_root)  # CommInit + 리소스 로드
  api.set_login_mode(LOGIN_MODE_OVERSEAS_MOCK)
  api.login_cloud_cert(user_id)
  client = HanaFuturesClient(api, account_no, account_pw, user_id)
  client.prepare()
  client.subscribe_tick("MGCQ26")
  ...
  app.exec_()   # Qt 이벤트 루프
"""

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from utils.logger import setup_logger

logger = setup_logger("hana_api")

_IS_WINDOWS = sys.platform == "win32"

# ── PyQt5 가용성 ─────────────────────────────────────────────────────────────

if _IS_WINDOWS:
    try:
        from PyQt5.QAxContainer import QAxWidget
        from PyQt5.QtWidgets import QApplication
        from PyQt5.QtCore import QEventLoop, QTimer
        _QT_AVAILABLE = True
    except ImportError:
        _QT_AVAILABLE = False
        logger.warning("PyQt5 미설치. pip install PyQt5 후 재시도하세요.")
else:
    _QT_AVAILABLE = False
    logger.warning("Windows 환경이 아닙니다. API 기능이 제한됩니다.")


# ── 상수 ─────────────────────────────────────────────────────────────────────

HANA_PROGID = "HFCOMMAGENT.HFCommAgentCtrl.1"

# SetLoginMode(nOption=0, nMode)
LOGIN_MODE_DOMESTIC_OVERSEAS_LIVE = 0
LOGIN_MODE_DOMESTIC_MOCK          = 1
LOGIN_MODE_OVERSEAS_MOCK          = 2
LOGIN_MODE_DOMESTIC_LIVE          = 3

# GetCommRecvOptionValue
OPT_TR_CODE = 0
OPT_ERROR   = 7
OPT_MSG     = 4

# 해외선물 TR 코드
TR_ACCOUNT_LIST = "HHTACCNM01"
TR_ORDER_NEW    = "OTS5901U01"
TR_ORDER_MODIFY = "OTS5901U02"
TR_ORDER_CANCEL = "OTS5901U03"
TR_POSITIONS    = "OTS5919Q41"
TR_FILLED       = "OTS5911Q52"
TR_UNFILLED     = "OTS5911Q41"
TR_DEPOSIT      = "OTS5943Q01"

REAL_TICK      = "V10"
REAL_ORDERBOOK = "V11"
REAL_EXEC      = "EF1"
REAL_OPEN_POS  = "EF2"
REAL_UNFILLED  = "EF4"


# ── 데이터 클래스 ─────────────────────────────────────────────────────────────

@dataclass
class OrderResult:
    success: bool
    order_no: str = ""
    msg: str = ""


@dataclass
class Position:
    symbol: str
    side: str
    qty: int
    avg_price: float
    current_price: float
    eval_pnl: float
    currency: str = ""


@dataclass
class AccountInfo:
    total_asset_value: float
    cash_balance: float
    order_available: float
    maintenance_margin: float
    withdrawable: float
    currency: str = "USD"


# ── Qt Application 싱글턴 ────────────────────────────────────────────────────

_qapp: Optional["QApplication"] = None


def ensure_qapp() -> "QApplication":
    """QAxWidget 생성 전 QApplication 이 반드시 존재해야 함."""
    global _qapp
    if not _QT_AVAILABLE:
        raise RuntimeError("PyQt5 사용 불가 (설치 필요)")
    if _qapp is None:
        _qapp = QApplication.instance() or QApplication(sys.argv)
    return _qapp


# ── 저수준 COM 래퍼 (QAxWidget) ──────────────────────────────────────────────

if _QT_AVAILABLE:

    class HanaOpenAPI(QAxWidget):
        """
        HFCOMMAGENT ActiveX 를 QAxWidget 로 호스팅하는 저수준 래퍼.
        Qt 이벤트 루프가 도는 상태에서 사용해야 함 (app.exec_()).
        """

        def __init__(self, progid: Optional[str] = None):
            ensure_qapp()
            super().__init__()
            self.progid = progid or HANA_PROGID

            logger.info(f"ActiveX setControl: {self.progid}")
            if not self.setControl(self.progid):
                raise RuntimeError(
                    f"ActiveX 생성 실패: {self.progid} — "
                    f"regHFCommAgent.bat 관리자권한 등록 확인"
                )
            logger.info("ActiveX 호스팅 성공 (QAxWidget)")

            self._orig_cwd: Optional[str] = None

            # 사용자 콜백
            self.on_login: Optional[Callable[[bool], None]] = None
            self.on_real_data: Optional[Callable[[str, str], None]] = None
            self.on_agent_event: Optional[Callable[[int, int, str], None]] = None

            # 동기 Tran 관리
            self._pending_specs: Dict[int, dict] = {}
            self._pending_loops: Dict[int, "QEventLoop"] = {}
            self._tran_results: Dict[int, List[Dict[str, str]]] = {}
            self._tran_errors: Dict[int, str] = {}
            self._tran_msgs: Dict[int, str] = {}

            self._connect_events()

        # ── OCX 이벤트를 Qt 슬롯으로 연결 ────────────────────────────────────

        def _connect_events(self):
            """COM 이벤트가 QAxWidget 에 시그널로 노출됨."""
            handlers = [
                ("OnGetTranData(int,QString,int)", self._slot_tran_data),
                ("OnGetRealData(QString,QString,QString,int)", self._slot_real_data),
                ("OnGetFidData(int,QString,int)", self._slot_fid_data),
                ("OnAgentEventHandler(int,int,QString)", self._slot_agent_event),
            ]
            for sig, slot in handlers:
                try:
                    ok = self.connectSlot(sig, slot) if hasattr(self, "connectSlot") \
                         else self._connect_by_name(sig, slot)
                    if not ok:
                        logger.warning(f"이벤트 연결 실패(무시): {sig}")
                except Exception as e:
                    logger.warning(f"이벤트 연결 예외 {sig}: {e}")

        def _connect_by_name(self, sig: str, slot) -> bool:
            """QAxWidget 이벤트를 시그니처 문자열로 연결."""
            name = sig.split("(")[0]
            try:
                getattr(self, name).connect(slot)
                return True
            except Exception:
                pass
            # 폴백 — QMetaObject 이용
            try:
                from PyQt5.QtCore import QMetaObject
                idx = self.metaObject().indexOfSignal(sig)
                if idx < 0:
                    return False
                self.connect(self, self.metaObject().method(idx),
                             slot, type=0)
                return True
            except Exception:
                return False

        # ── OCX 메소드 호출 헬퍼 ──────────────────────────────────────────────

        def _call(self, sig: str, *args):
            """dynamicCall 로 OCX 메소드 호출."""
            return self.dynamicCall(sig, list(args))

        # ── 연결 / 리소스 로드 ────────────────────────────────────────────────

        def connect_server(self, openapi_root: str) -> bool:
            if not os.path.isdir(openapi_root):
                logger.error(f"openapi_root 경로 없음: {openapi_root}")
                return False

            # CWD 를 API 루트로 변경 (comms.ini 등 상대경로 검색)
            try:
                self._orig_cwd = os.getcwd()
                os.chdir(openapi_root)
                logger.info(f"CWD 변경: {openapi_root}")
            except Exception as e:
                logger.error(f"CWD 변경 실패: {e}")
                return False

            # 팝업 방지 (CommInit 전 필수)
            try:
                self._call("SetOffAgentMessageBox(int)", 1)
                logger.debug("SetOffAgentMessageBox OK")
            except Exception as e:
                logger.warning(f"SetOffAgentMessageBox: {e}")

            # 해외 서비스 초기설정
            try:
                self._call("SetOptionalInitBox(int)", 0)
                logger.debug("SetOptionalInitBox OK")
            except Exception as e:
                logger.warning(f"SetOptionalInitBox: {e}")

            # 통신 초기화
            try:
                ret = self._call("CommInit()")
            except Exception as e:
                logger.error(
                    f"CommInit 예외: {e}\n"
                    f"  체크: openapi_root={openapi_root}\n"
                    f"        System/comms.ini 존재? {os.path.exists('System/comms.ini')}\n"
                    f"        regHFCommAgent.bat 등록 완료?\n"
                    f"        vcredist_x86.exe 설치 완료?\n"
                    f"        Python 관리자권한 실행?"
                )
                self._restore_cwd()
                return False

            if int(ret or 0) != 0:
                logger.error(f"CommInit 실패 ret={ret}: {self._last_err()}")
                self._restore_cwd()
                return False
            logger.info("CommInit 성공")

            self._load_all_resources(openapi_root)
            return True

        def _load_all_resources(self, openapi_root: str):
            for sub, meth in [("TranRes", "LoadTranResource(const QString&)"),
                              ("RealRes", "LoadRealResource(const QString&)")]:
                dpath = os.path.join(openapi_root, sub)
                if not os.path.isdir(dpath):
                    logger.warning(f"리소스 폴더 없음: {dpath}")
                    continue
                n = 0
                for fname in os.listdir(dpath):
                    if fname.lower().endswith(".res"):
                        fpath = os.path.join(dpath, fname)
                        ret = self._call(meth, fpath)
                        if int(ret or 0) == 1:
                            n += 1
                        else:
                            logger.warning(f"{sub} 로드 실패: {fname}")
                logger.info(f"{sub} {n}개 리소스 로드")

        def _restore_cwd(self):
            if self._orig_cwd:
                try:
                    os.chdir(self._orig_cwd)
                except Exception:
                    pass

        def disconnect(self):
            try:
                self._call("CommTerminate(int)", 1)
            except Exception:
                pass
            self._restore_cwd()
            logger.info("API 연결 해제")

        # ── 로그인 ────────────────────────────────────────────────────────────

        def set_login_mode(self, mode: int):
            self._call("SetLoginMode(int,int)", 0, mode)
            logger.info(f"SetLoginMode(0, {mode})")

        def login(self, user_id: str, password: str, cert_pw: str) -> bool:
            ret = self._call("CommLogin(const QString&,const QString&,const QString&)",
                             user_id, password, cert_pw)
            ok = int(ret or 0) == 1
            logger.info("공동인증서 로그인 " + ("성공" if ok else f"실패: {self._last_err()}"))
            if self.on_login:
                self.on_login(ok)
            return ok

        def login_cloud_cert(self, user_id: str) -> bool:
            ret = self._call("CommCloudCert(const QString&)", user_id)
            ok = int(ret or 0) == 1
            if ok:
                logger.info("클라우드인증서 로그인 성공")
            else:
                err = self._last_err()
                logger.info(f"클라우드인증서 로그인 실패: {err}")
                if "일치" in err or "match" in err.lower():
                    logger.warning(
                        "  힌트: '일치하지 않음' 메시지는 실제로 중복 세션(좀비 로그인)일 수 있습니다.\n"
                        "        1) 1QHTS 에 같은 ID 로 로그인해서 세션 정리\n"
                        "        2) 그래도 안 되면 5~15분 대기 후 재시도 (서버 자동 세션 만료)"
                    )
            if self.on_login:
                self.on_login(ok)
            return ok

        def logout(self, user_id: str) -> bool:
            return int(self._call("CommLogout(const QString&)", user_id) or 0) == 0

        def is_logged_in(self) -> bool:
            try:
                return int(self._call("GetLoginState()") or 0) == 1
            except Exception:
                return False

        def is_connected(self) -> bool:
            try:
                return int(self._call("CommGetConnectState()") or 0) == 1
            except Exception:
                return False

        def get_account_count(self) -> int:
            return int(self._call("GetUserAccCnt()") or 0)

        def get_account_no(self, index: int = 0) -> str:
            return str(self._call("GetUserAccNo(int)", index) or "").strip()

        def encrypt(self, plaintext: str) -> str:
            return str(self._call("GetEncrpyt(const QString&)", plaintext) or "")

        # ── Tran 조회 (동기 QEventLoop) ───────────────────────────────────────

        def request_tran(self,
                         tr_code: str,
                         in_records: List[tuple],
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
            rq_id = int(self._call("CreateRequestID()") or 0)
            if rq_id <= 0:
                logger.error(f"CreateRequestID 실패 rq_id={rq_id}")
                return None

            for rec_name, inputs in in_records:
                for item, value in inputs.items():
                    self._call(
                        "SetTranInputData(int,const QString&,const QString&,"
                        "const QString&,const QString&)",
                        rq_id, tr_code, rec_name, item, str(value),
                    )

            loop = QEventLoop()
            self._pending_specs[rq_id] = {
                "tr_code": tr_code, "out_record": out_record, "fields": fields,
            }
            self._pending_loops[rq_id] = loop

            ret = self._call(
                "RequestTran(int,const QString&,const QString&,const QString&,"
                "const QString&,const QString&,const QString&,int)",
                rq_id, tr_code, is_benefit, prev_or_next,
                prev_next_key, screen_no, tran_type, request_count,
            )
            if int(ret or 0) <= 0:
                logger.error(f"RequestTran 실패 {tr_code} ret={ret}: {self._last_err()}")
                self._cleanup(rq_id)
                return None

            # 타임아웃 타이머
            QTimer.singleShot(int(timeout * 1000), loop.quit)
            loop.exec_()

            rows = self._tran_results.pop(rq_id, None)
            err = self._tran_errors.pop(rq_id, "")
            err_msg = self._tran_msgs.pop(rq_id, "")
            self._cleanup(rq_id)
            if err and err != "0":
                logger.warning(f"Tran 오류 ({tr_code}) err={err} msg={err_msg!r} "
                               f"| last={self._last_err()!r}")
            return rows

        def _cleanup(self, rq_id: int):
            self._pending_specs.pop(rq_id, None)
            self._pending_loops.pop(rq_id, None)
            try:
                self._call("ReleaseRqId(int)", rq_id)
            except Exception:
                pass

        # ── 실시간 ────────────────────────────────────────────────────────────

        def register_real(self, real_name: str, real_key: str) -> bool:
            ret = self._call("RegisterReal(const QString&,const QString&)",
                             real_name, real_key)
            ok = int(ret or 0) == 0
            (logger.info if ok else logger.warning)(
                f"실시간 {'등록' if ok else '등록실패'}: {real_name}/{real_key}"
            )
            return ok

        def unregister_real(self, real_name: str, real_key: str) -> bool:
            return int(self._call("UnRegisterReal(const QString&,const QString&)",
                                  real_name, real_key) or 0) == 1

        def all_unregister_real(self) -> bool:
            return int(self._call("AllUnRegisterReal()") or 0) == 1

        def get_real_output(self, real_name: str, item_name: str) -> str:
            try:
                return str(self._call(
                    "GetRealOutputData(const QString&,const QString&)",
                    real_name, item_name,
                ) or "").strip()
            except Exception:
                return ""

        # ── 이벤트 슬롯 ───────────────────────────────────────────────────────

        def _slot_tran_data(self, rq_id, pBlock, nBlockLength):
            spec = self._pending_specs.get(rq_id)
            if not spec:
                return

            tr_code = spec["tr_code"]
            out_record = spec["out_record"]
            fields = spec["fields"]

            error = self._get_opt(OPT_ERROR)
            msg = self._get_opt(OPT_MSG)

            rows: List[Dict[str, str]] = []
            try:
                count = int(self._call(
                    "GetTranOutputRowCnt(const QString&,const QString&)",
                    tr_code, out_record,
                ) or 0)
                for i in range(count):
                    row = {}
                    for f in fields:
                        v = self._call(
                            "GetTranOutputData(const QString&,const QString&,"
                            "const QString&,int)",
                            tr_code, out_record, f, i,
                        )
                        row[f] = (str(v) if v is not None else "").strip()
                    rows.append(row)
            except Exception as e:
                logger.error(f"Tran 파싱 오류 {tr_code}/{out_record}: {e}", exc_info=True)

            self._tran_results[rq_id] = rows
            self._tran_errors[rq_id] = error
            self._tran_msgs[rq_id] = msg
            if msg:
                logger.debug(f"Tran msg [{tr_code}] {msg}")

            loop = self._pending_loops.get(rq_id)
            if loop:
                loop.quit()

        def _slot_real_data(self, real_name, real_key, pBlock, nBlockLength):
            if self.on_real_data:
                try:
                    self.on_real_data(str(real_name), str(real_key))
                except Exception as e:
                    logger.error(f"on_real_data 콜백 오류: {e}", exc_info=True)

        def _slot_fid_data(self, rq_id, pBlock, nBlockLength):
            pass  # 필요 시 확장

        def _slot_agent_event(self, event_type, param, str_param):
            logger.info(f"AgentEvent type={event_type} param={param} str={str_param}")
            if self.on_agent_event:
                try:
                    self.on_agent_event(int(event_type), int(param), str(str_param))
                except Exception as e:
                    logger.error(f"on_agent_event 콜백 오류: {e}", exc_info=True)

        # ── 유틸 ──────────────────────────────────────────────────────────────

        def _get_opt(self, opt_type: int) -> str:
            try:
                return str(self._call(
                    "GetCommRecvOptionValue(int)", opt_type,
                ) or "").strip()
            except Exception:
                return ""

        def _last_err(self) -> str:
            try:
                return str(self._call("GetLastErrMsg()") or "")
            except Exception:
                return ""

else:

    class HanaOpenAPI:   # 폴백 — 논-Windows 환경에서 import 만 되도록
        def __init__(self, *_, **__):
            raise RuntimeError("PyQt5/Windows 환경 필요")


# ── 상위: 해외선물 매매 클라이언트 ────────────────────────────────────────────

def _safe_float(s, default: float = 0.0) -> float:
    if s is None:
        return default
    try:
        return float(str(s).replace(",", "").strip() or default)
    except (ValueError, AttributeError):
        return default


def _safe_int(s, default: int = 0) -> int:
    try:
        return int(_safe_float(s, default))
    except (ValueError, TypeError):
        return default


class HanaFuturesClient:
    """해외선물 매매 상위 API."""

    def __init__(self, api: "HanaOpenAPI", account_no: str, account_pw: str,
                 user_id: str = ""):
        self.api = api
        self.account_no = account_no
        self.account_pw = account_pw
        self.user_id = user_id

        self.ctno, self.apno = self._parse_account(account_no)
        self._pw_enc: str = ""

        self.on_tick: Optional[Callable[[str, float, int, datetime], None]] = None
        self.on_order_execution: Optional[Callable[[dict], None]] = None

        self.api.on_real_data = self._route_real_data
        self._subscribed_ticks: set = set()

        # 관찰성 — 첫 N틱은 INFO 로그, 이후에는 심볼별 카운터
        self._tick_counter: Dict[str, int] = {}
        self._VERBOSE_TICKS = 5           # 심볼별 첫 5틱은 INFO 로그
        self._TICK_SUMMARY_EVERY = 100    # 매 100틱마다 요약

    @staticmethod
    def _parse_account(account_no: str):
        parts = account_no.replace(" ", "").split("-")
        if len(parts) == 2:
            ctno, apno = parts
        else:
            ctno, apno = account_no[:-3], account_no[-3:]
        return ctno.zfill(9), apno.zfill(3)

    def prepare(self):
        self._pw_enc = self.api.encrypt(self.account_pw)
        logger.info(f"[해외선물] 준비 ctno={self.ctno} apno={self.apno}")

    # ── 조회 ──────────────────────────────────────────────────────────────────

    def get_account_list(self) -> List[Dict[str, str]]:
        return self.api.request_tran(
            tr_code=TR_ACCOUNT_LIST,
            in_records=[
                ("HHTACCNM01_InRec1", {
                    "func": "1", "usid": self.user_id,
                    "errc": "", "emsg": "", "nrec": "0",
                }),
            ],
            out_record="HHTACCNM01_out_sub01",
            fields=["accn", "sub_accn", "acnm", "acal", "achk"],
        ) or []

    def get_positions(self) -> List[Position]:
        rows = self.api.request_tran(
            tr_code=TR_POSITIONS,
            in_records=[
                ("OTS5919Q41_in", {"ODRV_SELL_BUY_DCD": ""}),
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
                symbol=r.get("PRDT_CD", ""), side=side, qty=abs(qty),
                avg_price=_safe_float(r.get("TRDE_AVR_UNPR")),
                current_price=_safe_float(r.get("ODRV_NOW_PRC")),
                eval_pnl=_safe_float(r.get("ODRV_EVL_PFLS_AMT")),
                currency=r.get("CRRY_CD", ""),
            ))
        return positions

    def get_deposit(self) -> List[Dict[str, str]]:
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
        sell_buy = {"BUY": "B", "SELL": "S"}.get(side.upper())
        if sell_buy is None:
            return OrderResult(False, msg=f"잘못된 side: {side}")

        prc_cnd = {
            "LIMIT": "1", "MARKET": "2", "STOP": "3", "STOP_LIMIT": "4",
        }.get(prc_type.upper())
        if prc_cnd is None:
            return OrderResult(False, msg=f"잘못된 prc_type: {prc_type}")

        prc_str = "0" if prc_type.upper() == "MARKET" else \
                  f"{price or 0:.10f}".rstrip("0").rstrip(".")
        stop_str = (f"{stop_loss or 0:.10f}".rstrip("0").rstrip(".")
                    if stop_loss else "0")

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
                    "ORDR_HND_DCD": "1", "ORDR_DCD": "1",
                    "ETC_ORDR_DCD": "", "CNCS_CND_DCD": "1",
                    "CLR_PST_NO": "", "ORDR_EXPR_DT": "",
                }),
            ],
            out_record="OTS5901U01_out",
            fields=["ODRV_ODNO"],
            tran_type="U", is_benefit="Y",
        )
        if not rows:
            return OrderResult(False, msg="주문 응답 없음")
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

    # ── 실시간 ────────────────────────────────────────────────────────────────

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
        return self.api.register_real(REAL_EXEC, self.user_id or self.ctno)

    def _route_real_data(self, real_name: str, real_key: str):
        if real_name == REAL_TICK:
            self._emit_tick(real_key)
        elif real_name == REAL_EXEC:
            self._emit_execution(real_key)
        else:
            logger.debug(f"실시간 미처리 {real_name}/{real_key}")

    def _emit_tick(self, symbol: str):
        price = _safe_float(self.api.get_real_output(REAL_TICK, "TRDPRC_1"))
        vol = _safe_int(self.api.get_real_output(REAL_TICK, "TRDVOL_1"))
        ts_str = self.api.get_real_output(REAL_TICK, "KTRADE_TIME")
        ts = self._parse_ktime(ts_str) or datetime.now()

        # 관찰성 로그
        n = self._tick_counter.get(symbol, 0) + 1
        self._tick_counter[symbol] = n
        if n <= self._VERBOSE_TICKS:
            logger.info(f"[tick #{n}] {symbol} price={price} vol={vol} time={ts_str}")
        elif n % self._TICK_SUMMARY_EVERY == 0:
            logger.info(f"[tick] {symbol} 누적 {n}틱 최신 price={price}")

        if price > 0 and self.on_tick:
            self.on_tick(symbol, price, vol, ts)

    def _emit_execution(self, key: str):
        if not self.on_order_execution:
            return
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
            logger.error(f"on_order_execution 오류: {e}", exc_info=True)

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
