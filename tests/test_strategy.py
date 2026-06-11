"""Unit tests for the pure strategy logic (no network required)."""

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_temp_bot.strategy import (
    MarketView,
    has_liquidity,
    idle_watch_summary,
    parse_time,
    pick_best_candidate,
    position_size,
    seconds_to_close,
    select_buy_candidates,
    volume_qualifying_markets,
)


def test_has_liquidity_excludes_rails():
    for ask, expected in [(0, False), (1, False), (2, True), (50, True),
                          (98, True), (99, False), (100, False), (None, False)]:
        assert has_liquidity(mk("X", yes_ask=ask)) is expected


def test_idle_summary_skips_no_liquidity_closest():
    # The rail-priced 99c market is numerically closest to 90 but has no real
    # liquidity, so the non-rail 70c market should be reported instead.
    markets = [
        mk("RAIL", volume=300, yes_ask=99),   # closest by |ask-90| but a settled rail
        mk("LIQUID", volume=300, yes_ask=70),  # real liquidity, further from 90
    ]
    summary = idle_watch_summary(
        markets, target_yes_price_cents=90, volume_threshold_ratio=2 / 3, now=NOW)
    assert "closest qualifying LIQUID ask 70c" in summary
    assert "RAIL" not in summary

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


# --- volume_qualifying_markets --------------------------------------------
def test_volume_qualifying_global():
    markets = [mk("MAX", volume=300, yes_ask=10), mk("OK", volume=200), mk("LOW", volume=199)]
    got = {m.ticker for m in volume_qualifying_markets(
        markets, volume_threshold_ratio=2 / 3, scope="global")}
    assert got == {"MAX", "OK"}  # price is irrelevant to the volume filter


# --- idle_watch_summary ----------------------------------------------------
def test_idle_summary_reports_counts_and_closest():
    markets = [
        mk("MAX", volume=300, yes_ask=92),   # qualifies on volume, ask 92 (closest to 90)
        mk("FAR", volume=300, yes_ask=40),   # qualifies on volume, ask far from 90
        mk("LOW", volume=10, yes_ask=90),    # at 90 but fails volume -> not a candidate
    ]
    summary = idle_watch_summary(
        markets, target_yes_price_cents=90, volume_threshold_ratio=2 / 3, now=NOW)
    assert "watching 3 markets" in summary
    assert "0 at 90c" in summary
    assert "closest qualifying MAX ask 92c" in summary


def test_idle_summary_empty():
    assert "watching 0 markets" in idle_watch_summary(
        [], target_yes_price_cents=90, volume_threshold_ratio=2 / 3, now=NOW)


def test_idle_summary_hides_closest_when_candidate_exists():
    markets = [mk("HIT", volume=300, yes_ask=90)]
    summary = idle_watch_summary(
        markets, target_yes_price_cents=90, volume_threshold_ratio=2 / 3, now=NOW)
    assert "1 at 90c" in summary
    assert "closest" not in summary
