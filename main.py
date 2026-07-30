"""
하나증권 1Q Open API - 단일 종목 자동매매 메인 (기본: CME 호주달러 6A)

실행:
  python main.py

다종목(금+S&P) 동시 운용은 portfolio_main.py 를 사용하세요.

요구사항:
  - Windows + 하나증권 1QHTS 로그인 + pip install -r requirements.txt
  - config/config.yaml 의 symbol: 블록 설정

주의:
  - 실계좌 전 반드시 is_mock: true 로 충분히 모의투자 검증
  - 거래 시간: CME (일~금, 1시간 점검 휴장)
"""

import sys
import time

from api.hana_api import HanaAPI
from risk.risk_manager import RiskManager, SymbolSpec
from strategy.factory import create_strategy
from engine.symbol_trader import SymbolTrader
from utils.logger import setup_logger
from utils.safety import validate_account_safety, AccountSafetyError, validate_python_bits
from utils.config_loader import load_config

logger = setup_logger("main")


class TradingEngine:
    """단일 종목 자동매매 엔진 (SymbolTrader 래퍼)."""

    def __init__(self, config: dict):
        api_cfg = config["api"]
        sym_cfg = config["symbol"]
        risk_cfg = config["risk"]
        exec_cfg = config["execution"]

        self.api = HanaAPI(
            account_no=api_cfg["account_no"],
            account_pw=api_cfg["account_pw"],
            is_mock=api_cfg["is_mock"],
            progid=api_cfg.get("progid"),
        )
        self.api.on_login = self._on_login
        self.api.on_tick = self._on_tick
        self.api.on_order_filled = self._on_order_filled

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
            api=self.api,
            risk=self.risk,
            order_type=exec_cfg.get("order_type", "MARKET"),
            contract_code=sym_cfg.get("contract_code", ""),
        )

    def start(self):
        logger.info("=" * 60)
        logger.info(" 하나증권 해외선물 자동매매 시작")
        logger.info(f" 전략: {self.trader.strategy.name} | 종목: {self.trader.spec.code} "
                    f"({'모의투자' if self.api.is_mock else '실계좌'})")
        logger.info("=" * 60)
        if not self.api.connect():
            logger.error("API 연결 실패. 종료.")
            sys.exit(1)

    def stop(self):
        self.trader.stop()
        self.api.disconnect()
        logger.info("자동매매 종료")

    def _on_login(self, success: bool):
        if not success:
            logger.error("로그인 실패")
            return
        logger.info("로그인 완료 → 초기화")
        self.trader.setup()
        logger.info("자동매매 가동")

    def _on_tick(self, symbol, price, volume, timestamp):
        if symbol == self.trader.active_symbol:
            self.trader.on_tick(price, volume, timestamp)

    def _on_order_filled(self, order_no, symbol, qty, price, side):
        logger.info(f"체결 확인 | #{order_no} {symbol} {side} {qty}계약 @ {price:.5f}")


def main():
    validate_python_bits()
    config = load_config()
    try:
        validate_account_safety(config)
    except AccountSafetyError as e:
        logger.error(f"계좌 안전 검증 실패 — 봇 시작 거부\n{e}")
        sys.exit(2)
    engine = TradingEngine(config)
    try:
        engine.start()
        logger.info("Ctrl+C로 종료")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("사용자 종료 요청")
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
