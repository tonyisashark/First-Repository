"""Pure, side-effect-free trading-strategy logic.

Keeping the decision making here (separate from API/IO) makes it unit-testable.

Strategy summary
----------------
The bot estimates each market's *true* probability purely from market data and
then takes whichever side of whichever market offers the largest risk-adjusted
edge -- YES or NO.

1. Probability estimate (maximized accuracy from market data alone):
   * base   = depth-weighted order-book midpoint over *all* levels (a
     multi-level Stoikov microprice: resting pressure near the touch pulls the
     estimate toward the opposite quote, deeper levels count with exponentially
     decaying weight);
   * smooth = EWMA over time (the bot loop owns the state), so one spoofed or
     transient quote cannot move the estimate by itself;
   * renorm = the buckets of one event are mutually exclusive and exhaustive,
     so their probabilities must sum to 100%; estimates are rescaled only when
     the book proves a real deviation (bids summing above 100c or asks below),
     which strips genuine overround while ignoring stale minimum-tick quotes;
   * gate   = a book wider than ``MAX_INFORMATIVE_SPREAD_CENTS`` carries no
     probability information and produces no estimate at all.

2. Edge: for each market, both sides are priced as a taker --
   YES at the ask, NO at ``100 - bid`` -- including Kalshi's taker fee
   (``0.07 * P * (1-P)`` per contract). ``edge = estimate - all-in cost``.

3. Selection: among sides with ``edge >= MIN_EDGE_CENTS``, rank by expected
   log-growth of the bankroll at the fraction that will actually be deployed
   (the configured portfolio fraction, capped at the Kelly fraction so a thin
   edge is never over-bet). The single highest-growth side is bought.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

# --- self-tuned constants (chosen from market structure, not user input) -----
# Kalshi's taker fee: 0.07 * price * (1 - price) dollars per contract.
TAKER_FEE_RATE = 0.07
# Minimum net edge (cents) required to trade: covers estimate noise so the bot
# only acts when the model and the price genuinely disagree.
MIN_EDGE_CENTS = 2.0
# Exit a held position when the market's bid overprices our side by this much
# (net of the exit fee) -- the model says cashing out now beats holding.
EXIT_EDGE_CENTS = 3.0
# A book wider than this carries no probability information -> no estimate.
MAX_INFORMATIVE_SPREAD_CENTS = 20
# An event's bucket estimates should sum to ~100 cents. Renormalize only when
# the sum is plausibly complete; far outside this window the data is suspect
# (buckets missing from the scan), so the raw estimate is safer.
RENORM_SUM_MIN = 80.0
RENORM_SUM_MAX = 125.0
# A bucket priced at the rails is settlement certainty, not market opinion:
# its probability is pinned, so renormalization must not rescale it (taxing a
# ~99.5c settled winner for the event's stale tail quotes would manufacture a
# phantom NO edge), and such markets carry nothing tradeable for a taker.
RAIL_MIN_CENTS = 3.0
RAIL_MAX_CENTS = 97.0
# Order-book level weighting: a level's quantity counts at half weight for
# every 3 cents it sits away from the best price (the touch dominates, depth
# behind it still matters).
LEVEL_DECAY_CENTS = 3.0
# EWMA half-life for smoothing the microprice estimate over time.
EWMA_HALF_LIFE_SECONDS = 20.0
# A book-based estimate older than this is stale and is not used.
ESTIMATE_MAX_AGE_SECONDS = 60.0
# Never open a position in a market closing sooner than this.
MIN_SECONDS_TO_CLOSE = 300


@dataclass
class MarketView:
    """Normalised snapshot of one market used by the strategy (prices in cents)."""

    ticker: str
    event_ticker: str
    yes_bid: Optional[int]
    yes_ask: Optional[int]
    last_price: Optional[int]
    volume: float
    close_time: Optional[datetime]
    status: str = "open"


@dataclass
class TradeCandidate:
    """One tradeable side of one market, with its edge and growth metrics."""

    market: MarketView
    side: str            # "yes" or "no"
    price_cents: int     # taker price for this side (the side's ask)
    chance_cents: float  # estimated probability this side wins (cents = %)
    cost_cents: float    # price + taker fee
    edge_cents: float    # chance - cost
    fraction: float      # bankroll fraction to deploy (Kelly-capped)
    growth: float        # expected log-growth per trade at ``fraction``


def parse_time(value: Optional[str]) -> Optional[datetime]:
    """Parse an RFC3339 / ISO-8601 timestamp into an aware UTC datetime."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def seconds_to_close(market: MarketView, now: Optional[datetime] = None) -> Optional[float]:
    """Seconds until the market closes, or ``None`` if the close time is unknown."""
    if market.close_time is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (market.close_time - now).total_seconds()


# --- probability estimation --------------------------------------------------
def mid_price_cents(market: MarketView) -> Optional[float]:
    """Bid/ask midpoint in cents. A missing bid counts as 0 (an empty bid side
    is a real statement, not missing data); no ask means no tradeable price."""
    if market.yes_ask is None:
        return None
    return ((market.yes_bid or 0) + market.yes_ask) / 2.0


def spread_cents(market: MarketView) -> Optional[int]:
    """Bid/ask spread in cents (missing bid counts as 0, so pinned/one-sided
    books show a huge spread and are treated as uninformative)."""
    if market.yes_ask is None:
        return None
    return market.yes_ask - (market.yes_bid or 0)


def informative(market: MarketView) -> bool:
    """True when the book is tight enough to carry probability information.

    A *crossed* view (bid above ask) is data skew -- e.g. one side updated by
    the realtime feed while the other is a stale scan -- not information.
    """
    spread = spread_cents(market)
    return spread is not None and 0 <= spread <= MAX_INFORMATIVE_SPREAD_CENTS


def microprice_cents(
    yes_bid: Optional[int],
    yes_ask: Optional[int],
    bid_depth: float,
    ask_depth: float,
) -> Optional[float]:
    """Depth-weighted midpoint (Stoikov micro-price) in cents.

    Weights each quote by the *opposite* side's depth, so heavy bidding pressure
    pulls the estimate toward the ask and vice versa. Falls back to the plain
    midpoint when either side's depth is unknown/empty.
    """
    if yes_bid is None or yes_ask is None:
        return None
    if bid_depth <= 0 or ask_depth <= 0:
        return (yes_bid + yes_ask) / 2.0
    return (yes_ask * bid_depth + yes_bid * ask_depth) / (bid_depth + ask_depth)


def renormalized_estimates(
    markets: List[MarketView],
    raw_estimates: Dict[str, float],
) -> Dict[str, float]:
    """Final per-ticker probability estimates (cents), renormalized per event.

    ``raw_estimates`` maps ticker -> smoothed microprice (or quoted mid) for
    the markets being estimated; only those tickers receive a final value.

    Renormalization is *arbitrage-grounded*: an event's mutually exclusive
    buckets must truly sum to 100%, but mid-sums are easily polluted by stale
    minimum-tick quotes (a dead event's 1c-bid/3c-ask tails add 2c of "mid"
    each and once manufactured a phantom NO edge on the settled winner). So
    estimates are scaled DOWN only when the event's BIDS sum above 100c --
    selling every bucket at bid would lock a profit, i.e. real money says the
    event is overpriced -- and scaled UP only when its ASKS sum below 100c
    (buying the whole event at ask would cost less than its certain payout).
    Anything between is spread-noise around parity and is left alone. The
    conservative side of the book is used for the factor, so the correction
    is never larger than what resting orders actually justify.

    Rail-priced values (outside ``RAIL_MIN/MAX_CENTS``) are settlement
    certainty, not opinion: they are never rescaled. Uninformative
    (wide-spread) books contribute nothing to the sums and get no estimate.
    """
    bid_sums: Dict[str, float] = {}
    ask_sums: Dict[str, float] = {}
    for market in markets:
        if not informative(market):
            continue
        event = market.event_ticker
        bid_sums[event] = bid_sums.get(event, 0.0) + (market.yes_bid or 0)
        ask_sums[event] = ask_sums.get(event, 0.0) + market.yes_ask

    final: Dict[str, float] = {}
    for market in markets:
        value = raw_estimates.get(market.ticker)
        if value is None:
            continue
        sum_bid = bid_sums.get(market.event_ticker, 0.0)
        sum_ask = ask_sums.get(market.event_ticker, 0.0)
        factor = 1.0
        if 100.0 < sum_bid <= RENORM_SUM_MAX:
            factor = 100.0 / sum_bid
        elif RENORM_SUM_MIN <= sum_ask < 100.0:
            factor = 100.0 / sum_ask
        if RAIL_MIN_CENTS < value < RAIL_MAX_CENTS:
            value *= factor
        final[market.ticker] = min(99.0, max(1.0, value))
    return final


# --- edge / growth mathematics ------------------------------------------------
def taker_fee_cents(price_cents: float) -> float:
    """Kalshi taker fee per contract, in cents: ``0.07 * P * (1-P)`` dollars."""
    return TAKER_FEE_RATE * price_cents * (100.0 - price_cents) / 100.0


def kelly_fraction(chance_cents: float, cost_cents: float) -> float:
    """Kelly-optimal bankroll fraction for a binary contract.

    Buying at all-in cost ``a`` (cents) with win probability ``p`` (cents),
    the log-growth-optimal fraction is ``(p - a) / (100 - a)``.
    """
    if cost_cents >= 100.0:
        return 0.0
    return max(0.0, (chance_cents - cost_cents) / (100.0 - cost_cents))


def growth_rate(chance_cents: float, cost_cents: float, fraction: float) -> float:
    """Expected log-growth of the bankroll for one trade at ``fraction``.

    ``p*ln(1 + f*(100-a)/a) + (1-p)*ln(1-f)`` -- the compounding-correct value
    of the bet: positive only when the edge genuinely beats the risk taken.
    """
    if fraction <= 0.0 or cost_cents <= 0.0 or cost_cents >= 100.0:
        return 0.0
    fraction = min(fraction, 0.99)
    p = min(0.999, max(0.001, chance_cents / 100.0))
    win = 1.0 + fraction * (100.0 - cost_cents) / cost_cents
    return p * math.log(win) + (1.0 - p) * math.log(1.0 - fraction)


def side_quotes(market: MarketView, side: str) -> Dict[str, Optional[int]]:
    """The bid/ask for one side, in that side's own terms.

    NO quotes are the YES book mirrored: ``no_bid = 100 - yes_ask`` and
    ``no_ask = 100 - yes_bid``.
    """
    if side == "yes":
        return {"bid": market.yes_bid, "ask": market.yes_ask}
    return {
        "bid": (100 - market.yes_ask) if market.yes_ask is not None else None,
        "ask": (100 - market.yes_bid) if market.yes_bid is not None else None,
    }


def evaluate_side(
    market: MarketView,
    side: str,
    estimate_cents: float,
    portfolio_fraction: float,
) -> Optional[TradeCandidate]:
    """Price one side of one market as a taker; ``None`` if it isn't tradeable."""
    ask = side_quotes(market, side)["ask"]
    if ask is None or not (1 <= ask <= 99):
        return None
    chance = estimate_cents if side == "yes" else 100.0 - estimate_cents
    cost = ask + taker_fee_cents(ask)
    edge = chance - cost
    fraction = min(portfolio_fraction, kelly_fraction(chance, cost))
    return TradeCandidate(
        market=market,
        side=side,
        price_cents=ask,
        chance_cents=chance,
        cost_cents=cost,
        edge_cents=edge,
        fraction=fraction,
        growth=growth_rate(chance, cost, fraction),
    )


def select_trade_candidates(
    markets: List[MarketView],
    *,
    estimates: Dict[str, float],
    portfolio_fraction: float,
    min_edge_cents: float = MIN_EDGE_CENTS,
    min_seconds_to_close: int = MIN_SECONDS_TO_CLOSE,
    now: Optional[datetime] = None,
) -> List[TradeCandidate]:
    """Every market side worth taking: edge >= the minimum and positive growth.

    ``estimates`` is the output of :func:`renormalized_estimates` -- only
    tickers with a (book-backed, fresh) estimate are eligible at all.
    """
    now = now or datetime.now(timezone.utc)
    candidates: List[TradeCandidate] = []
    for market in markets:
        estimate = estimates.get(market.ticker)
        if estimate is None:
            continue
        stc = seconds_to_close(market, now)
        if stc is not None and stc < min_seconds_to_close:
            continue
        for side in ("yes", "no"):
            cand = evaluate_side(market, side, estimate, portfolio_fraction)
            if cand is None:
                continue
            if cand.edge_cents >= min_edge_cents and cand.growth > 0.0:
                candidates.append(cand)
    return candidates


def best_candidate(candidates: List[TradeCandidate]) -> Optional[TradeCandidate]:
    """The single highest expected-log-growth side -- the greatest true edge."""
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.growth)


def position_size(
    balance_cents: int,
    fraction: float,
    price_cents: int,
    fractional: bool = True,
) -> float:
    """Contracts affordable with ``fraction`` of the balance.

    With ``fractional`` (Kalshi's fixed-point contracts, 0.01 granularity) the
    budget is deployed almost exactly -- e.g. $300 balance, 1/3, 90c ->
    111.11 contracts ($99.999). Without it, whole contracts: floor -> 111.
    """
    if price_cents <= 0:
        return 0.0
    raw = (balance_cents * fraction) / price_cents
    if fractional:
        return int(raw * 100 + 1e-9) / 100.0
    return float(int(raw + 1e-9))


def idle_watch_summary(
    markets: List[MarketView],
    *,
    estimates: Dict[str, float],
    portfolio_fraction: float,
    now: Optional[datetime] = None,
) -> str:
    """One-line, human-readable summary of what the bot sees while idle.

    Reports the universe size, how many sides currently clear the edge bar,
    and the best edge on offer (even when it is below the bar, so the operator
    can see how close the bot is to acting).
    """
    if not markets:
        return "watching 0 markets -- check Environment=prod and the series tickers"

    now = now or datetime.now(timezone.utc)
    n_events = len({m.event_ticker for m in markets})
    head = f"watching {len(markets)} markets / {n_events} events ({len(estimates)} estimated)"

    candidates = select_trade_candidates(
        markets, estimates=estimates, portfolio_fraction=portfolio_fraction, now=now,
    )
    if candidates:
        best = best_candidate(candidates)
        return (
            f"{head} | {len(candidates)} side(s) above the edge bar | best: "
            f"{best.market.ticker} {best.side.upper()} est {best.chance_cents:.1f}% "
            f"@ {best.price_cents}c edge {best.edge_cents:+.1f}c"
        )

    # Nothing clears the bar -- show the closest miss so progress is visible.
    # Markets without a book-backed estimate fall back to their renormalized
    # quoted mid (marked with ~) so the distance to the bar is always shown.
    rough = renormalized_estimates(
        markets,
        {
            m.ticker: mid
            for m in markets
            if informative(m) and (mid := mid_price_cents(m)) is not None
        },
    )
    merged = dict(rough)
    merged.update(estimates)
    near: Optional[TradeCandidate] = None
    for market in markets:
        estimate = merged.get(market.ticker)
        if estimate is None:
            continue
        mid = mid_price_cents(market)
        if mid is None or not (RAIL_MIN_CENTS <= mid <= RAIL_MAX_CENTS):
            continue  # effectively settled: nothing a taker could trade
        for side in ("yes", "no"):
            cand = evaluate_side(market, side, estimate, portfolio_fraction)
            if cand is not None and (near is None or cand.edge_cents > near.edge_cents):
                near = cand
    if near is None:
        return f"{head} | books too wide or one-sided to estimate (off-hours lull?)"
    approx = "" if near.market.ticker in estimates else "~"
    return (
        f"{head} | no side above the +{MIN_EDGE_CENTS:.0f}c edge bar | closest: "
        f"{near.market.ticker} {near.side.upper()} est {approx}{near.chance_cents:.1f}% "
        f"@ {near.price_cents}c edge {approx}{near.edge_cents:+.1f}c"
    )
