"""Pure, side-effect-free trading-strategy logic.

Keeping the decision making here (separate from API/IO) makes it unit-testable.

Strategy summary
----------------
* Universe: daily-temperature *range* markets (one bucket = one market).
* Buy candidate: a market whose volume is >= 2/3 of the maximum-volume market
  AND whose *estimated chance* falls inside the configured [min, max] band.
* The chance estimate is built from market data rather than the raw displayed
  number (the last trade), which is noisy and can be stale:
    1. base  = depth-weighted midpoint ("microprice") of the order book when
       book depth is known, else the plain bid/ask midpoint;
    2. it is renormalized across the event: the buckets of one event are
       mutually exclusive and exhaustive, so their probabilities must sum to
       100% -- dividing by the event's actual mid-sum removes the structural
       overround (longshot bias);
    3. a maximum bid/ask spread gate rejects markets whose book is too wide to
       mean anything ("85 bid / 99 ask" is not a 92% belief).
  Smoothing of the microprice over time (EWMA) is the bot loop's job, since it
  needs state; the smoothed value is passed in via ``micro_estimates``.
* The "maximum volume" is the single highest-volume **range** market -- not an
  event-aggregate total.  Among all candidates the single best (highest volume)
  is entered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional


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


# An event's bucket mids should sum to ~100 cents. Renormalize only when the
# sum is plausibly complete; far outside this window the data is suspect
# (buckets missing from the scan, or a degenerate test universe), so the raw
# estimate is safer than a wildly scaled one.
RENORM_SUM_MIN = 80.0
RENORM_SUM_MAX = 125.0


def mid_price_cents(market: MarketView) -> Optional[float]:
    """Bid/ask midpoint in cents. A missing bid counts as 0 (an empty bid side
    is a real statement, not missing data); no ask means no tradeable price."""
    if market.yes_ask is None:
        return None
    return ((market.yes_bid or 0) + market.yes_ask) / 2.0


def spread_cents(market: MarketView) -> Optional[int]:
    """Bid/ask spread in cents (missing bid counts as 0, so pinned/one-sided
    books show a huge spread and get rejected by the spread gate)."""
    if market.yes_ask is None:
        return None
    return market.yes_ask - (market.yes_bid or 0)


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


def event_mid_sums(markets: List[MarketView]) -> Dict[str, float]:
    """Sum of bucket midpoints per event -- the event's actual "overround"."""
    sums: Dict[str, float] = {}
    for market in markets:
        mid = mid_price_cents(market)
        if mid is None:
            continue
        sums[market.event_ticker] = sums.get(market.event_ticker, 0.0) + mid
    return sums


def estimate_chance_cents(
    market: MarketView,
    event_sums: Dict[str, float],
    micro: Optional[float] = None,
) -> Optional[float]:
    """The market's estimated true chance in cents (= percent).

    ``micro`` is an externally computed (typically EWMA-smoothed) microprice for
    this market; without one the bid/ask midpoint is used. The base estimate is
    renormalized by its event's bucket-mid sum when that sum looks complete.
    """
    base = micro if micro is not None else mid_price_cents(market)
    if base is None:
        return None
    total = event_sums.get(market.event_ticker, 0.0)
    if RENORM_SUM_MIN <= total <= RENORM_SUM_MAX:
        return base * 100.0 / total
    return base


def has_liquidity(market: MarketView) -> bool:
    """True if the YES ask is a real, tradeable price (strictly between the rails).

    A bucket pinned at 0/1c (settled loser) or 99/100c (settled winner) has no
    meaningful two-sided liquidity, so it shouldn't be surfaced as a near-target
    market.
    """
    return market.yes_ask is not None and 1 < market.yes_ask < 99


def _group_markets(markets: List[MarketView], scope: str) -> Dict[str, List[MarketView]]:
    groups: Dict[str, List[MarketView]] = {}
    for market in markets:
        key = "__global__" if scope == "global" else market.event_ticker
        groups.setdefault(key, []).append(market)
    return groups


def volume_qualifying_markets(
    markets: List[MarketView],
    *,
    volume_threshold_ratio: float,
    scope: str = "global",
) -> List[MarketView]:
    """Markets whose volume is >= ``volume_threshold_ratio`` of their group's
    maximum-volume market (ignoring price). The shared volume filter used by both
    the buy rule and the heartbeat."""
    qualifying: List[MarketView] = []
    for group in _group_markets(markets, scope).values():
        volumes = [m.volume for m in group if m.volume is not None]
        if not volumes:
            continue
        max_volume = max(volumes)
        if max_volume <= 0:
            continue
        threshold = volume_threshold_ratio * max_volume
        qualifying.extend(m for m in group if m.volume is not None and m.volume >= threshold)
    return qualifying


def select_buy_candidates(
    markets: List[MarketView],
    *,
    min_chance_cents: int,
    max_chance_cents: int,
    volume_threshold_ratio: float,
    scope: str = "global",
    min_seconds_to_close: Optional[int] = None,
    max_spread_cents: Optional[int] = None,
    micro_estimates: Optional[Dict[str, float]] = None,
    now: Optional[datetime] = None,
) -> List[MarketView]:
    """Return every market that satisfies the buy rule.

    A market qualifies when, within its volume-comparison group, its volume is
    ``>= volume_threshold_ratio * max_group_volume``, its bid/ask spread is at
    most ``max_spread_cents`` (when set), and its estimated chance (see
    :func:`estimate_chance_cents`) lies inside
    ``[min_chance_cents, max_chance_cents]`` inclusive.

    ``micro_estimates`` maps ticker -> smoothed microprice in cents for markets
    where the bot has order-book data; other markets fall back to the midpoint.

    ``scope`` controls the comparison group for "maximum volume":
      * ``"global"`` -> compared against the single highest-volume range market
        across every monitored market at once -- the default.
      * ``"event"``  -> compared against other range markets in the same event
        (same city/day).
    """
    now = now or datetime.now(timezone.utc)
    micro_estimates = micro_estimates or {}
    event_sums = event_mid_sums(markets)

    candidates: List[MarketView] = []
    for market in volume_qualifying_markets(
        markets, volume_threshold_ratio=volume_threshold_ratio, scope=scope
    ):
        if max_spread_cents is not None:
            spread = spread_cents(market)
            if spread is None or spread > max_spread_cents:
                continue
        chance = estimate_chance_cents(market, event_sums, micro_estimates.get(market.ticker))
        if chance is None or not (min_chance_cents <= chance <= max_chance_cents):
            continue
        if min_seconds_to_close is not None:
            stc = seconds_to_close(market, now)
            if stc is not None and stc < min_seconds_to_close:
                continue
        candidates.append(market)
    return candidates


def pick_best_candidate(candidates: List[MarketView]) -> Optional[MarketView]:
    """Choose one market to trade (highest volume) since only one trade runs."""
    if not candidates:
        return None
    return max(candidates, key=lambda m: m.volume)


def position_size(balance_cents: int, portfolio_fraction: float, price_cents: int) -> int:
    """Number of contracts to buy with ``portfolio_fraction`` of the balance.

    ``floor( (balance * fraction) / price )`` -- e.g. $300 balance, 1/3, 90c
    -> floor(10000 / 90) = 111 contracts.
    """
    if price_cents <= 0:
        return 0
    budget_cents = int(balance_cents * portfolio_fraction)
    return budget_cents // price_cents


def idle_watch_summary(
    markets: List[MarketView],
    *,
    min_chance_cents: int,
    max_chance_cents: int,
    volume_threshold_ratio: float,
    scope: str = "global",
    min_seconds_to_close: Optional[int] = None,
    max_spread_cents: Optional[int] = None,
    micro_estimates: Optional[Dict[str, float]] = None,
    now: Optional[datetime] = None,
) -> str:
    """One-line, human-readable summary of what the bot is watching while idle.

    Reports how many markets/events are tracked, how many currently fall in the
    buy band, and (if none do) the volume-qualifying market whose estimated
    chance is closest to the band -- i.e. how close the bot is to triggering.
    """
    if not markets:
        return "watching 0 markets -- check Environment=prod and the series tickers"

    now = now or datetime.now(timezone.utc)
    n_events = len({m.event_ticker for m in markets})
    candidates = select_buy_candidates(
        markets,
        min_chance_cents=min_chance_cents,
        max_chance_cents=max_chance_cents,
        volume_threshold_ratio=volume_threshold_ratio,
        scope=scope,
        min_seconds_to_close=min_seconds_to_close,
        max_spread_cents=max_spread_cents,
        micro_estimates=micro_estimates,
        now=now,
    )
    parts = [
        f"watching {len(markets)} markets / {n_events} events",
        f"{len(candidates)} in {min_chance_cents}-{max_chance_cents}% chance band",
    ]
    # Only consider markets with real liquidity (ignore rail-priced 0/1/99/100
    # buckets) and a known estimate when reporting the closest market to the band.
    event_sums = event_mid_sums(markets)
    micro_estimates = micro_estimates or {}
    qualifying = [
        (m, estimate_chance_cents(m, event_sums, micro_estimates.get(m.ticker)))
        for m in volume_qualifying_markets(
            markets, volume_threshold_ratio=volume_threshold_ratio, scope=scope
        )
        if has_liquidity(m)
    ]
    qualifying = [(m, c) for m, c in qualifying if c is not None]
    if qualifying and not candidates:
        def distance(chance: float) -> float:
            return max(min_chance_cents - chance, chance - max_chance_cents, 0.0)

        closest, chance = min(qualifying, key=lambda pair: distance(pair[1]))
        parts.append(f"closest qualifying {closest.ticker} chance {chance:.1f}%")
    return " | ".join(parts)
