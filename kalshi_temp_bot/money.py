"""Unit / money helpers for the Kalshi API.

Kalshi's current "external-api" surface returns money as *decimal dollar
strings* (e.g. ``"0.90"``) and contract quantities as *fixed-point strings*
(e.g. ``"10.00"``).  Older / backwards-compatible hosts return integer **cents**
(e.g. ``90``) and plain integers.  To keep the trading logic simple and exact we
normalise everything to integer **cents** for prices and to ``float`` for
quantities, then convert back to the wire format only when placing orders.

Working in integer cents means the "exactly 90 cents" / "exactly 99 cents"
comparisons the strategy relies on are exact, with no floating point fuzz.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional


def to_float(value: Any, default: float = 0.0) -> float:
    """Best-effort float conversion that tolerates strings and ``None``."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def dollars_to_cents(value: Any) -> Optional[int]:
    """Convert a dollar amount (string or number) to integer cents.

    ``"0.90"`` -> ``90``.  Returns ``None`` when the value is missing.
    """
    if value is None:
        return None
    try:
        cents = (Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return int(cents)
    except (ArithmeticError, ValueError):
        return None


def cents_to_dollars_str(cents: int) -> str:
    """Convert integer cents to a 2-decimal dollar string for order payloads.

    ``90`` -> ``"0.90"``.
    """
    return str((Decimal(int(cents)) / Decimal(100)).quantize(Decimal("0.01")))


def floor_contracts(count: float, fractional: bool = True) -> float:
    """Quantize a contract count downward to what the venue can fill.

    Fractional markets fill in 0.01-contract steps; others in whole contracts.
    (The tiny epsilon absorbs float error so e.g. ``0.29 * 100`` doesn't floor
    to 28 hundredths.)
    """
    if fractional:
        return int(count * 100 + 1e-9) / 100.0
    return float(int(count + 1e-9))


def fixed_point_str(count: float) -> str:
    """Format a contract quantity as a fixed-point (2dp) string.

    ``10`` -> ``"10.00"``, ``2.5`` -> ``"2.50"`` -- the wire format for
    fractional (0.01-granularity) contract counts.
    """
    return f"{count:.2f}"


def market_price_cents(market: dict, base: str) -> Optional[int]:
    """Read a market price field in integer cents.

    ``base`` is the un-suffixed field name, e.g. ``"yes_ask"``.  Prefers the new
    ``<base>_dollars`` string field, falling back to a legacy integer-cents field.
    """
    dollar_key = f"{base}_dollars"
    if market.get(dollar_key) is not None:
        return dollars_to_cents(market[dollar_key])
    if market.get(base) is not None:
        try:
            return int(round(float(market[base])))
        except (TypeError, ValueError):
            return None
    return None


def market_volume(market: dict) -> float:
    """Total traded volume for a single market (handles ``volume_fp``/``volume``)."""
    for key in ("volume_fp", "volume"):
        if market.get(key) is not None:
            return to_float(market[key])
    return 0.0


def _orderbook_levels(orderbook: dict, side: str):
    """Yield ``(price_cents, quantity)`` for one side of an orderbook payload.

    Levels are ``[price, quantity]`` pairs under ``"yes"`` / ``"no"`` (legacy
    integer cents) or ``"yes_dollars"`` / ``"no_dollars"`` (decimal strings).
    """
    levels = orderbook.get(f"{side}_dollars")
    dollars = levels is not None
    if levels is None:
        levels = orderbook.get(side)
    for level in levels or []:
        try:
            price = dollars_to_cents(level[0]) if dollars else int(round(float(level[0])))
            qty = to_float(level[1])
        except (IndexError, TypeError, ValueError):
            continue
        if price is not None:
            yield price, qty


def orderbook_summary(orderbook: dict, decay_cents: float = 3.0) -> dict:
    """Best YES bid/ask plus depth measures from a full orderbook, in cents.

    The book's ``yes`` side holds resting YES buys (bids). The YES *ask* side is
    derived from resting NO buys: a NO bid at ``q`` cents is an offer to take
    the other side of YES at ``100 - q``, so the best YES ask is ``100 - best
    NO bid``.

    Two depth measures per side:
      * effective depth (``bid_eff``/``ask_eff``): level quantities weighted by
        ``0.5 ** (distance_from_best / decay_cents)`` -- pressure near the touch
        dominates, deeper levels still count;
      * raw totals (``yes_total``/``no_total``): everything resting on the
        side, i.e. how many contracts a sweep could fill against.
    """
    yes_levels = list(_orderbook_levels(orderbook, "yes"))
    no_levels = list(_orderbook_levels(orderbook, "no"))

    best_bid = max((p for p, _ in yes_levels), default=None)
    best_no = max((p for p, _ in no_levels), default=None)
    best_ask = (100 - best_no) if best_no is not None else None

    bid_eff = sum(
        qty * 0.5 ** ((best_bid - price) / decay_cents) for price, qty in yes_levels
    ) if best_bid is not None else 0.0
    ask_eff = sum(
        qty * 0.5 ** (((100 - price) - best_ask) / decay_cents) for price, qty in no_levels
    ) if best_ask is not None else 0.0

    return {
        "bid": best_bid,
        "ask": best_ask,
        "bid_eff": bid_eff,
        "ask_eff": ask_eff,
        "yes_total": sum(qty for _, qty in yes_levels),
        "no_total": sum(qty for _, qty in no_levels),
    }


def orderbook_bid_depth(orderbook: dict, side: str = "yes") -> float:
    """Total resting buy quantity on one side of an orderbook, in contracts.

    The orderbook payload lists price levels as ``[price, quantity]`` pairs under
    ``"yes"`` / ``"no"`` (legacy integer cents) or ``"yes_dollars"`` /
    ``"no_dollars"`` (decimal-string) keys.  The summed quantity is how many
    contracts could currently be sold into that side's bids.
    """
    return sum(qty for _, qty in _orderbook_levels(orderbook, side))


def position_contracts(position: dict) -> float:
    """Signed contract count for a position (positive = long YES)."""
    for key in ("position_fp", "position"):
        if position.get(key) is not None:
            return to_float(position[key])
    return 0.0


def balance_cents(balance_payload: dict) -> int:
    """Available cash balance in cents from a /portfolio/balance response."""
    if balance_payload.get("balance") is not None:
        return int(to_float(balance_payload["balance"]))
    if balance_payload.get("balance_dollars") is not None:
        return dollars_to_cents(balance_payload["balance_dollars"]) or 0
    return 0


def portfolio_value_cents(balance_payload: dict) -> Optional[int]:
    """Total portfolio value in cents, if the balance response exposes it."""
    if balance_payload.get("portfolio_value") is not None:
        return int(to_float(balance_payload["portfolio_value"]))
    if balance_payload.get("portfolio_value_dollars") is not None:
        return dollars_to_cents(balance_payload["portfolio_value_dollars"])
    return None
