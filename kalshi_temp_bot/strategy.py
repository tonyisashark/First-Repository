"""Pure, side-effect-free trading-strategy logic.

Keeping the decision making here (separate from API/IO) makes it unit-testable.

Strategy summary
----------------
* Universe: daily-temperature *range* markets (one bucket = one market).
* Buy candidate: a market whose volume is >= 2/3 of the maximum-volume market
  AND whose YES *ask* is exactly the target price (90c).  The "maximum volume"
  is the single highest-volume **range** market -- not an event-aggregate total.
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


def select_buy_candidates(
    markets: List[MarketView],
    *,
    target_yes_price_cents: int,
    volume_threshold_ratio: float,
    scope: str = "global",
    min_seconds_to_close: Optional[int] = None,
    now: Optional[datetime] = None,
) -> List[MarketView]:
    """Return every market that satisfies the buy rule.

    A market qualifies when, within its volume-comparison group, its volume is
    ``>= volume_threshold_ratio * max_group_volume`` *and* its YES ask equals
    ``target_yes_price_cents``.

    ``scope`` controls the comparison group for "maximum volume":
      * ``"global"`` -> compared against the single highest-volume range market
        across every monitored market at once -- the default.
      * ``"event"``  -> compared against other range markets in the same event
        (same city/day).
    """
    now = now or datetime.now(timezone.utc)

    groups: Dict[str, List[MarketView]] = {}
    for market in markets:
        key = "__global__" if scope == "global" else market.event_ticker
        groups.setdefault(key, []).append(market)

    candidates: List[MarketView] = []
    for group in groups.values():
        volumes = [m.volume for m in group if m.volume is not None]
        if not volumes:
            continue
        max_volume = max(volumes)
        if max_volume <= 0:
            continue
        threshold = volume_threshold_ratio * max_volume
        for market in group:
            if market.volume is None or market.volume < threshold:
                continue
            if market.yes_ask != target_yes_price_cents:
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
