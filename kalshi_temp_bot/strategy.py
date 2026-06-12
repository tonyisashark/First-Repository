"""Pure, side-effect-free trading-strategy logic.

Keeping the decision making here (separate from API/IO) makes it unit-testable.

Strategy summary
----------------
* Universe: daily-temperature *range* markets (one bucket = one market).
* Buy candidate: a market whose volume is >= 2/3 of the maximum-volume market
  AND whose chance is exactly the target.  "Chance" is the percentage Kalshi
  displays for each market: the last traded YES price (1 cent = 1%), which is
  *not* necessarily the current YES ask.  The "maximum volume" is the single
  highest-volume **range** market -- not an event-aggregate total.
* Only one trade at a time, so among all candidates we pick the single best
  (highest volume) to enter.
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


def market_chance(market: MarketView) -> Optional[int]:
    """The market's "chance" as displayed by Kalshi, in percent (= cents).

    Kalshi's Chance column shows the last traded YES price, not the current
    ask -- a market can show 11% chance while the YES ask sits at 10c.
    """
    return market.last_price


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
    target_chance_cents: int,
    volume_threshold_ratio: float,
    scope: str = "global",
    min_seconds_to_close: Optional[int] = None,
    now: Optional[datetime] = None,
) -> List[MarketView]:
    """Return every market that satisfies the buy rule.

    A market qualifies when, within its volume-comparison group, its volume is
    ``>= volume_threshold_ratio * max_group_volume`` *and* its chance -- the
    last traded YES price shown in Kalshi's Chance column, where 1 cent = 1% --
    equals ``target_chance_cents``.

    ``scope`` controls the comparison group for "maximum volume":
      * ``"global"`` -> compared against the single highest-volume range market
        across every monitored market at once -- the default.
      * ``"event"``  -> compared against other range markets in the same event
        (same city/day).
    """
    now = now or datetime.now(timezone.utc)

    candidates: List[MarketView] = []
    for market in volume_qualifying_markets(
        markets, volume_threshold_ratio=volume_threshold_ratio, scope=scope
    ):
        if market_chance(market) != target_chance_cents:
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
    target_chance_cents: int,
    volume_threshold_ratio: float,
    scope: str = "global",
    min_seconds_to_close: Optional[int] = None,
    now: Optional[datetime] = None,
) -> str:
    """One-line, human-readable summary of what the bot is watching while idle.

    Reports how many markets/events are tracked, how many currently match the buy
    rule, and (if none do) the volume-qualifying market whose chance is closest to
    the target -- i.e. how close the bot is to triggering.
    """
    if not markets:
        return "watching 0 markets -- check Environment=prod and the series tickers"

    now = now or datetime.now(timezone.utc)
    n_events = len({m.event_ticker for m in markets})
    candidates = select_buy_candidates(
        markets,
        target_chance_cents=target_chance_cents,
        volume_threshold_ratio=volume_threshold_ratio,
        scope=scope,
        min_seconds_to_close=min_seconds_to_close,
        now=now,
    )
    parts = [
        f"watching {len(markets)} markets / {n_events} events",
        f"{len(candidates)} at {target_chance_cents}% chance",
    ]
    # Only consider markets with real liquidity (ignore rail-priced 0/1/99/100
    # buckets) and a known chance when reporting the closest market to the target.
    qualifying = [
        m
        for m in volume_qualifying_markets(
            markets, volume_threshold_ratio=volume_threshold_ratio, scope=scope
        )
        if has_liquidity(m) and market_chance(m) is not None
    ]
    if qualifying and not candidates:
        closest = min(qualifying, key=lambda m: abs(market_chance(m) - target_chance_cents))
        parts.append(f"closest qualifying {closest.ticker} chance {market_chance(closest)}%")
    return " | ".join(parts)
