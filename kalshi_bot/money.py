"""Exact money / quantity arithmetic for the Kalshi API.

Kalshi's current API surface expresses prices as *decimal dollar strings*
with up to six decimal places (e.g. ``"0.5600"``) and contract quantities as
*fixed-point strings* with two decimals (e.g. ``"10.00"``). Floating point is
not safe for either, so internally the bot uses:

- prices / cash amounts: integer **micro-dollars** (1 dollar = 1_000_000)
- contract quantities:   plain ``int`` whole contracts

Older payload variants (integer cents, plain ints) are still parsed
defensively because several endpoints kept legacy fields around.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

MICRO_PER_DOLLAR = 1_000_000
MICRO_PER_CENT = 10_000


def cents(n: float) -> int:
    """Micro-dollars for ``n`` cents (accepts fractional cents exactly enough)."""
    return int(round(n * MICRO_PER_CENT))


def usd_to_micro(value: Any) -> Optional[int]:
    """Parse a dollar amount (string or number) to integer micro-dollars."""
    if value is None:
        return None
    try:
        d = Decimal(str(value)) * MICRO_PER_DOLLAR
    except (InvalidOperation, ValueError):
        return None
    return int(d.to_integral_value(rounding="ROUND_HALF_UP"))


def micro_to_usd_str(micro: int) -> str:
    """Format micro-dollars as a dollar string for order payloads.

    Emits the shortest representation with at least 2 and at most 6 decimals
    (the API accepts up to 6): ``560000`` -> ``"0.56"``, ``5000`` -> ``"0.005"``.
    """
    sign = "-" if micro < 0 else ""
    micro = abs(int(micro))
    whole, frac = divmod(micro, MICRO_PER_DOLLAR)
    text = f"{frac:06d}".rstrip("0")
    if len(text) < 2:
        text = text.ljust(2, "0")
    return f"{sign}{whole}.{text}"


def micro_to_display(micro: int) -> str:
    """Human-friendly dollars, e.g. ``-1234500`` -> ``"-$1.23"``."""
    sign = "-" if micro < 0 else ""
    return f"{sign}${abs(micro) / MICRO_PER_DOLLAR:,.2f}"


def micro_to_cents_float(micro: int) -> float:
    return micro / MICRO_PER_CENT


def fp_to_count(value: Any, default: int = 0) -> int:
    """Parse a fixed-point quantity string/number to whole contracts.

    Truncates toward zero so fractional holdings are never over-counted.
    """
    if value is None:
        return default
    try:
        return int(Decimal(str(value)).to_integral_value(rounding="ROUND_DOWN"))
    except (InvalidOperation, ValueError):
        return default


def count_to_fp(count: int) -> str:
    """Whole contracts -> Kalshi fixed-point string: ``10`` -> ``"10.00"``."""
    return f"{int(count)}.00"


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def field_micro(payload: dict, base: str) -> Optional[int]:
    """Read a money field preferring the ``<base>_dollars`` string variant.

    Falls back to a legacy integer-cents field named ``base``.
    """
    dollars = payload.get(f"{base}_dollars")
    if dollars is not None:
        return usd_to_micro(dollars)
    legacy = payload.get(base)
    if legacy is None:
        return None
    try:
        return int(round(float(legacy))) * MICRO_PER_CENT
    except (TypeError, ValueError):
        return None


def field_count(payload: dict, base: str) -> Optional[int]:
    """Read a quantity field preferring the ``<base>_fp`` fixed-point variant."""
    fp = payload.get(f"{base}_fp")
    if fp is not None:
        return fp_to_count(fp)
    legacy = payload.get(base)
    if legacy is None:
        return None
    return fp_to_count(legacy)
