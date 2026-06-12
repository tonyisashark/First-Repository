"""Strategy interface and the per-cycle context handed to each strategy."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Callable, Dict, List

from ..config import Config
from ..execution import Executor
from ..models import Event, Market, OrderBook
from ..portfolio import PortfolioView
from ..risk import RiskManager
from ..state import StateStore


@dataclass
class BotContext:
    cfg: Config
    now: int
    view: PortfolioView
    events: List[Event]
    markets: Dict[str, Market]
    books: Callable[[List[str]], Dict[str, OrderBook]]
    executor: Executor
    risk: RiskManager
    state: StateStore
    strategy_used: Dict[str, int] = field(default_factory=dict)

    def budget(self, frac: float) -> int:
        return int(self.view.equity * frac)

    def used(self, strategy: str) -> int:
        return self.strategy_used.get(strategy, 0)


class Strategy(abc.ABC):
    name: str = "strategy"

    @abc.abstractmethod
    def step(self, ctx: BotContext) -> None:
        """Run one decision cycle. Must be safe to call repeatedly."""
