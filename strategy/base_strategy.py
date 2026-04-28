from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from enum import Enum


class Signal(Enum):
    NONE       = "NONE"
    LONG       = "LONG"
    SHORT      = "SHORT"
    EXIT       = "EXIT"
    SCALE_IN   = "SCALE_IN"      # 피라미드 추가 진입 (방향은 현재 포지션과 동일)
    PARTIAL_TP = "PARTIAL_TP"    # 부분 익절 (보유의 일부만 청산)


@dataclass
class TradeSignal:
    signal: Signal = Signal.NONE
    entry_price: float = 0.0
    stop_loss: float = 0.0
    reason: str = ""
    atr: float = 0.0
    # ── 공격적 운용 확장 필드 ────────────────────────────────────
    scale_qty: int = 0
    """피라미드 추가 단위 수 (0=신규/없음, 1+=추가 N단위 진입).
    Signal.SCALE_IN 일 때 사용."""
    take_profit_targets: List[float] = field(default_factory=list)
    """부분 익절 R-multiple 목표. 예: [3.0] = +3R 도달 시 50% 청산."""
    partial_close_pct: float = 0.0
    """부분 청산 비율 (0.0 ~ 1.0). Signal.PARTIAL_TP 일 때 사용. 0.5 = 보유의 50%."""


class BaseStrategy(ABC):
    """
    전략 베이스 클래스.

    on_bar 시그니처는 멀티 타임프레임을 지원하기 위해 dict를 받습니다:
      data_dict = {"1D": DataHandler, "4H": DataHandler, "1H": DataHandler}

    단일 TF 전략은 data_dict["1D"] 등 자신의 TF만 사용하면 됩니다.
    하위 호환을 위해 단일 DataHandler를 직접 받는 경우도 처리합니다.
    """

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def on_bar(self, data) -> TradeSignal:
        """새 봉이 완성될 때 호출. 매매 신호를 반환.
        data: Dict[str, DataHandler] (멀티TF) 또는 단일 DataHandler.
        """
        ...

    @abstractmethod
    def get_min_bars_required(self) -> int:
        """전략 실행에 필요한 최소 봉 수 (메인 TF 기준)"""
        ...

    def get_required_timeframes(self) -> List[str]:
        """전략이 필요로 하는 타임프레임 리스트. 기본 일봉만."""
        return ["1D"]

    @staticmethod
    def _resolve_data(data, timeframe: str = "1D"):
        """data가 dict인지 단일 DataHandler인지 자동 판별."""
        if isinstance(data, dict):
            return data.get(timeframe)
        return data
