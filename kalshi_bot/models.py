"""Typed views over Kalshi API payloads.

All parsing is defensive: the API has both current (``*_dollars`` /
``*_fp`` string) and legacy (integer-cents / plain-int) field variants, and
this module accepts either. Prices become integer micro-dollars, quantities
whole-contract ints, timestamps epoch seconds.

Orderbook semantics: Kalshi keeps a single unified book per market holding
*bids only* -- a resting bid for NO at price ``q`` is exactly an offer (ask)
to sell YES at ``notional - q``. Buying YES therefore consumes NO-bid levels
and vice versa.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from .money import (
    MICRO_PER_CENT,
    MICRO_PER_DOLLAR,
    field_count,
    field_micro,
    fp_to_count,
    to_float,
    usd_to_micro,
)

Level = Tuple[int, int]  # (price_micro, contracts)


def parse_time(value: Any) -> Optional[int]:
    """ISO-8601 string or epoch number -> epoch seconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return int(_dt.datetime.fromisoformat(text).timestamp())
    except (ValueError, OSError):
        return None


# --------------------------------------------------------------------------
# Markets
# --------------------------------------------------------------------------

@dataclass
class Market:
    ticker: str
    event_ticker: str = ""
    status: str = ""
    result: str = ""
    title: str = ""
    close_ts: Optional[int] = None
    yes_bid: Optional[int] = None      # micro-dollars
    yes_ask: Optional[int] = None
    no_bid: Optional[int] = None
    no_ask: Optional[int] = None
    last: Optional[int] = None
    volume: int = 0
    volume_24h: int = 0
    open_interest: int = 0
    notional: int = MICRO_PER_DOLLAR
    strike_type: str = ""
    floor_strike: Optional[float] = None
    cap_strike: Optional[float] = None
    tick_ranges: List[Tuple[int, int, int]] = field(default_factory=list)  # (start, end, step) micro
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, m: dict) -> "Market":
        ranges: List[Tuple[int, int, int]] = []
        raw_ranges = m.get("price_ranges")
        if raw_ranges is None and isinstance(m.get("price_level_structure"), dict):
            raw_ranges = m["price_level_structure"].get("price_ranges")
        for r in raw_ranges or []:
            start = usd_to_micro(r.get("start"))
            end = usd_to_micro(r.get("end"))
            step = usd_to_micro(r.get("step"))
            if step and step > 0:
                ranges.append((start or 0, end or MICRO_PER_DOLLAR, step))
        return cls(
            ticker=m.get("ticker", ""),
            event_ticker=m.get("event_ticker", ""),
            status=(m.get("status") or "").lower(),
            result=(m.get("result") or "").lower(),
            title=m.get("yes_sub_title") or m.get("title") or "",
            close_ts=parse_time(m.get("close_time")),
            yes_bid=field_micro(m, "yes_bid"),
            yes_ask=field_micro(m, "yes_ask"),
            no_bid=field_micro(m, "no_bid"),
            no_ask=field_micro(m, "no_ask"),
            last=field_micro(m, "last_price"),
            volume=field_count(m, "volume") or 0,
            volume_24h=field_count(m, "volume_24h") or 0,
            open_interest=field_count(m, "open_interest") or 0,
            notional=field_micro(m, "notional_value") or MICRO_PER_DOLLAR,
            strike_type=(m.get("strike_type") or "").lower(),
            floor_strike=(float(m["floor_strike"]) if m.get("floor_strike") is not None else None),
            cap_strike=(float(m["cap_strike"]) if m.get("cap_strike") is not None else None),
            tick_ranges=ranges,
            raw=m,
        )

    # -- convenience ------------------------------------------------------
    @property
    def tradeable(self) -> bool:
        return self.status in ("active", "open")

    def seconds_to_close(self, now_ts: int) -> Optional[int]:
        if self.close_ts is None:
            return None
        return self.close_ts - now_ts

    @property
    def spread(self) -> Optional[int]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    @property
    def mid(self) -> Optional[int]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) // 2

    def tick_at(self, price: int) -> int:
        for start, end, step in self.tick_ranges:
            if start <= price <= end:
                return step
        return MICRO_PER_CENT

    def snap(self, price: int, *, up: bool) -> int:
        """Snap ``price`` onto the market's tick grid, rounding up or down,
        clamped inside (0, notional) so it is always a placeable price."""
        step = self.tick_at(price)
        q, r = divmod(price, step)
        if r and up:
            q += 1
        snapped = q * step
        lo = self.tick_at(0)
        hi = self.notional - self.tick_at(self.notional)
        return max(lo, min(snapped, hi))


# --------------------------------------------------------------------------
# Orderbook
# --------------------------------------------------------------------------

@dataclass
class OrderBook:
    """Unified book: ``yes_bids`` / ``no_bids`` sorted best (highest) first."""

    yes_bids: List[Level] = field(default_factory=list)
    no_bids: List[Level] = field(default_factory=list)
    notional: int = MICRO_PER_DOLLAR

    @classmethod
    def from_payload(cls, payload: dict, notional: int = MICRO_PER_DOLLAR) -> "OrderBook":
        body = payload.get("orderbook_fp") or payload.get("orderbook") or payload or {}

        def levels(side_fp: str, side_legacy: str) -> List[Level]:
            out: List[Level] = []
            rows = body.get(side_fp)
            if rows is not None:
                for row in rows or []:
                    price = usd_to_micro(row[0])
                    count = fp_to_count(row[1])
                    if price is not None and count > 0:
                        out.append((price, count))
            else:
                for row in body.get(side_legacy) or []:
                    price = int(round(to_float(row[0]))) * MICRO_PER_CENT
                    count = fp_to_count(row[1])
                    if count > 0:
                        out.append((price, count))
            out.sort(key=lambda lv: -lv[0])
            return out

        return cls(
            yes_bids=levels("yes_dollars", "yes"),
            no_bids=levels("no_dollars", "no"),
            notional=notional,
        )

    # -- derived quotes -----------------------------------------------------
    def asks_for(self, side: str) -> List[Level]:
        """Offers to *buy* ``side`` against: list of (price, count), best first.

        Buying YES lifts NO bids at complementary prices; buying NO lifts YES
        bids. Returned prices are in terms of ``side``.
        """
        source = self.no_bids if side == "yes" else self.yes_bids
        return [(self.notional - price, count) for price, count in source]

    def bids_for(self, side: str) -> List[Level]:
        return list(self.yes_bids if side == "yes" else self.no_bids)

    def best_bid(self, side: str) -> Optional[int]:
        bids = self.bids_for(side)
        return bids[0][0] if bids else None

    def best_ask(self, side: str) -> Optional[int]:
        asks = self.asks_for(side)
        return asks[0][0] if asks else None

    def depth_at_or_better(self, side: str, limit_price: int) -> int:
        """Contracts of ``side`` purchasable at <= ``limit_price``."""
        return sum(c for p, c in self.asks_for(side) if p <= limit_price)

    def cost_to_buy(self, side: str, count: int) -> Optional[Tuple[int, int]]:
        """(total_cost_micro, attainable_count) sweeping the book for ``count``."""
        total = 0
        got = 0
        for price, avail in self.asks_for(side):
            take = min(avail, count - got)
            total += take * price
            got += take
            if got >= count:
                break
        if got == 0:
            return None
        return total, got


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

@dataclass
class Event:
    event_ticker: str
    series_ticker: str = ""
    title: str = ""
    mutually_exclusive: bool = False
    markets: List[Market] = field(default_factory=list)

    @classmethod
    def from_payload(cls, e: dict) -> "Event":
        return cls(
            event_ticker=e.get("event_ticker", ""),
            series_ticker=e.get("series_ticker", ""),
            title=e.get("title", ""),
            mutually_exclusive=bool(e.get("mutually_exclusive")),
            markets=[Market.from_payload(m) for m in e.get("markets") or []],
        )


# --------------------------------------------------------------------------
# Portfolio objects
# --------------------------------------------------------------------------

@dataclass
class Position:
    ticker: str
    count: int                 # signed: >0 YES contracts, <0 NO contracts
    exposure: int = 0          # micro-dollars at cost
    realized_pnl: int = 0
    fees_paid: int = 0

    @classmethod
    def from_payload(cls, p: dict) -> "Position":
        signed = p.get("position_fp")
        if signed is None:
            signed = p.get("position")
        count = fp_to_count(signed)
        return cls(
            ticker=p.get("ticker", ""),
            count=count,
            exposure=field_micro(p, "market_exposure") or 0,
            realized_pnl=field_micro(p, "realized_pnl") or 0,
            fees_paid=field_micro(p, "fees_paid") or 0,
        )

    @property
    def side(self) -> str:
        return "yes" if self.count >= 0 else "no"


@dataclass
class Order:
    order_id: str
    ticker: str
    side: str                  # normalized book side: "bid" (buy YES) / "ask" (sell YES)
    yes_price: int             # micro-dollars, in YES terms
    remaining: int
    initial: int = 0
    status: str = ""
    client_order_id: str = ""
    created_ts: Optional[int] = None

    @classmethod
    def from_payload(cls, o: dict, notional: int = MICRO_PER_DOLLAR) -> "Order":
        side = (o.get("side") or "").lower()
        action = (o.get("action") or "").lower()
        yes_price = field_micro(o, "yes_price")
        price = field_micro(o, "price")
        if side in ("bid", "ask"):
            book_side = side
            if yes_price is None:
                yes_price = price
        else:
            # legacy vocabulary: side yes/no + action buy/sell
            buying = action != "sell"
            if side == "no":
                no_price = field_micro(o, "no_price")
                if no_price is None and price is not None:
                    no_price = price
                yes_price = notional - (no_price or 0)
                book_side = "ask" if buying else "bid"
            else:
                if yes_price is None:
                    yes_price = price
                book_side = "bid" if buying else "ask"
        remaining = field_count(o, "remaining_count")
        initial = field_count(o, "initial_count")
        if initial is None:
            initial = field_count(o, "count") or 0
        return cls(
            order_id=o.get("order_id", ""),
            ticker=o.get("ticker", ""),
            side=book_side,
            yes_price=yes_price or 0,
            remaining=remaining if remaining is not None else (initial or 0),
            initial=initial or 0,
            status=(o.get("status") or "").lower(),
            client_order_id=o.get("client_order_id", ""),
            created_ts=parse_time(o.get("created_time")),
        )


@dataclass
class Fill:
    fill_id: str
    order_id: str
    ticker: str
    book_side: str             # "bid" / "ask" in YES terms
    count: int
    yes_price: int
    fee: int = 0
    is_taker: bool = False
    ts: Optional[int] = None

    @classmethod
    def from_payload(cls, f: dict, notional: int = MICRO_PER_DOLLAR) -> "Fill":
        book_side = (f.get("book_side") or "").lower()
        if book_side not in ("bid", "ask"):
            outcome = (f.get("outcome_side") or f.get("side") or "").lower()
            action = (f.get("action") or "buy").lower()
            long_yes = (outcome == "yes") == (action != "sell")
            book_side = "bid" if long_yes else "ask"
        yes_price = field_micro(f, "yes_price")
        if yes_price is None:
            no_price = field_micro(f, "no_price")
            yes_price = notional - no_price if no_price is not None else 0
        ts = f.get("ts")
        return cls(
            fill_id=f.get("fill_id") or f.get("trade_id") or "",
            order_id=f.get("order_id", ""),
            ticker=f.get("ticker") or f.get("market_ticker") or "",
            book_side=book_side,
            count=field_count(f, "count") or 0,
            yes_price=yes_price,
            fee=usd_to_micro(f.get("fee_cost")) or 0,
            is_taker=bool(f.get("is_taker")),
            ts=int(ts) if ts is not None else parse_time(f.get("created_time")),
        )
