"""Portfolio valuation: cash + resting escrow + conservative mark-to-market.

Equity is the compounding base -- every strategy budget is a fraction of the
number computed here, so as profits settle, position sizes grow
automatically (and shrink after losses).

Marks are conservative: a YES position is valued at the current YES *bid*
(what you could actually sell it for now), a NO position at the NO bid.
Resting bids escrow ``price x remaining``; resting asks escrow the
complement for whatever portion is not covered by a long position.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import Config
from .models import Market, Order, Position
from .money import field_micro
from .state import StateStore

logger = logging.getLogger(__name__)


@dataclass
class PortfolioView:
    ts: int
    cash: int
    positions: Dict[str, Position]
    orders: List[Order]
    markets: Dict[str, Market]
    mtm: int = 0
    resting_escrow: int = 0
    equity: int = 0
    exposure_by_market: Dict[str, int] = field(default_factory=dict)
    exposure_by_event: Dict[str, int] = field(default_factory=dict)
    exposure_by_series: Dict[str, int] = field(default_factory=dict)
    total_exposure: int = 0

    def market_exposure(self, ticker: str) -> int:
        return self.exposure_by_market.get(ticker, 0)


def series_of(market: Optional[Market], ticker: str) -> str:
    """Series key used for correlation caps; falls back to ticker prefix."""
    event = market.event_ticker if market else ""
    base = event or ticker
    return base.split("-", 1)[0]


def mark_position(position: Position, market: Optional[Market]) -> int:
    """Conservative liquidation value of a position, in micro-dollars."""
    if market is None or position.count == 0:
        return 0
    if position.count > 0:
        price = market.yes_bid if market.yes_bid is not None else (market.last or 0)
        return price * position.count
    no_bid = market.no_bid
    if no_bid is None and market.yes_ask is not None:
        no_bid = market.notional - market.yes_ask
    if no_bid is None and market.last is not None:
        no_bid = market.notional - market.last
    return (no_bid or 0) * (-position.count)


class PortfolioService:
    def __init__(self, cfg: Config, state: StateStore, client, paper=None) -> None:
        self.cfg = cfg
        self.state = state
        self.client = client
        self.paper = paper

    # ------------------------------------------------------------------ data
    def refresh(self, market_lookup: Dict[str, Market]) -> PortfolioView:
        if self.paper is not None:
            cash = self.paper.cash
            positions = self.paper.positions()
            orders = self.paper.orders()
        else:
            cash = field_micro(self.client.get_balance(), "balance") or 0
            positions = {}
            for raw in self.client.get_positions():
                pos = Position.from_payload(raw)
                if pos.count != 0:
                    positions[pos.ticker] = pos
            orders = [Order.from_payload(o) for o in self.client.get_orders("resting")]
            orders = [o for o in orders if o.remaining > 0]

        markets = self._market_quotes(set(positions) | {o.ticker for o in orders},
                                      market_lookup)
        view = PortfolioView(ts=int(time.time()), cash=cash, positions=positions,
                             orders=orders, markets=markets)
        self._value(view, paper=self.paper is not None)
        return view

    def _market_quotes(self, tickers: set, market_lookup: Dict[str, Market]
                       ) -> Dict[str, Market]:
        markets = {t: market_lookup[t] for t in tickers if t in market_lookup}
        missing = sorted(tickers - set(markets))
        if missing:
            try:
                for raw in self.client.get_markets(status=None, tickers=missing):
                    market = Market.from_payload(raw)
                    markets[market.ticker] = market
            except Exception as exc:  # pragma: no cover - network-shaped
                logger.warning("could not fetch quotes for %d position markets: %s",
                               len(missing), exc)
        return markets

    # ----------------------------------------------------------------- value
    def _value(self, view: PortfolioView, paper: bool = False) -> None:
        exposure: Dict[str, int] = {}
        mtm = 0
        for ticker, position in view.positions.items():
            value = mark_position(position, view.markets.get(ticker))
            mtm += value
            exposure[ticker] = exposure.get(ticker, 0) + value

        escrow = 0
        asks_by_ticker: Dict[str, List[Order]] = {}
        for order in view.orders:
            if order.side == "bid":
                amount = order.yes_price * order.remaining
                escrow += amount
                exposure[order.ticker] = exposure.get(order.ticker, 0) + amount
            else:
                asks_by_ticker.setdefault(order.ticker, []).append(order)
        for ticker, asks in asks_by_ticker.items():
            market = view.markets.get(ticker)
            notional = market.notional if market else 1_000_000
            position = view.positions.get(ticker)
            cover_left = max(position.count, 0) if position else 0
            for order in sorted(asks, key=lambda o: -o.yes_price):
                covered = min(order.remaining, cover_left)
                cover_left -= covered
                naked = order.remaining - covered
                amount = (notional - order.yes_price) * naked
                escrow += amount
                exposure[ticker] = exposure.get(ticker, 0) + amount

        view.mtm = mtm
        view.resting_escrow = escrow
        # Live: the exchange reserves balance for resting orders, so escrow is
        # part of equity. Paper: simulated cash is never reserved, so adding
        # escrow would double-count it (it still counts toward exposure caps).
        view.equity = view.cash + mtm + (0 if paper else escrow)
        view.exposure_by_market = exposure
        view.total_exposure = sum(exposure.values())

        by_event: Dict[str, int] = {}
        by_series: Dict[str, int] = {}
        for ticker, amount in exposure.items():
            market = view.markets.get(ticker)
            event = market.event_ticker if market and market.event_ticker else ticker
            by_event[event] = by_event.get(event, 0) + amount
            key = series_of(market, ticker)
            by_series[key] = by_series.get(key, 0) + amount
        view.exposure_by_event = by_event
        view.exposure_by_series = by_series

    # ------------------------------------------------------------- attribution
    def strategy_exposure(self, view: PortfolioView) -> Dict[str, int]:
        """Approximate deployed capital per strategy via order attribution."""
        owner = self.state.latest_strategy_by_ticker()
        out: Dict[str, int] = {}
        for ticker, amount in view.exposure_by_market.items():
            strategy = owner.get(ticker, "unattributed")
            out[strategy] = out.get(strategy, 0) + amount
        return out
