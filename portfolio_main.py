"""
다종목 포트폴리오 자동매매 (금 MGC + S&P MES) — 하나증권 Open API (1Q Pro)

실행:
  C:\\Python311-32\\python.exe portfolio_main.py

요구사항:
  - Windows + 32bit Python + pywin32
  - 하나증권 1Q Open API 설치 (regHFCommAgent.bat 관리자권한 등록 완료)
  - config/config.local.yaml 에 계좌·비밀번호·인증 정보 설정

주의:
  - 실계좌 전 반드시 is_mock: true 로 1~2개월 모의투자 검증
  - 거래당 리스크 3% 공격형 설정 — 손실 변동성 큼
"""

import atexit
import signal
import sys
from datetime import datetime
from typing import Dict

from api.hana_api import (
    HanaOpenAPI, HanaFuturesClient, ensure_qapp,
    LOGIN_MODE_OVERSEAS_MOCK, LOGIN_MODE_DOMESTIC_OVERSEAS_LIVE,
)
from risk.risk_manager import RiskManager, SymbolSpec
from strategy.factory import create_strategy
from engine.symbol_trader import SymbolTrader
from utils.logger import setup_logger
from utils.safety import (
    validate_account_safety, AccountSafetyError, validate_python_bits,
)
from utils.config_loader import load_config

logger = setup_logger("portfolio")


class PortfolioEngine:

    def __init__(self, config: dict):
        api_cfg = config["api"]
        risk_cfg = config["risk"]
        exec_cfg = config["execution"]

        # 저수준 API
        self.api = HanaOpenAPI(progid=api_cfg.get("progid") or None)
        self.api.on_agent_event = self._on_agent_event
        self.openapi_root = api_cfg["openapi_root"]
        self.user_id = api_cfg.get("user_id", "")
        self.account_no = api_cfg["account_no"]
        self.account_pw = api_cfg["account_pw"]           # 계좌 비번 (주문·조회용)
        self.login_pw = api_cfg.get("login_pw", "")       # 홈페이지 로그인 비번 (CommLogin 2번째 인자)
        self.cert_pw = api_cfg.get("cert_pw", "")         # 공동인증서 비번
        self.use_cloud_cert = api_cfg.get("use_cloud_cert", True)
        self.is_mock = api_cfg["is_mock"]

        # 상위 매매 클라이언트
        self.client = HanaFuturesClient(
            api=self.api, account_no=self.account_no,
            account_pw=self.account_pw, user_id=self.user_id,
        )
        self.client.on_tick = self._on_tick
        self.client.on_order_execution = self._on_order_execution

        # 계좌 단위 리스크
        self.risk = RiskManager(
            account_equity_krw=risk_cfg["account_equity"],
            risk_per_trade_pct=risk_cfg["risk_per_trade_pct"],
            daily_loss_limit_pct=risk_cfg["daily_loss_limit_pct"],
            max_positions=risk_cfg["max_positions"],
            max_contracts_per_trade=risk_cfg["max_contracts_per_trade"],
            usd_krw_rate=risk_cfg.get("usd_krw_rate", 1350.0),
        )

        order_prc_type = exec_cfg.get("order_type", "MARKET")
        self.poll_interval_sec = int(exec_cfg.get("quote_polling_interval_sec", 30))

        # 종목별 트레이더
        self.traders: list[SymbolTrader] = []
        for s in config["symbols"]:
            spec = SymbolSpec(s["code"], s["tick_size"], s["tick_value"], s.get("name", ""))
            strategy = create_strategy(s["strategy"])
            self.traders.append(SymbolTrader(
                spec=spec,
                timeframe=s.get("timeframe", "1D"),
                strategy=strategy,
                client=self.client,
                risk=self.risk,
                order_prc_type=order_prc_type,
                contract_code=s.get("contract_code", ""),
                quote_code=s.get("quote_code", ""),
            ))

        self._route: Dict[str, SymbolTrader] = {}
        self._running = False
        self._quote_timer = None

    # ── 시작 / 종료 ───────────────────────────────────────────────────────────

    def start(self):
        logger.info("=" * 60)
        logger.info(" 하나증권 다종목 포트폴리오 자동매매 시작")
        for t in self.traders:
            logger.info(f"  - {t.spec.code} ({t.spec.name}) / "
                        f"{t.strategy.name} / {t.timeframe}")
        logger.info(f" 계좌: {'모의투자' if self.is_mock else '실계좌'} | "
                    f"{self.risk.status_summary()}")
        logger.info("=" * 60)

        # 1) COM 컨트롤 초기화 + 리소스 로드 (QAxWidget 은 __init__ 때 완료됨)
        if not self.api.connect_server(self.openapi_root):
            logger.error("HanaOpenAPI 서버 접속 실패 — 종료")
            sys.exit(1)

        # 2) 로그인 (해외모의 or 실계좌)
        mode = LOGIN_MODE_OVERSEAS_MOCK if self.is_mock else LOGIN_MODE_DOMESTIC_OVERSEAS_LIVE
        self.api.set_login_mode(mode)

        if self.use_cloud_cert:
            ok = self.api.login_cloud_cert(self.user_id)
        else:
            # CommLogin(로그인ID, 홈페이지_로그인_비번, 공동인증서_비번)
            # ※ 홈페이지 로그인 비번이며 계좌 비번이 아님!
            ok = self.api.login(self.user_id, self.login_pw, self.cert_pw)

        if not ok:
            logger.error("로그인 실패 — 종료")
            self.api.disconnect()
            sys.exit(2)

        # 3) 매매 클라이언트 준비 (비밀번호 암호화)
        self.client.prepare()

        # 4) 종목별 초기화 (실시간 구독 + 포지션 복원)
        for t in self.traders:
            t.setup()
            self._route[t.quote_symbol] = t

        # 5) 주문체결 통보 구독
        self.client.subscribe_execution()

        # 6) FID 시세 폴링 시작 (V10 실시간 미제공 시 대체)
        self._start_quote_polling()

        self._running = True
        logger.info(f"포트폴리오 가동 ({len(self.traders)}종목 구독)")

    def _start_quote_polling(self):
        """QTimer 로 주기적 FID 시세 조회 → 트레이더 poll_quote 로 라우팅."""
        if self.poll_interval_sec <= 0:
            logger.info("FID 시세 폴링 비활성 (quote_polling_interval_sec<=0)")
            return
        from PyQt5.QtCore import QTimer
        self._quote_timer = QTimer()
        self._quote_timer.timeout.connect(self._poll_all_quotes)
        self._quote_timer.start(self.poll_interval_sec * 1000)
        logger.info(f"FID 시세 폴링 시작 (매 {self.poll_interval_sec}초, {len(self.traders)}종목)")

    def _poll_all_quotes(self):
        """모든 트레이더 종목의 시세를 순차 조회 후 각 트레이더로 전달."""
        self._poll_tick_count = getattr(self, "_poll_tick_count", 0) + 1
        now = datetime.now()
        debug = self._poll_tick_count <= 3

        if debug:
            logger.info(f"[폴링#{self._poll_tick_count}] 시세 조회 시작 ({len(self.traders)}종목)")

        for trader in self.traders:
            try:
                q = self.client.get_quote(trader.quote_symbol)
                if debug:
                    logger.info(f"[폴링#{self._poll_tick_count}] "
                                f"{trader.quote_symbol} 응답={type(q).__name__} "
                                f"{repr(q)[:200]}")
                if q:
                    trader.poll_quote(q, now)
                elif debug:
                    logger.warning(f"[폴링#{self._poll_tick_count}] "
                                   f"{trader.quote_symbol} 응답이 falsy — poll_quote 스킵")
            except Exception as e:
                logger.error(f"폴링 오류 {trader.quote_symbol}: {e}", exc_info=True)

    def stop(self):
        self._running = False
        if self._quote_timer:
            try:
                self._quote_timer.stop()
            except Exception:
                pass
        for t in self.traders:
            t.stop()
        try:
            self.api.all_unregister_real()
        except Exception:
            pass
        if self.user_id:
            try:
                self.api.logout(self.user_id)
            except Exception:
                pass
        self.api.disconnect()
        logger.info("포트폴리오 자동매매 종료")

    # ── 콜백 ─────────────────────────────────────────────────────────────────

    def _on_tick(self, symbol, price, volume, ts):
        trader = self._route.get(symbol)
        if trader:
            trader.route_tick(symbol, price, volume, ts)

    def _on_order_execution(self, data: dict):
        logger.info(f"[체결통보] {data}")

    def _on_agent_event(self, event_type: int, param: int, str_param: str):
        # 100번대: 통신 이벤트(접속/해제), 150번대: 공지
        if event_type >= 100 and event_type < 150:
            logger.warning(f"통신 이벤트 type={event_type}: {str_param}")
        elif event_type >= 150:
            logger.info(f"공지 이벤트 type={event_type}: {str_param}")


def main():
    validate_python_bits()
    config = load_config()

    if not config.get("symbols"):
        logger.error("config.yaml 에 symbols: 목록이 없습니다.")
        sys.exit(1)

    try:
        validate_account_safety(config)
    except AccountSafetyError as e:
        logger.error(f"계좌 안전 검증 실패 — 봇 시작 거부\n{e}")
        sys.exit(2)

    # PyQt5 이벤트 루프 필요 (QAxWidget 호스팅)
    app = ensure_qapp()

    # Qt 이벤트 루프 중에도 파이썬 signal 이 처리되도록 주기적으로 제어권 반환
    # (Windows + PyQt5 에서 Ctrl+C 가 안 먹히는 문제 해결)
    from PyQt5.QtCore import QTimer
    _sig_pump = QTimer()
    _sig_pump.timeout.connect(lambda: None)
    _sig_pump.start(200)   # 매 200ms 마다 파이썬 tick

    engine = PortfolioEngine(config)

    # 종료 훅 — 창 닫기·SIGTERM·인터프리터 종료 시에도 세션 정리 시도
    # (하나 API 서버 좀비 세션 방지)
    _stopped = [False]
    def _cleanup(reason=""):
        if _stopped[0]:
            return
        _stopped[0] = True
        if reason:
            logger.info(f"세션 정리 ({reason})")
        try:
            engine.stop()
        except Exception as e:
            logger.warning(f"stop 실패: {e}")

    atexit.register(lambda: _cleanup("atexit"))
    signal.signal(signal.SIGINT,  lambda *_: (_cleanup("SIGINT"),  app.quit()))
    signal.signal(signal.SIGTERM, lambda *_: (_cleanup("SIGTERM"), app.quit()))
    try:
        signal.signal(signal.SIGBREAK, lambda *_: (_cleanup("SIGBREAK"), app.quit()))
    except AttributeError:
        pass   # non-Windows

    try:
        engine.start()
        logger.info("Ctrl+C 로 종료")
        app.exec_()
    finally:
        _cleanup("finally")


if __name__ == "__main__":
    main()
