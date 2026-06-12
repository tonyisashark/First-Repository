"""Unit tests for the pure strategy logic (no network required)."""

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_temp_bot.strategy import (
    MarketView,
    estimate_chance_cents,
    event_mid_sums,
    has_liquidity,
    idle_watch_summary,
    microprice_cents,
    mid_price_cents,
    parse_time,
    pick_best_candidate,
    position_size,
    seconds_to_close,
    select_buy_candidates,
    spread_cents,
    volume_qualifying_markets,
)

NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def mk(ticker, *, event="EVT", volume=0.0, chance=None, yes_ask=None, yes_bid=None,
       close_in_s=None):
    # ``chance`` builds a tight book around the value (mid == chance, spread 2),
    # the common case; pass yes_bid/yes_ask explicitly to shape the book.
    if chance is not None and yes_ask is None and yes_bid is None:
        yes_bid, yes_ask = chance - 1, chance + 1
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


def band(markets, lo=90, hi=90, **kwargs):
    kwargs.setdefault("volume_threshold_ratio", 2 / 3)
    return {m.ticker for m in select_buy_candidates(
        markets, min_chance_cents=lo, max_chance_cents=hi, now=NOW, **kwargs)}


# --- chance estimation -------------------------------------------------------
def test_mid_price_treats_missing_bid_as_zero():
    assert mid_price_cents(mk("X", yes_ask=10)) == 5.0
    assert mid_price_cents(mk("X", yes_bid=90, yes_ask=94)) == 92.0
    assert mid_price_cents(mk("X")) is None


def test_spread_counts_one_sided_books_as_wide():
    assert spread_cents(mk("X", yes_bid=90, yes_ask=94)) == 4
    assert spread_cents(mk("X", yes_ask=94)) == 94  # no bid -> effectively untradeable
    assert spread_cents(mk("X")) is None


def test_microprice_weights_toward_the_pressured_side():
    # Heavy bid depth pulls the estimate toward the ask, and vice versa.
    assert microprice_cents(90, 94, 1000, 10) == pytest.approx(93.96, abs=0.01)
    assert microprice_cents(90, 94, 10, 1000) == pytest.approx(90.04, abs=0.01)
    # Unknown/empty depth on either side -> plain midpoint.
    assert microprice_cents(90, 94, 0, 1000) == 92.0
    assert microprice_cents(None, 94, 5, 5) is None


def test_estimate_renormalizes_event_overround():
    # The event's buckets sum to 108% -> the 94c bucket's genuine chance ~87%.
    markets = [
        mk("BIG", chance=94),
        mk("MID", chance=10),
        mk("LOW", chance=4),
    ]
    sums = event_mid_sums(markets)
    assert sums["EVT"] == pytest.approx(108.0)
    assert estimate_chance_cents(markets[0], sums) == pytest.approx(94 * 100 / 108)


def test_estimate_skips_renormalization_when_sum_is_implausible():
    # A lone 50c bucket sums to 50 -- far from a complete event, so scaling it
    # to 100% would be nonsense; the raw estimate is kept.
    markets = [mk("ONLY", chance=50)]
    sums = event_mid_sums(markets)
    assert estimate_chance_cents(markets[0], sums) == 50.0


def test_estimate_prefers_supplied_microprice():
    markets = [mk("A", chance=90), mk("B", chance=10)]
    sums = event_mid_sums(markets)  # 100 -> renormalization is a no-op
    assert estimate_chance_cents(markets[0], sums, micro=92.5) == pytest.approx(92.5)


# --- select_buy_candidates ---------------------------------------------------
def test_selects_when_volume_and_chance_match():
    markets = [
        mk("MAX", volume=300, chance=90),    # max volume, in band -> candidate
        mk("TWO_THIRDS", volume=200, chance=90),  # exactly 2/3 of 300 -> candidate
        mk("BELOW", volume=199, chance=90),  # just under 2/3 -> excluded
        mk("WRONG", volume=300, chance=85),  # enough volume but out of band
    ]
    assert band(markets) == {"MAX", "TWO_THIRDS"}


def test_band_is_inclusive_and_supports_a_range():
    markets = [
        mk("LO", volume=100, chance=90),
        mk("IN", volume=100, chance=93),
        mk("HI", volume=100, chance=95),
        mk("OUT", volume=100, chance=96),
    ]
    assert band(markets, lo=90, hi=95) == {"LO", "IN", "HI"}


def test_spread_gate_rejects_wide_books():
    markets = [
        mk("TIGHT", volume=100, yes_bid=89, yes_ask=91),   # mid 90, spread 2
        mk("WIDE", volume=100, yes_bid=80, yes_ask=100),   # mid 90 but spread 20
    ]
    assert band(markets, max_spread_cents=5) == {"TIGHT"}


def test_micro_estimates_override_the_midpoint():
    markets = [mk("A", volume=100, chance=89), mk("B", volume=100, chance=11)]
    # Event sums to 100 (renormalization is a no-op). The mid says 89 (out of
    # band) but the smoothed microprice says 90 (in band).
    assert band(markets) == set()
    assert band(markets, micro_estimates={"A": 90.0}) == {"A"}


def test_event_scope_isolates_max_per_event():
    # NY has a huge-volume market; CHI's smaller volumes should still qualify
    # against CHI's own max, not NY's.
    markets = [
        mk("NY_BIG", event="NY", volume=900, chance=10),
        mk("NY_SMALL", event="NY", volume=100, chance=90),  # < 2/3 of 900 -> excluded
        mk("CHI_MAX", event="CHI", volume=120, chance=90),  # CHI max, in band
        mk("CHI_OK", event="CHI", volume=80, chance=90),    # 80 >= 2/3*120 -> candidate
    ]
    assert band(markets, scope="event") == {"CHI_MAX", "CHI_OK"}


def test_default_scope_is_global():
    # With no scope argument the single global max governs: CHI_MAX is excluded
    # because NY_BIG's volume dominates across all monitored markets.
    markets = [
        mk("NY_BIG", event="NY", volume=900, chance=10),
        mk("CHI_MAX", event="CHI", volume=120, chance=90),
    ]
    assert band(markets) == set()


def test_skips_markets_closing_too_soon():
    markets = [
        mk("SOON", volume=300, chance=90, close_in_s=60),   # closes in 60s
        mk("LATER", volume=300, chance=90, close_in_s=9999),
    ]
    assert band(markets, min_seconds_to_close=300) == {"LATER"}


# --- pick_best_candidate -----------------------------------------------------
def test_pick_best_is_highest_volume():
    markets = [mk("A", volume=100, chance=90), mk("B", volume=250, chance=90)]
    assert pick_best_candidate(markets).ticker == "B"


def test_pick_best_empty_is_none():
    assert pick_best_candidate([]) is None


# --- position_size -----------------------------------------------------------
def test_position_size_basic():
    # $300 balance, deploy 1/3 = $100 = 10000c, at 90c -> floor(10000/90) = 111
    assert position_size(300_00, 1 / 3, 90) == 111


def test_position_size_floors():
    assert position_size(100, 1 / 3, 90) == 0  # 33c budget, can't afford one @ 90c


def test_position_size_zero_price_guarded():
    assert position_size(100_00, 1 / 3, 0) == 0


# --- time helpers ------------------------------------------------------------
def test_parse_time_z_suffix():
    dt = parse_time("2026-06-08T23:59:00Z")
    assert dt == datetime(2026, 6, 8, 23, 59, 0, tzinfo=timezone.utc)


def test_seconds_to_close():
    m = mk("X", close_in_s=120)
    assert seconds_to_close(m, now=NOW) == pytest.approx(120.0)


def test_seconds_to_close_unknown():
    assert seconds_to_close(mk("X"), now=NOW) is None


# --- has_liquidity / volume_qualifying_markets --------------------------------
def test_has_liquidity_excludes_rails():
    for ask, expected in [(0, False), (1, False), (2, True), (50, True),
                          (98, True), (99, False), (100, False), (None, False)]:
        assert has_liquidity(mk("X", yes_ask=ask)) is expected


def test_volume_qualifying_global():
    markets = [mk("MAX", volume=300, chance=10), mk("OK", volume=200), mk("LOW", volume=199)]
    got = {m.ticker for m in volume_qualifying_markets(
        markets, volume_threshold_ratio=2 / 3, scope="global")}
    assert got == {"MAX", "OK"}  # price is irrelevant to the volume filter


# --- idle_watch_summary --------------------------------------------------------
def summary(markets, lo=90, hi=90, **kwargs):
    kwargs.setdefault("volume_threshold_ratio", 2 / 3)
    return idle_watch_summary(
        markets, min_chance_cents=lo, max_chance_cents=hi, now=NOW, **kwargs)


def test_idle_summary_reports_counts_and_closest():
    markets = [
        mk("MAX", volume=300, chance=92),   # qualifies on volume, closest to band
        mk("FAR", volume=300, chance=40),   # qualifies on volume, far from band
        mk("LOW", volume=10, chance=90),    # in band but fails volume -> not a candidate
    ]
    text = summary(markets)
    assert "watching 3 markets" in text
    assert "0 in 90-90% chance band" in text
    assert "closest qualifying MAX chance 92.0%" in text


def test_idle_summary_skips_no_liquidity_closest():
    # The rail-priced market is numerically closest to the band but has no real
    # liquidity, so the liquid 70c market should be reported instead.
    markets = [
        mk("RAIL", volume=300, yes_bid=98, yes_ask=99),    # ask on the 99c rail
        mk("LIQUID", volume=300, chance=70),               # real two-sided book
    ]
    text = summary(markets)
    assert "closest qualifying LIQUID chance 70.0%" in text
    assert "RAIL" not in text


def test_idle_summary_empty():
    assert "watching 0 markets" in summary([])


def test_idle_summary_hides_closest_when_candidate_exists():
    markets = [mk("HIT", volume=300, chance=90), mk("REST", volume=300, chance=10)]
    text = summary(markets)
    assert "1 in 90-90% chance band" in text
    assert "closest" not in text
