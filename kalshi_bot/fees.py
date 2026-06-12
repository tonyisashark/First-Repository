"""Kalshi trading-fee model.

Per the published fee schedule, the fee for a fill of ``C`` contracts at
dollar price ``P`` is::

    taker:  round_up_to_cent( 0.07   * C * P * (1 - P) )
    maker:  round_up_to_cent( 0.0175 * C * P * (1 - P) )

(maker fees are zero on many series; the quarter-rate above is the published
schedule for fee-charging series). Rates are configurable in basis points of
``C * P * (1-P)`` -- 0.07 == 700 bps. The bot deliberately *overestimates*
costs: every prospective trade is gated on expected value computed with these
fees, so a market that actually charges less simply clears the bar more
easily.
"""

from __future__ import annotations

from .money import MICRO_PER_CENT, MICRO_PER_DOLLAR

TAKER_BPS_DEFAULT = 700   # 0.07  * C * P * (1-P)
MAKER_BPS_DEFAULT = 175   # 0.0175 * C * P * (1-P)


def fill_fee_micro(
    price_micro: int,
    count: int,
    rate_bps: int,
    notional_micro: int = MICRO_PER_DOLLAR,
) -> int:
    """Fee in micro-dollars for ``count`` contracts at ``price_micro``.

    Rounded up to the next whole cent, matching the exchange's
    round-up-per-fill behaviour for standard ($0.01-precision) members.
    ``notional_micro`` is the contract settlement value (normally $1).
    """
    if count <= 0:
        return 0
    # Fees vanish at the boundaries (P=0 or P=1); clamp defensively.
    price_micro = min(max(price_micro, 0), notional_micro)
    if price_micro in (0, notional_micro):
        return 0
    # rate_bps/10_000 * count * (p/N) * (1 - p/N) * N  micro-dollars, exactly:
    numerator = rate_bps * count * price_micro * (notional_micro - price_micro)
    denominator = 10_000 * notional_micro
    exact = numerator // denominator
    if numerator % denominator:
        exact += 1
    # round up to whole cent
    remainder = exact % MICRO_PER_CENT
    if remainder:
        exact += MICRO_PER_CENT - remainder
    return exact


def per_contract_fee_micro(price_micro: int, rate_bps: int,
                           notional_micro: int = MICRO_PER_DOLLAR) -> float:
    """Unrounded per-contract fee (micro-dollars) for EV estimates on size>1."""
    p = price_micro / notional_micro
    return (rate_bps / 10_000) * p * (1 - p) * notional_micro
