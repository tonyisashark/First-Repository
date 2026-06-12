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


def fixed_point_str(count: float) -> str:
    """Format a contract quantity as a fixed-point (2dp) string, e.g. ``"10.00"``."""
    return f"{int(count)}.00"


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


def orderbook_best_levels(orderbook: dict) -> dict:
    """Best YES bid/ask price and quantity from an orderbook, in cents.

    The book's ``yes`` side holds resting YES buys (bids). The YES *ask* side is
    derived from resting NO buys: a NO bid at ``q`` cents is an offer to take
    the other side of YES at ``100 - q``, so the best YES ask is ``100 - best
    NO bid``. Returns ``{"bid": price|None, "bid_qty": float, "ask": price|None,
    "ask_qty": float}``.
    """
    best_bid, bid_qty = None, 0.0
    for price, qty in _orderbook_levels(orderbook, "yes"):
        if best_bid is None or price > best_bid:
            best_bid, bid_qty = price, qty
    best_no, no_qty = None, 0.0
    for price, qty in _orderbook_levels(orderbook, "no"):
        if best_no is None or price > best_no:
            best_no, no_qty = price, qty
    return {
        "bid": best_bid,
        "bid_qty": bid_qty,
        "ask": (100 - best_no) if best_no is not None else None,
        "ask_qty": no_qty,
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
