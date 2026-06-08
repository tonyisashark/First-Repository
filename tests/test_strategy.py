"""Unit tests for the pure strategy logic (no network required)."""

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_temp_bot.strategy import (
    MarketView,
    parse_time,
    pick_best_candidate,
    position_size,
    seconds_to_close,
    select_buy_candidates,
)

NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def mk(ticker, *, event="EVT", volume=0.0, yes_ask=None, yes_bid=None, close_in_s=None):
    close_time = NOW + timedelta(seconds=close_in_s) if close_in_s is not None else None
    return MarketView(
        ticker=ticker,
        event_ticker=event,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        last_price=None,
        volume=volume,
        close_time=close_time,
    )


# --- position_size ---------------------------------------------------------
def test_position_size_basic():
    # $300 balance, deploy 1/3 = $100 = 10000c, at 90c -> floor(10000/90) = 111
    assert position_size(300_00, 1 / 3, 90) == 111


def test_position_size_floors():
    assert position_size(100, 1 / 3, 90) == 0  # 33c budget, can't afford one @ 90c


def test_position_size_zero_price_guarded():
    assert position_size(100_00, 1 / 3, 0) == 0


# --- select_buy_candidates -------------------------------------------------
def test_selects_when_volume_and_price_match():
    markets = [
        mk("MAX", volume=300, yes_ask=90),   # max volume, also priced 90 -> candidate
        mk("TWO_THIRDS", volume=200, yes_ask=90),  # exactly 2/3 of 300 -> candidate
        mk("BELOW", volume=199, yes_ask=90),  # just under 2/3 -> excluded
        mk("WRONGPRICE", volume=300, yes_ask=89),  # enough volume but wrong price
    ]
    got = {m.ticker for m in select_buy_candidates(
        markets, target_yes_price_cents=90, volume_threshold_ratio=2 / 3, now=NOW)}
    assert got == {"MAX", "TWO_THIRDS"}


def test_threshold_is_inclusive_at_exactly_two_thirds():
    markets = [mk("MAX", volume=300, yes_ask=90), mk("EQ", volume=200.0, yes_ask=90)]
    got = {m.ticker for m in select_buy_candidates(
        markets, target_yes_price_cents=90, volume_threshold_ratio=2 / 3, now=NOW)}
    assert "EQ" in got


def test_price_must_be_exact():
    markets = [mk("A", volume=100, yes_ask=91), mk("B", volume=100, yes_ask=90)]
    got = {m.ticker for m in select_buy_candidates(
        markets, target_yes_price_cents=90, volume_threshold_ratio=2 / 3, now=NOW)}
    assert got == {"B"}


def test_event_scope_isolates_max_per_event():
    # NY has a huge-volume market; CHI's smaller volumes should still qualify
    # against CHI's own max, not NY's.
    markets = [
        mk("NY_BIG", event="NY", volume=900, yes_ask=10),
        mk("NY_SMALL", event="NY", volume=100, yes_ask=90),  # < 2/3 of 900 -> excluded
        mk("CHI_MAX", event="CHI", volume=120, yes_ask=90),  # CHI max, priced 90
        mk("CHI_OK", event="CHI", volume=80, yes_ask=90),    # 80 >= 2/3*120 -> candidate
    ]
    got = {m.ticker for m in select_buy_candidates(
        markets, target_yes_price_cents=90, volume_threshold_ratio=2 / 3,
        scope="event", now=NOW)}
    assert got == {"CHI_MAX", "CHI_OK"}


def test_default_scope_is_global():
    # With no scope argument the single global max governs: CHI_MAX is excluded
    # because NY_BIG's volume dominates across all monitored markets.
    markets = [
        mk("NY_BIG", event="NY", volume=900, yes_ask=10),
        mk("CHI_MAX", event="CHI", volume=120, yes_ask=90),
    ]
    got = {m.ticker for m in select_buy_candidates(
        markets, target_yes_price_cents=90, volume_threshold_ratio=2 / 3, now=NOW)}
    assert got == set()


def test_global_scope_uses_single_max():
    markets = [
        mk("NY_BIG", event="NY", volume=900, yes_ask=10),
        mk("CHI_MAX", event="CHI", volume=120, yes_ask=90),  # < 2/3*900 -> excluded globally
    ]
    got = {m.ticker for m in select_buy_candidates(
        markets, target_yes_price_cents=90, volume_threshold_ratio=2 / 3,
        scope="global", now=NOW)}
    assert got == set()


def test_skips_markets_closing_too_soon():
    markets = [
        mk("SOON", volume=300, yes_ask=90, close_in_s=60),   # closes in 60s
        mk("LATER", volume=300, yes_ask=90, close_in_s=9999),
    ]
    got = {m.ticker for m in select_buy_candidates(
        markets, target_yes_price_cents=90, volume_threshold_ratio=2 / 3,
        min_seconds_to_close=300, now=NOW)}
    assert got == {"LATER"}


# --- pick_best_candidate ---------------------------------------------------
def test_pick_best_is_highest_volume():
    markets = [mk("A", volume=100, yes_ask=90), mk("B", volume=250, yes_ask=90)]
    assert pick_best_candidate(markets).ticker == "B"


def test_pick_best_empty_is_none():
    assert pick_best_candidate([]) is None


# --- time helpers ----------------------------------------------------------
def test_parse_time_z_suffix():
    dt = parse_time("2026-06-08T23:59:00Z")
    assert dt == datetime(2026, 6, 8, 23, 59, 0, tzinfo=timezone.utc)


def test_seconds_to_close():
    m = mk("X", close_in_s=120)
    assert seconds_to_close(m, now=NOW) == pytest.approx(120.0)


def test_seconds_to_close_unknown():
    assert seconds_to_close(mk("X"), now=NOW) is None
