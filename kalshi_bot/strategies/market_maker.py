"""Passive market making: earn the spread on liquid mid-range markets.

The bot rests post-only quotes on both sides of the book in the top-N
markets by 24h volume that pass liquidity/safety filters (wide enough
spread, price away from the 0/1 pins, enough time before close). Quotes
join the touch (no improving) and are skewed away from accumulated
inventory; once inventory on a side reaches its cap, that side stops
quoting and the position is worked off passively.

Adverse selection is the real cost in this business: quotes are pulled
near market close, in too-tight books, and whenever the risk layer halts
entries. Paper-mode fills for this strategy are optimistic (no queue
modeling) -- treat paper MM P&L as an upper bound and validate small in
demo first.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from ..models import Market, Order, OrderBook
from .base import BotContext, Strategy

logger = logging.getLogger(__name__)


class MarketMakerStrategy(Strategy):
    name = "mm"

    def step(self, ctx: BotContext) -> None:
        cfg = ctx.cfg
        budget = ctx.budget(cfg.mm_budget_frac)
        universe = select_universe(list(ctx.markets.values()), ctx.now, cfg)
        universe_tickers = {m.ticker for m in universe}
        mine = self._my_orders(ctx)

        # Tear down quotes in markets that left the universe; work off inventory.
        for ticker, orders in mine.items():
            if ticker not in universe_tickers:
                for order in orders:
                    ctx.executor.cancel(order.order_id)
        self._flatten_orphans(ctx, universe_tickers)

        if not universe:
            return
        blocked, _ = ctx.risk.entries_blocked()
        if blocked:
            for orders in mine.values():
                for order in orders:
                    ctx.executor.cancel(order.order_id)
            return

        cap_per_market = budget // max(len(universe), 1)
        books = ctx.books([m.ticker for m in universe])
        for market in universe:
            book = books.get(market.ticker)
            if book is None:
                continue
            self._quote_market(ctx, market, book, mine.get(market.ticker, []),
                               cap_per_market)

    # ------------------------------------------------------------------ quotes
    def _quote_market(self, ctx: BotContext, market: Market, book: OrderBook,
                      existing: List[Order], cap: int) -> None:
        cfg = ctx.cfg
        plan = desired_quotes(market, book, self._inventory_value(ctx, market),
                              cap, cfg)
        by_side: Dict[str, List[Order]] = {"bid": [], "ask": []}
        for order in existing:
            by_side.setdefault(order.side, []).append(order)

        for side in ("bid", "ask"):
            desired = plan.get(side)
            orders = by_side.get(side, [])
            if desired is None:
                for order in orders:
                    ctx.executor.cancel(order.order_id)
                continue
            price, size = desired
            tick = market.tick_at(price)
            keep: Optional[Order] = None
            for order in orders:
                if keep is None and abs(order.yes_price - price) <= \
                        cfg.mm_requote_ticks * tick and order.remaining >= max(1, size // 2):
                    keep = order
                else:
                    ctx.executor.cancel(order.order_id)
            if keep is not None:
                continue
            cost = price * size if side == "bid" else (market.notional - price) * size
            allowance = ctx.risk.allowance(
                ctx.view, market,
                strategy_budget=ctx.budget(cfg.mm_budget_frac),
                strategy_used=ctx.used(self.name))
            if cost > allowance:
                continue
            result = ctx.executor.place(
                strategy=self.name, market=market, side=side, price=price,
                count=size, time_in_force="good_till_canceled",
                post_only=cfg.mm_post_only, book=book,
            )
            if result.get("order_id"):
                ctx.view.exposure_by_market[market.ticker] = \
                    ctx.view.exposure_by_market.get(market.ticker, 0) + cost
                ctx.strategy_used[self.name] = ctx.used(self.name) + cost

    # ------------------------------------------------------------- inventory
    def _inventory_value(self, ctx: BotContext, market: Market) -> int:
        position = ctx.view.positions.get(market.ticker)
        if position is None or position.count == 0:
            return 0
        mid = market.mid or market.last or 0
        if position.count > 0:
            return position.count * mid
        # short YES: negative value scaled by the NO-side mark
        return position.count * (market.notional - mid)

    def _my_orders(self, ctx: BotContext) -> Dict[str, List[Order]]:
        mine: Dict[str, List[Order]] = {}
        for order in ctx.view.orders:
            if ctx.state.strategy_of_order(order.order_id) == self.name:
                mine.setdefault(order.ticker, []).append(order)
        return mine

    def _flatten_orphans(self, ctx: BotContext, universe_tickers: set) -> None:
        """Passively exit inventory in markets we no longer quote."""
        owner = ctx.state.latest_strategy_by_ticker()
        for ticker, position in ctx.view.positions.items():
            if ticker in universe_tickers or position.count == 0:
                continue
            if owner.get(ticker) != self.name:
                continue
            market = ctx.markets.get(ticker)
            if market is None or not market.tradeable:
                continue
            has_exit = any(o.ticker == ticker for o in ctx.view.orders)
            if has_exit:
                continue
            book = ctx.books([ticker]).get(ticker)
            if book is None:
                continue
            if position.count > 0:
                touch = book.best_ask("yes")
                side = "ask"
            else:
                touch = book.best_bid("yes")
                side = "bid"
            if touch is None:
                continue
            ctx.executor.place(
                strategy=self.name, market=market, side=side, price=touch,
                count=abs(position.count), time_in_force="good_till_canceled",
                post_only=True, reduce_only=True, book=book,
            )


# ---------------------------------------------------------------------------
# Pure helpers (unit tested without I/O)
# ---------------------------------------------------------------------------

def select_universe(markets: List[Market], now: int, cfg) -> List[Market]:
    min_close = now + int(cfg.mm_min_hours_to_close * 3600)
    eligible = []
    for market in markets:
        if not market.tradeable or market.close_ts is None:
            continue
        if market.close_ts < min_close:
            continue
        if market.volume_24h < cfg.mm_min_volume_24h:
            continue
        if market.yes_bid is None or market.yes_ask is None:
            continue
        if market.spread is None or market.spread < cfg.mm_min_spread:
            continue
        mid = market.mid or 0
        if not cfg.mm_band_low <= mid <= cfg.mm_band_high:
            continue
        eligible.append(market)
    eligible.sort(key=lambda m: -m.volume_24h)
    return eligible[: cfg.mm_top_n]


def desired_quotes(market: Market, book: OrderBook, inventory_value: int,
                   cap: int, cfg) -> Dict[str, Optional[tuple]]:
    """{"bid": (price, size) | None, "ask": (price, size) | None}."""
    plan: Dict[str, Optional[tuple]] = {"bid": None, "ask": None}
    best_bid = book.best_bid("yes")
    best_ask = book.best_ask("yes")
    if best_bid is None or best_ask is None or cap <= 0:
        return plan
    spread = best_ask - best_bid
    if spread < cfg.mm_min_spread:
        return plan

    tick = market.tick_at(best_bid)
    ratio = max(-1.0, min(1.0, inventory_value / cap)) if cap else 0.0
    skew = int(-ratio * cfg.mm_skew_ticks) * tick  # long -> quote lower

    bid_price = market.snap(best_bid + skew, up=False)
    ask_price = market.snap(best_ask + skew, up=True)
    bid_price = min(bid_price, best_bid)        # join, never improve
    ask_price = max(ask_price, best_ask)
    if ask_price - bid_price < cfg.mm_min_spread:
        return plan

    quote_budget = max(int(cap * cfg.mm_quote_frac), 1)
    bid_size = quote_budget // max(bid_price, 1)
    ask_size = quote_budget // max(market.notional - ask_price, 1)

    long_capped = inventory_value >= cap
    short_capped = inventory_value <= -cap
    if not long_capped and bid_size >= 1:
        plan["bid"] = (bid_price, bid_size)
    if not short_capped and ask_size >= 1:
        plan["ask"] = (ask_price, ask_size)
    return plan
