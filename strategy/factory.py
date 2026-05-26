"""전략 생성 팩토리 — config 딕셔너리로부터 전략 인스턴스를 만든다."""

from strategy.base_strategy import BaseStrategy
from strategy.donchian_breakout import DonchianBreakoutStrategy
from strategy.ma_crossover import MACrossoverStrategy
from strategy.mean_reversion import MeanReversionStrategy


def create_strategy(strat_cfg: dict) -> BaseStrategy:
    """
    strat_cfg 예:
      {"name": "donchian_breakout", "donchian_entry_period": 20, ...}
    """
    name = strat_cfg.get("name", "donchian_breakout")

    if name == "donchian_breakout":
        return DonchianBreakoutStrategy(
            entry_period=strat_cfg.get("donchian_entry_period", 20),
            exit_period=strat_cfg.get("donchian_exit_period", 10),
            atr_period=strat_cfg.get("atr_period", 14),
            atr_multiplier=strat_cfg.get("atr_multiplier", 2.0),
        )

    if name == "ma_crossover":
        return MACrossoverStrategy(
            fast_period=strat_cfg.get("fast_period", 9),
            slow_period=strat_cfg.get("slow_period", 21),
            adx_period=strat_cfg.get("adx_period", 14),
            adx_threshold=strat_cfg.get("adx_threshold", 25.0),
            atr_period=strat_cfg.get("atr_period", 14),
            atr_multiplier=strat_cfg.get("atr_multiplier", 1.5),
        )

    if name == "mean_reversion":
        return MeanReversionStrategy(
            bb_period=strat_cfg.get("bb_period", 20),
            bb_std=strat_cfg.get("bb_std", 2.0),
            rsi_period=strat_cfg.get("rsi_period", 14),
            rsi_oversold=strat_cfg.get("rsi_oversold", 30.0),
            rsi_overbought=strat_cfg.get("rsi_overbought", 70.0),
            atr_period=strat_cfg.get("atr_period", 14),
            atr_multiplier=strat_cfg.get("atr_multiplier", 2.5),
        )

    raise ValueError(f"알 수 없는 전략: {name}")


AVAILABLE_STRATEGIES = ["donchian_breakout", "ma_crossover", "mean_reversion"]
