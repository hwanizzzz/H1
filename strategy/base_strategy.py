from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class Signal(Enum):
    NONE  = "NONE"
    LONG  = "LONG"
    SHORT = "SHORT"
    EXIT  = "EXIT"


@dataclass
class TradeSignal:
    signal: Signal = Signal.NONE
    entry_price: float = 0.0
    stop_loss: float = 0.0
    reason: str = ""
    atr: float = 0.0


class BaseStrategy(ABC):
    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def on_bar(self, data_handler) -> TradeSignal:
        """새 봉이 완성될 때 호출. 매매 신호를 반환."""
        ...

    @abstractmethod
    def get_min_bars_required(self) -> int:
        """전략 실행에 필요한 최소 봉 수"""
        ...
