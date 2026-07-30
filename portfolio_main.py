"""
다종목 포트폴리오 자동매매 메인 — 금(MGC) + S&P(MES) 동시 운용

실행:
  python portfolio_main.py

요구사항:
  - Windows + 하나증권 1QHTS 로그인 + pip install -r requirements.txt
  - config/config.yaml 의 symbols: 목록 설정
  - 정확한 종목코드/월물은 각 symbol 의 contract_code 로 지정
    (예: 금 4월물 → contract_code: "J26")

주의:
  - 실계좌 전 반드시 is_mock: true 로 1~2개월 모의투자 검증
  - 거래당 리스크 3% 공격형 설정 — 손실 변동성 큼, 소액부터 시작
"""

import sys
import time
from typing import Dict

from api.hana_api import HanaAPI
from risk.risk_manager import RiskManager, SymbolSpec
from strategy.factory import create_strategy
from engine.symbol_trader import SymbolTrader
from utils.logger import setup_logger
from utils.safety import validate_account_safety, AccountSafetyError, validate_python_bits
from utils.config_loader import load_config

logger = setup_logger("portfolio")


class PortfolioEngine:

    def __init__(self, config: dict):
        api_cfg = config["api"]
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

        # 계좌 단위 리스크 (다종목 공유)
        self.risk = RiskManager(
            account_equity_krw=risk_cfg["account_equity"],
            risk_per_trade_pct=risk_cfg["risk_per_trade_pct"],
            daily_loss_limit_pct=risk_cfg["daily_loss_limit_pct"],
            max_positions=risk_cfg["max_positions"],
            max_contracts_per_trade=risk_cfg["max_contracts_per_trade"],
            usd_krw_rate=risk_cfg.get("usd_krw_rate", 1350.0),
        )

        order_type = exec_cfg.get("order_type", "MARKET")

        # 종목별 트레이더 구성
        self.traders: list[SymbolTrader] = []
        for s in config["symbols"]:
            spec = SymbolSpec(s["code"], s["tick_size"], s["tick_value"], s.get("name", ""))
            strategy = create_strategy(s["strategy"])
            self.traders.append(SymbolTrader(
                spec=spec,
                timeframe=s.get("timeframe", "1D"),
                strategy=strategy,
                api=self.api,
                risk=self.risk,
                order_type=order_type,
                contract_code=s.get("contract_code", ""),
            ))

        self._route: Dict[str, SymbolTrader] = {}
        self._running = False

    def start(self):
        logger.info("=" * 60)
        logger.info(" 하나증권 다종목 포트폴리오 자동매매 시작")
        for t in self.traders:
            logger.info(f"  - {t.spec.code} ({t.spec.name}) / {t.strategy.name} / {t.timeframe}")
        logger.info(f" 계좌: {'모의투자' if self.api.is_mock else '실계좌'} | {self.risk.status_summary()}")
        logger.info("=" * 60)

        if not self.api.connect():
            logger.error("API 연결 실패. 종료.")
            sys.exit(1)

    def stop(self):
        self._running = False
        for t in self.traders:
            t.stop()
        self.api.disconnect()
        logger.info("포트폴리오 자동매매 종료")

    # ── 콜백 ─────────────────────────────────────────────────────────────────

    def _on_login(self, success: bool):
        if not success:
            logger.error("로그인 실패")
            return
        logger.info("로그인 완료 → 종목별 초기화")
        for t in self.traders:
            t.setup()
            self._route[t.active_symbol] = t
        self._running = True
        logger.info(f"포트폴리오 가동 ({len(self.traders)}종목 구독)")

    def _on_tick(self, symbol: str, price: float, volume: int, timestamp):
        trader = self._route.get(symbol)
        if trader:
            trader.on_tick(price, volume, timestamp)

    def _on_order_filled(self, order_no, symbol, qty, price, side):
        logger.info(f"체결 확인 | #{order_no} {symbol} {side} {qty}계약 @ {price:.5f}")


def main():
    validate_python_bits()
    config = load_config()
    if not config.get("symbols"):
        logger.error("config.yaml 에 symbols: 목록이 없습니다. (단일 종목은 main.py 사용)")
        sys.exit(1)

    try:
        validate_account_safety(config)
    except AccountSafetyError as e:
        logger.error(f"계좌 안전 검증 실패 — 봇 시작 거부\n{e}")
        sys.exit(2)

    engine = PortfolioEngine(config)
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
