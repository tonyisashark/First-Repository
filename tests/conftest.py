"""Shared fixtures and payload builders for offline tests.

Builders emit *modern* API shapes (``*_dollars`` strings, ``orderbook_fp``)
so the defensive parsers are exercised exactly as production data would.
"""

from __future__ import annotations

import time
from typing import List, Optional

import pytest

from kalshi_bot.config import Config
from kalshi_bot.models import Event, Market, OrderBook
from kalshi_bot.state import StateStore

NOW = int(time.time())


def usd(micro: int) -> str:
    return f"{micro / 1_000_000:.4f}"


def market_payload(
    ticker: str,
    event_ticker: str = "EV-A",
    *,
    yes_bid: Optional[int] = None,
    yes_ask: Optional[int] = None,
    status: str = "active",
    close_in: int = 86_400,
    volume_24h: int = 5_000,
    strike_type: str = "",
    floor_strike: Optional[float] = None,
    cap_strike: Optional[float] = None,
    result: str = "",
    last: Optional[int] = None,
) -> dict:
    payload = {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "status": status,
        "result": result,
        "yes_sub_title": f"title {ticker}",
        "close_time": NOW + close_in,
        "volume_24h_fp": f"{volume_24h}.00",
        "volume_fp": f"{volume_24h * 3}.00",
        "open_interest_fp": "1000.00",
        "notional_value_dollars": "1.0000",
    }
    if yes_bid is not None:
        payload["yes_bid_dollars"] = usd(yes_bid)
        payload["no_ask_dollars"] = usd(1_000_000 - yes_bid)
    if yes_ask is not None:
        payload["yes_ask_dollars"] = usd(yes_ask)
        payload["no_bid_dollars"] = usd(1_000_000 - yes_ask)
    if last is not None:
        payload["last_price_dollars"] = usd(last)
    if strike_type:
        payload["strike_type"] = strike_type
    if floor_strike is not None:
        payload["floor_strike"] = floor_strike
    if cap_strike is not None:
        payload["cap_strike"] = cap_strike
    return payload


def event_payload(event_ticker: str, markets: List[dict],
                  mutually_exclusive: bool = True,
                  series_ticker: str = "") -> dict:
    return {
        "event_ticker": event_ticker,
        "series_ticker": series_ticker or event_ticker.split("-")[0],
        "title": f"event {event_ticker}",
        "mutually_exclusive": mutually_exclusive,
        "markets": markets,
    }


def book_payload(*, yes_bids: List[tuple] = (), no_bids: List[tuple] = ()) -> dict:
    """Levels given as (price_micro, count)."""
    return {
        "orderbook_fp": {
            "yes_dollars": [[usd(p), f"{c}.00"] for p, c in yes_bids],
            "no_dollars": [[usd(p), f"{c}.00"] for p, c in no_bids],
        }
    }


def simple_book(yes_bid: int, yes_ask: int, depth: int = 100,
                notional: int = 1_000_000) -> OrderBook:
    """Two-sided book: best YES bid and (derived) YES ask with given depth."""
    payload = book_payload(
        yes_bids=[(yes_bid, depth)],
        no_bids=[(notional - yes_ask, depth)],
    )
    return OrderBook.from_payload(payload, notional)


def mk(ticker: str = "T-1", **kwargs) -> Market:
    return Market.from_payload(market_payload(ticker, **kwargs))


def ev(event_ticker: str, market_payloads: List[dict], **kwargs) -> Event:
    return Event.from_payload(event_payload(event_ticker, market_payloads, **kwargs))


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(state_db_path=str(tmp_path / "state.sqlite3"))


@pytest.fixture
def state(cfg) -> StateStore:
    store = StateStore(cfg.state_db_path)
    yield store
    store.close()
