"""
단일 종목 자동매매 (기본: 금 MGCQ26) — 하나증권 Open API (1Q Pro)

실행:
  C:\\Python311-32\\python.exe main.py

다종목 동시 운용은 portfolio_main.py 사용.

요구사항:
  - Windows + 32bit Python + pywin32
  - 하나증권 1Q Open API 설치 완료 (regHFCommAgent.bat 등록)
  - config/config.local.yaml 에 계좌·비밀번호 설정
"""

import atexit
import signal
import sys
from datetime import datetime

from api.hana_api import (
    HanaOpenAPI, HanaFuturesClient, ensure_qapp,
    LOGIN_MODE_OVERSEAS_MOCK, LOGIN_MODE_DOMESTIC_OVERSEAS_LIVE,
)
from api.external_quote import YahooQuoteClient
from risk.risk_manager import RiskManager, SymbolSpec
from strategy.factory import create_strategy
from engine.symbol_trader import SymbolTrader
from utils.logger import setup_logger
from utils.safety import (
    validate_account_safety, AccountSafetyError, validate_python_bits,
)
from utils.config_loader import load_config

logger = setup_logger("main")


class TradingEngine:
    """단일 종목 자동매매 (SymbolTrader 래퍼)."""

    def __init__(self, config: dict):
        api_cfg = config["api"]
        sym_cfg = config["symbol"]
        risk_cfg = config["risk"]
        exec_cfg = config["execution"]

        self.api = HanaOpenAPI(progid=api_cfg.get("progid") or None)
        self.api.on_agent_event = self._on_agent_event
        self.openapi_root = api_cfg["openapi_root"]
        self.user_id = api_cfg.get("user_id", "")
        self.account_no = api_cfg["account_no"]
        self.account_pw = api_cfg["account_pw"]           # 계좌 비번 (주문·조회용)
        self.login_pw = api_cfg.get("login_pw", "")       # 홈페이지 로그인 비번
        self.cert_pw = api_cfg.get("cert_pw", "")         # 공동인증서 비번
        self.use_cloud_cert = api_cfg.get("use_cloud_cert", True)
        self.is_mock = api_cfg["is_mock"]

        self.client = HanaFuturesClient(
            api=self.api, account_no=self.account_no,
            account_pw=self.account_pw, user_id=self.user_id,
        )
        self.client.on_tick = self._on_tick
        self.client.on_order_execution = self._on_order_execution

        spec = SymbolSpec(sym_cfg["code"], sym_cfg["tick_size"],
                          sym_cfg["tick_value"], sym_cfg.get("name", ""))

        self.risk = RiskManager(
            account_equity_krw=risk_cfg["account_equity"],
            risk_per_trade_pct=risk_cfg["risk_per_trade_pct"],
            daily_loss_limit_pct=risk_cfg["daily_loss_limit_pct"],
            max_positions=risk_cfg["max_positions"],
            max_contracts_per_trade=risk_cfg["max_contracts_per_trade"],
            usd_krw_rate=risk_cfg.get("usd_krw_rate", 1350.0),
            default_spec=spec,
        )

        self.trader = SymbolTrader(
            spec=spec,
            timeframe=sym_cfg.get("timeframe", "1D"),
            strategy=create_strategy(sym_cfg["strategy"]),
            client=self.client,
            risk=self.risk,
            order_prc_type=exec_cfg.get("order_type", "MARKET"),
            contract_code=sym_cfg.get("contract_code", ""),
            quote_code=sym_cfg.get("quote_code", ""),
        )
        self.poll_interval_sec = int(exec_cfg.get("quote_polling_interval_sec", 30))
        self.history_bars_days = int(exec_cfg.get("history_bars_days", 60))
        self._ext_quote = YahooQuoteClient()
        self._quote_timer = None

    def start(self):
        logger.info("=" * 60)
        logger.info(" 하나증권 해외선물 자동매매 시작")
        logger.info(f" 전략 {self.trader.strategy.name} | 종목 {self.trader.spec.code} "
                    f"({'모의투자' if self.is_mock else '실계좌'})")
        logger.info("=" * 60)

        if not self.api.connect_server(self.openapi_root):
            logger.error("HanaOpenAPI 서버 접속 실패 — 종료")
            sys.exit(1)

        mode = LOGIN_MODE_OVERSEAS_MOCK if self.is_mock else LOGIN_MODE_DOMESTIC_OVERSEAS_LIVE
        self.api.set_login_mode(mode)
        # CommLogin(로그인ID, 홈페이지_로그인_비번, 공동인증서_비번)
        ok = (self.api.login_cloud_cert(self.user_id) if self.use_cloud_cert
              else self.api.login(self.user_id, self.login_pw, self.cert_pw))
        if not ok:
            logger.error("로그인 실패 — 종료")
            self.api.disconnect()
            sys.exit(2)

        self.client.prepare()
        self.trader.setup()

        # 과거 일봉 웜업
        if self.history_bars_days > 0:
            try:
                hist = self._ext_quote.get_history_bars(
                    self.trader.quote_symbol, days=self.history_bars_days,
                )
                if hist:
                    self.trader.preload_history(hist)
            except Exception as e:
                logger.error(f"과거 봉 로드 오류: {e}", exc_info=True)

        self.client.subscribe_execution()

        if self.poll_interval_sec > 0:
            from PyQt5.QtCore import QTimer
            self._quote_timer = QTimer()
            self._quote_timer.timeout.connect(self._poll_quote)
            self._quote_timer.start(self.poll_interval_sec * 1000)
            logger.info(f"FID 시세 폴링 시작 (매 {self.poll_interval_sec}초)")

        logger.info("자동매매 가동")

    def _poll_quote(self):
        try:
            q = self.client.get_quote(self.trader.quote_symbol)
            if q:
                self.trader.poll_quote(q, datetime.now())
        except Exception as e:
            logger.error(f"폴링 오류: {e}", exc_info=True)

    def stop(self):
        if self._quote_timer:
            try:
                self._quote_timer.stop()
            except Exception:
                pass
        self.trader.stop()
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
        logger.info("자동매매 종료")

    def _on_tick(self, symbol, price, volume, ts):
        self.trader.route_tick(symbol, price, volume, ts)

    def _on_order_execution(self, data: dict):
        logger.info(f"[체결통보] {data}")

    def _on_agent_event(self, event_type: int, param: int, str_param: str):
        logger.info(f"AgentEvent type={event_type} strParam={str_param}")


def main():
    validate_python_bits()
    config = load_config()
    try:
        validate_account_safety(config)
    except AccountSafetyError as e:
        logger.error(f"계좌 안전 검증 실패 — 봇 시작 거부\n{e}")
        sys.exit(2)

    app = ensure_qapp()

    # Windows + PyQt5 에서 Ctrl+C 가 이벤트 루프에 막히지 않도록
    from PyQt5.QtCore import QTimer
    _sig_pump = QTimer()
    _sig_pump.timeout.connect(lambda: None)
    _sig_pump.start(200)

    engine = TradingEngine(config)

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
        pass

    try:
        engine.start()
        logger.info("Ctrl+C 로 종료")
        app.exec_()
    finally:
        _cleanup("finally")


if __name__ == "__main__":
    main()
