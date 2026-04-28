"""
Tier 3 어그레시브 운용 기능의 단위 테스트.

실행: pytest tests/test_aggressive.py -v
또는: python -m unittest tests.test_aggressive
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

# 테스트 경로에서 H1 모듈 임포트
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils.data_handler import DataHandler, OHLCVBar, TIMEFRAME_SECONDS
from strategy.base_strategy import Signal, TradeSignal
from strategy.multi_tf_aggressive import MultiTFAggressiveStrategy
from risk.risk_manager import RiskManager


# ── 1. DataHandler 멀티 TF 봉 집계 ──────────────────────────────────────────

class TestMultiTFAggregation(unittest.TestCase):

    def test_1h_aggregation_from_ticks(self):
        """1시간 동안의 60틱이 1H봉 1개로 합쳐지는지"""
        d = DataHandler("TEST", timeframe="1H")
        ts = datetime(2024, 1, 1, 10, 0, 0)
        for i in range(60):
            d.update_tick(0.65 + i * 0.0001, volume=1.0,
                          timestamp=ts + timedelta(minutes=i))
        # 모두 같은 1H 버킷이므로 봉은 아직 미확정 (current_bar에만 존재)
        self.assertEqual(d.bar_count(), 0)
        self.assertIsNotNone(d._current_bar)
        self.assertAlmostEqual(d._current_bar.open, 0.65, places=5)
        self.assertAlmostEqual(d._current_bar.close, 0.65 + 59 * 0.0001, places=5)

        # 다음 시간 첫 틱 → 이전 봉 자동 확정
        d.update_tick(0.70, timestamp=ts + timedelta(hours=1, minutes=1))
        self.assertEqual(d.bar_count(), 1)

    def test_4h_boundary_alignment(self):
        """4H 봉 경계가 UTC 0/4/8/12/16/20시로 정렬되는지"""
        d = DataHandler("TEST", timeframe="4H")
        # UTC 03:59 → 00:00 버킷
        ts1 = datetime(2024, 1, 1, 3, 59, 0)
        # UTC 04:00 → 04:00 버킷 (다음 봉)
        ts2 = datetime(2024, 1, 1, 4, 0, 0)
        d.update_tick(0.65, timestamp=ts1)
        d.update_tick(0.66, timestamp=ts2)
        self.assertEqual(d.bar_count(), 1)
        # 첫 봉의 open_ts가 00:00이어야 함
        self.assertEqual(d.bars[0].timestamp.hour, 0)


# ── 2. TradeSignal 확장 필드 ────────────────────────────────────────────────

class TestTradeSignalExtension(unittest.TestCase):

    def test_scale_qty_default(self):
        sig = TradeSignal(Signal.LONG, 0.65, 0.64)
        self.assertEqual(sig.scale_qty, 0)
        self.assertEqual(sig.partial_close_pct, 0.0)
        self.assertEqual(sig.take_profit_targets, [])

    def test_scale_in_signal(self):
        sig = TradeSignal(Signal.SCALE_IN, 0.66, 0.65, scale_qty=1)
        self.assertEqual(sig.signal, Signal.SCALE_IN)
        self.assertEqual(sig.scale_qty, 1)


# ── 3. RiskManager 피라미드 사이징 ──────────────────────────────────────────

class TestPyramidSizing(unittest.TestCase):

    def setUp(self):
        self.rm = RiskManager(
            account_equity_krw=30_000_000,
            risk_per_trade_pct=3.0,
            pyramid_size_decay=[1.0, 0.75, 0.5, 0.5],
        )

    def test_first_unit_full_size(self):
        result = self.rm.calc_pyramid_contracts(base_contracts=4, units_held=1)
        self.assertEqual(result, 4)

    def test_second_unit_75pct(self):
        result = self.rm.calc_pyramid_contracts(base_contracts=4, units_held=2)
        self.assertEqual(result, 3)  # int(4 * 0.75) = 3

    def test_third_unit_50pct(self):
        result = self.rm.calc_pyramid_contracts(base_contracts=4, units_held=3)
        self.assertEqual(result, 2)

    def test_fourth_unit_50pct(self):
        result = self.rm.calc_pyramid_contracts(base_contracts=4, units_held=4)
        self.assertEqual(result, 2)

    def test_minimum_one_contract(self):
        """base_contracts가 1이어도 최소 1계약은 보장"""
        result = self.rm.calc_pyramid_contracts(base_contracts=1, units_held=4)
        self.assertEqual(result, 1)

    def test_out_of_range_unit(self):
        result = self.rm.calc_pyramid_contracts(base_contracts=4, units_held=5)
        self.assertEqual(result, 0)


# ── 4. RiskManager 회로차단기 ───────────────────────────────────────────────

class TestCircuitBreaker(unittest.TestCase):

    def setUp(self):
        self.rm = RiskManager(
            account_equity_krw=30_000_000,
            daily_circuit_breaker_pct=6.0,
            consecutive_loss_halt=5,
        )

    def test_no_trigger_when_within_limit(self):
        self.rm._daily_loss_krw = 1_500_000  # 5%
        triggered, _ = self.rm.check_circuit_breaker()
        self.assertFalse(triggered)

    def test_daily_circuit_trigger(self):
        self.rm._daily_loss_krw = 2_000_000  # 6.67%
        triggered, reason = self.rm.check_circuit_breaker()
        self.assertTrue(triggered)
        self.assertIn("일일", reason)
        self.assertTrue(self.rm.is_halted)

    def test_consecutive_loss_trigger(self):
        for _ in range(5):
            self.rm.on_position_closed(-100_000)
        triggered, reason = self.rm.check_circuit_breaker()
        self.assertTrue(triggered)
        self.assertIn("연속", reason)

    def test_win_resets_consecutive_loss(self):
        for _ in range(3):
            self.rm.on_position_closed(-100_000)
        self.rm.on_position_closed(+200_000)  # win
        triggered, _ = self.rm.check_circuit_breaker()
        self.assertFalse(triggered)
        self.assertEqual(self.rm._consecutive_losses, 0)

    def test_can_open_blocked_during_halt(self):
        self.rm._daily_loss_krw = 2_000_000
        self.rm.check_circuit_breaker()  # 발동
        can, reason = self.rm.can_open_position()
        self.assertFalse(can)
        self.assertIn("정지", reason)

    def test_manual_reset(self):
        self.rm._daily_loss_krw = 2_000_000
        self.rm.check_circuit_breaker()
        self.rm.reset_halt()
        self.assertFalse(self.rm.is_halted)


# ── 5. MultiTFAggressiveStrategy 동작 ────────────────────────────────────────

class TestMultiTFStrategy(unittest.TestCase):

    def test_required_timeframes(self):
        s = MultiTFAggressiveStrategy()
        self.assertEqual(s.get_required_timeframes(), ["4H", "1H"])

    def test_rejects_non_dict_input(self):
        """단일 DataHandler 입력 시 NONE 시그널 반환"""
        s = MultiTFAggressiveStrategy()
        d = DataHandler("TEST", timeframe="4H")
        sig = s.on_bar(d)  # dict 아닌 단일 핸들러 → 거부
        self.assertEqual(sig.signal, Signal.NONE)

    def test_position_open_close_cycle(self):
        """진입 후 set_position 복원 검증"""
        s = MultiTFAggressiveStrategy()
        s.set_position("LONG", 0.6500, 0.6450)
        self.assertEqual(s.current_position, "LONG")
        self.assertEqual(s._entry_price, 0.6500)
        self.assertEqual(s._stop_loss, 0.6450)
        self.assertEqual(s.units_held, 1)

    def test_pyramid_state_reset_after_close(self):
        s = MultiTFAggressiveStrategy()
        s._open("LONG", 0.65, 0.64, atr_4h=0.005)
        self.assertEqual(s._units_held, 1)
        s._reset_position()
        self.assertEqual(s._units_held, 0)
        self.assertEqual(s.current_position, "NONE")


# ── 6. 통합: 4H ATR 기반 1단 사이즈 계산 (자본 적정성) ──────────────────────

class TestCapitalAdequacy(unittest.TestCase):

    def test_3pct_risk_on_4h_yields_at_least_1_contract(self):
        """3% 리스크 + 4H 1.5xATR 손절폭에서 1계약 이상 사이징되는지"""
        rm = RiskManager(account_equity_krw=30_000_000,
                         risk_per_trade_pct=3.0,
                         usd_krw_rate=1400.0)
        # 4H 평균 ATR 0.0020 가정 → 1.5×ATR = 0.0030 = 30틱 = $300/계약
        # 자본 USD: 30M / 1400 ≈ $21,400
        # 3% 리스크 ≈ $642
        # 계약수: int($642 / $300) = 2
        contracts = rm.calc_contracts(entry_price=0.6500, stop_loss=0.6470)
        self.assertGreaterEqual(contracts, 1)

    def test_1pct_risk_on_daily_yields_zero(self):
        """기존 문제 재현: 1% 리스크 + 일봉 2xATR로는 0계약"""
        rm = RiskManager(account_equity_krw=30_000_000,
                         risk_per_trade_pct=1.0,
                         usd_krw_rate=1400.0)
        # 일봉 ATR 0.006 가정 → 2xATR = 0.012 = 120틱 = $1,200/계약
        # 1% 리스크: $214 → 0계약
        contracts = rm.calc_contracts(entry_price=0.6500, stop_loss=0.6380)
        self.assertEqual(contracts, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
