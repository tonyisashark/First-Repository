"""Event-level arbitrage on mutually exclusive markets.

In a Kalshi event flagged ``mutually_exclusive``, exactly one market settles
YES. Two structural mispricings are harvested:

- **Long set**: if the sum of YES asks across all outcomes is below the
  notional (after taker fees), buy one YES of each -- the set pays exactly
  one notional at settlement, whatever happens.
- **Short set**: if the sum of YES bids exceeds the notional (after fees),
  sell one YES of each (equivalently, buy every NO). All legs but one pay
  out, locking in ``sum(bids) - notional - fees``.

The long set additionally requires the outcomes to be *exhaustive* (some
market must resolve YES). That is verified structurally -- contiguous
floor/cap strikes with open ends -- or, failing that, by a market-consensus
proxy (sum of YES bids close to notional). Short sets need no exhaustiveness:
if nothing resolves YES, every NO leg pays and the trade does strictly
better.

Execution risk is legging: IOC legs are sized at a haircut of the visible
top-of-book depth, fired in one batch request, then verified; imbalances get
one repair attempt (chasing the missing legs within the opportunity's margin)
before any unmatched excess is unwound reduce-only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..fees import fill_fee_micro
from ..models import Event, Market, OrderBook
from .base import BotContext, Strategy

logger = logging.getLogger(__name__)

STRIKE_GAP_TOLERANCE = 1.0  # max gap between adjacent buckets still "contiguous"
MAX_OPPORTUNITIES_PER_CYCLE = 3


@dataclass
class Leg:
    market: Market
    price: int          # YES-terms price for the order
    available: int      # contracts at that price


@dataclass
class Opportunity:
    event: Event
    direction: str               # "long" (buy every YES) | "short" (sell every YES)
    legs: List[Leg]
    sets: int                    # number of complete sets to trade
    profit_per_set: int          # micro-dollars, net of fees
    capital_per_set: int         # cash needed per set (cost or collateral + fees)

    @property
    def total_profit(self) -> int:
        return self.profit_per_set * self.sets

    @property
    def total_capital(self) -> int:
        return self.capital_per_set * self.sets


class ArbitrageStrategy(Strategy):
    name = "arb"

    def step(self, ctx: BotContext) -> None:
        budget = ctx.budget(ctx.cfg.arb_budget_frac)
        used = ctx.used(self.name)
        if budget - used <= 0:
            return
        candidates = find_candidates(ctx.events, ctx.now, ctx.cfg)
        traded = 0
        for event in candidates:
            if traded >= MAX_OPPORTUNITIES_PER_CYCLE:
                break
            tickers = [m.ticker for m in event.markets]
            books = ctx.books(tickers)
            if len(books) < len(tickers):
                continue
            opp = plan_opportunity(event, books, ctx.cfg)
            if opp is None:
                continue
            opp = self._size_to_budget(ctx, opp, budget)
            if opp is None or opp.total_profit < ctx.cfg.arb_min_total_profit:
                continue
            logger.info("[arb] %s %s: %d sets x %d legs, profit/set %.2fc, total %.2f USD",
                        opp.direction, event.event_ticker, opp.sets, len(opp.legs),
                        opp.profit_per_set / 10_000, opp.total_profit / 1e6)
            self._execute(ctx, opp, books)
            # count committed capital against this cycle's budget immediately
            ctx.strategy_used[self.name] = ctx.used(self.name) + opp.total_capital
            traded += 1

    # ------------------------------------------------------------------ size
    def _size_to_budget(self, ctx: BotContext, opp: Opportunity,
                        budget: int) -> Optional[Opportunity]:
        view = ctx.view
        equity = view.equity
        event_key = opp.event.event_ticker
        headroom = [
            budget - ctx.used(self.name),
            int(ctx.cfg.arb_max_cost_frac * budget),
            int(ctx.cfg.max_event_exposure_frac * equity)
            - view.exposure_by_event.get(event_key, 0),
            int(ctx.cfg.max_total_exposure_frac * equity) - view.total_exposure,
            int(view.cash * 0.98),
        ]
        capital_cap = max(0, min(headroom))
        if opp.capital_per_set <= 0:
            return None
        sets = min(opp.sets, capital_cap // opp.capital_per_set)
        if sets < 1:
            return None
        return Opportunity(event=opp.event, direction=opp.direction, legs=opp.legs,
                           sets=sets, profit_per_set=opp.profit_per_set,
                           capital_per_set=opp.capital_per_set)

    # --------------------------------------------------------------- execute
    def _execute(self, ctx: BotContext, opp: Opportunity,
                 books: Dict[str, OrderBook]) -> None:
        side = "bid" if opp.direction == "long" else "ask"
        legs = [{
            "market": leg.market,
            "side": side,
            "price": leg.price,
            "count": opp.sets,
            "time_in_force": "immediate_or_cancel",
            "book": books.get(leg.market.ticker),
        } for leg in opp.legs]
        results = ctx.executor.batch_place(self.name, legs)
        fills = [r.get("filled", 0) for r in results]
        ctx.state.journal("arb_attempt", {
            "event": opp.event.event_ticker, "direction": opp.direction,
            "sets": opp.sets, "profit_per_set_micro": opp.profit_per_set,
            "fills": fills,
            "errors": [r.get("error") for r in results if r.get("error")],
        })
        if len(set(fills)) <= 1 and fills and fills[0] == opp.sets:
            return  # perfectly balanced
        self._repair(ctx, opp, fills, books)

    def _repair(self, ctx: BotContext, opp: Opportunity, fills: List[int],
                books: Dict[str, OrderBook]) -> None:
        """One chase for under-filled legs, then unwind any leftover excess."""
        side = "bid" if opp.direction == "long" else "ask"
        target = max(fills) if fills else 0
        if target == 0:
            return
        chase = ctx.cfg.arb_repair_slippage

        for i, leg in enumerate(opp.legs):
            deficit = target - fills[i]
            if deficit <= 0:
                continue
            price = leg.price + chase if side == "bid" else leg.price - chase
            result = ctx.executor.place(
                strategy=self.name, market=leg.market, side=side, price=price,
                count=deficit, time_in_force="immediate_or_cancel",
                book=books.get(leg.market.ticker),
            )
            fills[i] += result.get("filled", 0)

        floor = min(fills)
        for i, leg in enumerate(opp.legs):
            excess = fills[i] - floor
            if excess <= 0:
                continue
            unwind_side = "ask" if side == "bid" else "bid"
            concession = 2 * ctx.cfg.arb_repair_slippage
            price = leg.price - concession if side == "bid" else leg.price + concession
            result = ctx.executor.place(
                strategy=self.name, market=leg.market, side=unwind_side, price=price,
                count=excess, time_in_force="immediate_or_cancel", reduce_only=True,
                book=books.get(leg.market.ticker),
            )
            left = excess - result.get("filled", 0)
            if left > 0:
                ctx.state.journal("arb_unbalanced", {
                    "event": opp.event.event_ticker, "ticker": leg.market.ticker,
                    "excess_contracts": left, "direction": opp.direction,
                })
                logger.warning("[arb] %s left %d unmatched contracts on %s",
                               opp.event.event_ticker, left, leg.market.ticker)


# ---------------------------------------------------------------------------
# Pure planning functions (unit tested without any I/O)
# ---------------------------------------------------------------------------

def find_candidates(events: List[Event], now: int, cfg) -> List[Event]:
    """Cheap pre-filter using the quotes embedded in nested market payloads."""
    scored: List[tuple] = []
    for event in events:
        if not event.mutually_exclusive:
            continue
        markets = [m for m in event.markets if m.tradeable]
        if not 2 <= len(markets) <= cfg.arb_max_legs:
            continue
        if len(markets) != len(event.markets):
            continue  # a non-tradeable outcome breaks the set
        if any((m.close_ts or 0) < now + 120 for m in markets):
            continue
        notional = markets[0].notional
        if any(m.notional != notional for m in markets):
            continue
        asks = [m.yes_ask for m in markets]
        bids = [m.yes_bid for m in markets]
        margin = 0
        if all(a is not None and a > 0 for a in asks):
            margin = max(margin, notional - sum(asks))
        if all(b is not None for b in bids):
            margin = max(margin, sum(b or 0 for b in bids) - notional)
        if margin > 0:
            scored.append((margin, event))
    scored.sort(key=lambda pair: -pair[0])
    return [event for _, event in scored]


def plan_opportunity(event: Event, books: Dict[str, OrderBook], cfg) -> Optional[Opportunity]:
    """Depth-aware plan from live books; returns the better direction or None."""
    markets = [m for m in event.markets if m.tradeable]
    if len(markets) < 2:
        return None
    notional = markets[0].notional

    long_legs: List[Leg] = []
    short_legs: List[Leg] = []
    for market in markets:
        book = books.get(market.ticker)
        if book is None:
            return None
        asks = book.asks_for("yes")
        bids = book.bids_for("yes")
        if asks:
            long_legs.append(Leg(market, asks[0][0], asks[0][1]))
        if bids:
            short_legs.append(Leg(market, bids[0][0], bids[0][1]))

    best: Optional[Opportunity] = None
    if len(long_legs) == len(markets) and _exhaustive(event, markets, cfg, notional):
        best = _evaluate(event, "long", long_legs, notional, cfg)
    if len(short_legs) == len(markets):
        short = _evaluate(event, "short", short_legs, notional, cfg)
        if short and (best is None or short.profit_per_set > best.profit_per_set):
            best = short
    return best


def _evaluate(event: Event, direction: str, legs: List[Leg], notional: int,
              cfg) -> Optional[Opportunity]:
    sets = max(1, int(min(leg.available for leg in legs) * cfg.arb_depth_shave))
    sets = min(sets, min(leg.available for leg in legs))

    fees = sum(fill_fee_micro(leg.price, sets, cfg.taker_fee_bps, notional)
               for leg in legs)
    if direction == "long":
        cost = sum(leg.price for leg in legs) * sets + fees
        payout = notional * sets
        profit = payout - cost
        capital = cost
    else:
        collateral = sum(notional - leg.price for leg in legs) * sets
        payout = notional * (len(legs) - 1) * sets
        profit = payout - collateral - fees
        capital = collateral + fees
    if sets <= 0 or profit <= 0:
        return None
    per_set = profit // sets
    if per_set < cfg.arb_min_set_profit:
        return None
    return Opportunity(event=event, direction=direction, legs=legs, sets=sets,
                       profit_per_set=per_set, capital_per_set=(capital + sets - 1) // sets)


def _exhaustive(event: Event, markets: List[Market], cfg, notional: int) -> bool:
    """Must some market resolve YES? Structural check, then consensus proxy."""
    if covers_real_line(markets):
        return True
    bids = [m.yes_bid for m in markets]
    if all(b is not None for b in bids):
        return sum(b or 0 for b in bids) >= cfg.arb_exhaustive_bid_floor * notional
    return False


def covers_real_line(markets: List[Market]) -> bool:
    """True when floor/cap strikes tile the whole real line with open ends."""
    open_bottom = [m for m in markets if m.strike_type in ("less", "less_or_equal")]
    open_top = [m for m in markets if m.strike_type in ("greater", "greater_or_equal")]
    middles = [m for m in markets if m.strike_type == "between"]
    if len(open_bottom) != 1 or len(open_top) != 1:
        return False
    if len(middles) != len(markets) - 2:
        return False
    if any(m.floor_strike is None or m.cap_strike is None for m in middles):
        return False
    if open_bottom[0].cap_strike is None or open_top[0].floor_strike is None:
        return False
    chain = sorted(middles, key=lambda m: m.floor_strike)
    cursor = open_bottom[0].cap_strike
    for m in chain:
        if m.floor_strike - cursor > STRIKE_GAP_TOLERANCE:
            return False
        cursor = max(cursor, m.cap_strike)
    return open_top[0].floor_strike - cursor <= STRIKE_GAP_TOLERANCE
