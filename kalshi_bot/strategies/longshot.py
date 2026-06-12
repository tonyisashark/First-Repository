"""Favorite-side harvesting (the flip side of longshot bias).

Prediction markets persistently exhibit *favorite-longshot bias*: cheap
longshots trade above their true probability and heavy favorites below it.
This strategy buys the favorite side (YES **or** NO) when it is priced in a
configurable band (default 90-97 cents), close to resolution, and the
expected value after taker fees clears a threshold under a conservative
calibration assumption (``true probability ~ price + LONGSHOT_EDGE_CENTS``).

Run ``kalshi-bot calibrate`` to measure that edge from the exchange's own
recently settled markets instead of trusting the default.

Positions are diversified hard: per-market, per-event and per-series caps
all apply, sized by fractional Kelly on the assumed edge. Contracts are held
to settlement (they are short-dated by construction).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from ..fees import per_contract_fee_micro
from ..models import Market
from .base import BotContext, Strategy

logger = logging.getLogger(__name__)

MAX_ENTRIES_PER_RUN = 10


@dataclass
class Candidate:
    market: Market
    side: str            # outcome side: "yes" | "no"
    ask: int             # cost per contract for that side, micro-dollars
    ev: int              # estimated EV per contract after fees, micro-dollars
    p_hat: float


class LongshotStrategy(Strategy):
    name = "longshot"

    def step(self, ctx: BotContext) -> None:
        budget = ctx.budget(ctx.cfg.longshot_budget_frac)
        used = ctx.used(self.name)
        if budget - used <= 0:
            return
        candidates = scan_candidates(ctx.markets.values(), ctx.now, ctx.cfg)
        entries = 0
        for cand in candidates:
            if entries >= MAX_ENTRIES_PER_RUN:
                break
            if ctx.used(self.name) >= budget:
                break
            if self._enter(ctx, cand, budget):
                entries += 1

    # ----------------------------------------------------------------- entry
    def _enter(self, ctx: BotContext, cand: Candidate, budget: int) -> bool:
        cfg = ctx.cfg
        market = cand.market
        view = ctx.view

        # Idempotence across cycles: respect the tighter longshot per-market cap
        per_market_cap = int(cfg.longshot_per_market_frac * view.equity)
        already = view.exposure_by_market.get(market.ticker, 0)
        if already >= per_market_cap:
            return False

        book = ctx.books([market.ticker]).get(market.ticker)
        if book is None:
            return False
        asks = book.asks_for(cand.side)
        if not asks:
            return False
        ask, depth = asks[0]
        appraisal = appraise(market, cand.side, ask, cfg)
        if appraisal is None:
            return False
        p_hat, ev, cost_per_contract = appraisal

        count = ctx.risk.kelly_count(p_hat, cost_per_contract, market.notional,
                                     view.equity)
        allowance = ctx.risk.allowance(view, market,
                                       strategy_budget=budget,
                                       strategy_used=ctx.used(self.name))
        spend_cap = min(allowance, per_market_cap - already)
        count = min(count,
                    spend_cap // max(cost_per_contract, 1),
                    int(depth * cfg.arb_depth_shave) or 1)
        if count < 1:
            return False

        side = "bid" if cand.side == "yes" else "ask"
        price = ask if cand.side == "yes" else market.notional - ask
        result = ctx.executor.place(
            strategy=self.name, market=market, side=side, price=price,
            count=count, time_in_force="immediate_or_cancel", book=book,
        )
        filled = result.get("filled", 0)
        if filled > 0:
            spent = (result.get("avg_price") or price) * filled
            if cand.side == "no":
                spent = (market.notional - (result.get("avg_price") or price)) * filled
            ctx.strategy_used[self.name] = ctx.used(self.name) + spent
            view.exposure_by_market[market.ticker] = already + spent
            ctx.state.journal("longshot_entry", {
                "ticker": market.ticker, "side": cand.side, "count": filled,
                "cost_micro": spent, "p_hat": round(p_hat, 4), "ev_micro": ev,
            })
        return filled > 0


# ---------------------------------------------------------------------------
# Pure scanning / appraisal helpers (unit tested without I/O)
# ---------------------------------------------------------------------------

def appraise(market: Market, side: str, ask: int, cfg) -> Optional[tuple]:
    """(p_hat, ev_per_contract, all_in_cost) for buying ``side`` at ``ask``.

    Uses the *unrounded* per-contract fee: real entries are multi-contract,
    so the exchange's round-up-to-a-cent-per-fill washes out. ``side`` is
    accepted for symmetry/logging; the fee curve is symmetric in price.
    """
    notional = market.notional
    if not cfg.longshot_min_price <= ask <= cfg.longshot_max_price:
        return None
    p_hat = min((ask + cfg.longshot_edge) / notional, 0.995)
    fee = per_contract_fee_micro(ask, cfg.taker_fee_bps, notional)
    ev = round(p_hat * notional - ask - fee)
    if ev < cfg.longshot_min_ev:
        return None
    return p_hat, ev, ask + round(fee)


def scan_candidates(markets, now: int, cfg) -> List[Candidate]:
    """Filter the cached universe down to favorite-side opportunities."""
    out: List[Candidate] = []
    min_close = now + int(cfg.longshot_min_hours_to_close * 3600)
    max_close = now + int(cfg.longshot_max_days_to_close * 86400)
    for market in markets:
        if not market.tradeable or market.close_ts is None:
            continue
        if not min_close <= market.close_ts <= max_close:
            continue
        if market.volume_24h < cfg.longshot_min_volume_24h:
            continue
        for side in ("yes", "no"):
            ask = market.yes_ask if side == "yes" else market.no_ask
            if ask is None and side == "no" and market.yes_bid is not None:
                ask = market.notional - market.yes_bid
            if ask is None:
                continue
            appraisal = appraise(market, side, ask, cfg)
            if appraisal is not None:
                out.append(Candidate(market=market, side=side, ask=ask,
                                     ev=appraisal[1], p_hat=appraisal[0]))
    out.sort(key=lambda c: -c.ev)
    return out
