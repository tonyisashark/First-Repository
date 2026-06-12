"""Unit tests for the pure strategy logic (no network required)."""

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_temp_bot.strategy import (
    MIN_EDGE_CENTS,
    MarketView,
    best_candidate,
    evaluate_side,
    growth_rate,
    idle_watch_summary,
    informative,
    kelly_fraction,
    microprice_cents,
    mid_price_cents,
    parse_time,
    position_size,
    renormalized_estimates,
    seconds_to_close,
    select_trade_candidates,
    side_quotes,
    spread_cents,
    taker_fee_cents,
)

NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def mk(ticker, *, event="EVT", volume=100.0, yes_bid=None, yes_ask=None, close_in_s=None):
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


# --- book arithmetic ----------------------------------------------------------
def test_mid_price_treats_missing_bid_as_zero():
    assert mid_price_cents(mk("X", yes_ask=10)) == 5.0
    assert mid_price_cents(mk("X", yes_bid=90, yes_ask=94)) == 92.0
    assert mid_price_cents(mk("X")) is None


def test_spread_counts_one_sided_books_as_wide():
    assert spread_cents(mk("X", yes_bid=90, yes_ask=94)) == 4
    assert spread_cents(mk("X", yes_ask=94)) == 94  # no bid -> effectively untradeable
    assert spread_cents(mk("X")) is None
    assert informative(mk("X", yes_bid=80, yes_ask=99)) is True   # spread 19 <= 20
    assert informative(mk("X", yes_bid=70, yes_ask=99)) is False  # spread 29 > 20


def test_microprice_weights_toward_the_pressured_side():
    # Heavy bid depth pulls the estimate toward the ask, and vice versa.
    assert microprice_cents(90, 94, 1000, 10) == pytest.approx(93.96, abs=0.01)
    assert microprice_cents(90, 94, 10, 1000) == pytest.approx(90.04, abs=0.01)
    # Unknown/empty depth on either side -> plain midpoint.
    assert microprice_cents(90, 94, 0, 1000) == 92.0
    assert microprice_cents(None, 94, 5, 5) is None


def test_side_quotes_mirror_for_no():
    m = mk("X", yes_bid=92, yes_ask=95)
    assert side_quotes(m, "yes") == {"bid": 92, "ask": 95}
    assert side_quotes(m, "no") == {"bid": 5, "ask": 8}
    empty = mk("X")
    assert side_quotes(empty, "no") == {"bid": None, "ask": None}


# --- renormalization ----------------------------------------------------------
def test_renormalization_strips_event_overround():
    # Buckets sum to 108% -> a 94c estimate's genuine chance is ~87%.
    markets = [
        mk("BIG", yes_bid=93, yes_ask=95),
        mk("MID", yes_bid=9, yes_ask=11),
        mk("LOW", yes_bid=3, yes_ask=5),
    ]
    est = renormalized_estimates(markets, {"BIG": 94.0, "MID": 10.0, "LOW": 4.0})
    assert est["BIG"] == pytest.approx(94 * 100 / 108)
    assert est["MID"] == pytest.approx(10 * 100 / 108)


def test_renormalization_uses_mids_for_book_less_buckets():
    # Only BIG has a book estimate; the other bucket's quoted mid (6) still
    # counts toward the event sum (94 + 6 = 100 -> no-op scaling).
    markets = [mk("BIG", yes_bid=93, yes_ask=95), mk("REST", yes_bid=5, yes_ask=7)]
    est = renormalized_estimates(markets, {"BIG": 94.0})
    assert est["BIG"] == pytest.approx(94.0)
    assert "REST" not in est  # no book estimate -> not tradeable


def test_renormalization_skipped_when_sum_is_implausible():
    # A lone 50c bucket sums to 50 -- far from a complete event, so scaling it
    # to 100% would be nonsense; the raw estimate is kept.
    markets = [mk("ONLY", yes_bid=49, yes_ask=51)]
    est = renormalized_estimates(markets, {"ONLY": 50.0})
    assert est["ONLY"] == 50.0


def test_renormalization_pins_rail_priced_buckets():
    # A settled winner (99/100) in an event whose stale tails inflate the sum
    # to ~105.5: proportional renormalization would tax the winner to ~94.3
    # and manufacture a phantom +4.6c NO edge at the 1c NO ask. Rail-priced
    # values are settlement certainty and must not be rescaled.
    markets = [
        mk("WIN", yes_bid=99, yes_ask=100),
        mk("T1", yes_bid=1, yes_ask=3),
        mk("T2", yes_bid=1, yes_ask=3),
        mk("T3", yes_bid=1, yes_ask=3),
    ]
    est = renormalized_estimates(markets, {"WIN": 99.5})
    assert est["WIN"] == 99.0  # pinned (and clamped), not scaled down
    no = evaluate_side(markets[0], "no", est["WIN"], 1 / 3)
    assert no.edge_cents < 0   # no phantom NO edge on the settled winner


def test_idle_summary_skips_rail_priced_markets():
    # A settled overnight event (winner at 99 with stale tails) sits next to a
    # live one. Without the rail pin+skip the winner would show a phantom
    # "+4.6c NO edge" as the closest; the genuine live market must win instead.
    markets = [
        mk("WIN", event="DONE", yes_bid=99, yes_ask=100),
        mk("T1", event="DONE", yes_bid=1, yes_ask=3),
        mk("T2", event="DONE", yes_bid=1, yes_ask=3),
        mk("T3", event="DONE", yes_bid=1, yes_ask=3),
        mk("MID", event="LIVE", yes_bid=92, yes_ask=94),
        mk("Z", event="LIVE", yes_bid=5, yes_ask=7),
    ]
    text = idle_watch_summary(markets, estimates={}, portfolio_fraction=1 / 3, now=NOW)
    assert "WIN" not in text
    assert "closest: MID" in text


def test_renormalization_ignores_uninformative_mids():
    # The wide-spread bucket would poison the event sum; it must not count.
    markets = [
        mk("BIG", yes_bid=93, yes_ask=95),
        mk("WIDE", yes_bid=1, yes_ask=99),   # spread 98: no information
        mk("REST", yes_bid=5, yes_ask=7),
    ]
    est = renormalized_estimates(markets, {"BIG": 94.0})
    assert est["BIG"] == pytest.approx(94.0)  # sum stays 94 + 6 = 100


# --- fee / kelly / growth ------------------------------------------------------
def test_taker_fee_matches_kalshi_formula():
    assert taker_fee_cents(94) == pytest.approx(0.07 * 94 * 6 / 100)   # ~0.39c
    assert taker_fee_cents(50) == pytest.approx(1.75)                  # worst case
    assert taker_fee_cents(99) == pytest.approx(0.0693)


def test_kelly_fraction():
    # p=96, cost=94 -> (96-94)/(100-94) = 1/3.
    assert kelly_fraction(96, 94) == pytest.approx(1 / 3)
    assert kelly_fraction(90, 94) == 0.0   # no edge -> no bet
    assert kelly_fraction(50, 100) == 0.0


def test_growth_rate_signs():
    # An underpriced contract has positive growth; an overpriced one negative.
    assert growth_rate(97, 94, 1 / 3) > 0
    assert growth_rate(91, 94, 1 / 3) < 0
    # Compounding penalty: even at a *fair* price the growth is negative
    # (variance drag), which is exactly why the bot demands a positive edge.
    assert growth_rate(94, 94, 1 / 3) < 0


# --- evaluate_side / selection --------------------------------------------------
def test_evaluate_side_yes_and_no():
    m = mk("X", yes_bid=92, yes_ask=94)
    yes = evaluate_side(m, "yes", 96.0, 1 / 3)
    assert yes.price_cents == 94
    assert yes.cost_cents == pytest.approx(94 + taker_fee_cents(94))
    assert yes.edge_cents == pytest.approx(96 - 94 - taker_fee_cents(94))

    # NO is bought at 100 - bid = 8c; its chance is 100 - estimate.
    no = evaluate_side(m, "no", 96.0, 1 / 3)
    assert no.price_cents == 8
    assert no.chance_cents == pytest.approx(4.0)
    assert no.edge_cents < 0  # fairly priced -> no NO edge

    assert evaluate_side(mk("X"), "yes", 96.0, 1 / 3) is None  # no ask -> no trade


def test_fair_market_offers_no_candidate():
    # Book 92/94, estimate equal to the mid: neither side clears the edge bar.
    markets = [mk("A", yes_bid=92, yes_ask=94), mk("Z", yes_bid=5, yes_ask=7)]
    est = renormalized_estimates(markets, {"A": 93.0})
    cands = select_trade_candidates(markets, estimates=est, portfolio_fraction=1 / 3, now=NOW)
    assert cands == []


def test_underpriced_yes_is_selected():
    # Ask 92 while the renormalized estimate is ~96.8 -> a real YES edge.
    markets = [mk("A", yes_bid=91, yes_ask=92), mk("Z", yes_bid=2, yes_ask=4)]
    est = renormalized_estimates(markets, {"A": 91.5})   # sum 91.5+3 -> renorm up
    assert est["A"] == pytest.approx(91.5 * 100 / 94.5)
    cands = select_trade_candidates(markets, estimates=est, portfolio_fraction=1 / 3, now=NOW)
    assert [(c.market.ticker, c.side) for c in cands] == [("A", "yes")]
    assert cands[0].edge_cents >= MIN_EDGE_CENTS
    assert cands[0].growth > 0


def test_overpriced_yes_selects_no_side():
    # The bid (55) is far above the renormalized estimate (~47.8): buying NO
    # at 45c with a ~52.2% chance is the edge.
    markets = [mk("A", yes_bid=55, yes_ask=57), mk("Z", yes_bid=59, yes_ask=61)]
    est = renormalized_estimates(markets, {"A": 55.0})   # sum 55+60=115 -> scale down
    assert est["A"] == pytest.approx(55 * 100 / 115)
    cands = select_trade_candidates(markets, estimates=est, portfolio_fraction=1 / 3, now=NOW)
    assert [(c.market.ticker, c.side) for c in cands] == [("A", "no")]
    no = cands[0]
    assert no.price_cents == 45
    assert no.chance_cents == pytest.approx(100 - 55 * 100 / 115)
    # Thin-edge protection: the Kelly cap deploys less than the 1/3 ceiling.
    assert no.fraction < 1 / 3
    assert no.fraction == pytest.approx(
        (no.chance_cents - no.cost_cents) / (100 - no.cost_cents))


def test_markets_without_estimates_are_ignored():
    markets = [mk("A", yes_bid=80, yes_ask=82)]
    cands = select_trade_candidates(markets, estimates={}, portfolio_fraction=1 / 3, now=NOW)
    assert cands == []


def test_skips_markets_closing_too_soon():
    # Identical edges in two separate events; only the close time differs.
    markets = [
        mk("SOON", event="E1", yes_bid=91, yes_ask=92, close_in_s=60),
        mk("Z1", event="E1", yes_bid=2, yes_ask=4),
        mk("LATER", event="E2", yes_bid=91, yes_ask=92, close_in_s=9999),
        mk("Z2", event="E2", yes_bid=2, yes_ask=4),
    ]
    est = renormalized_estimates(markets, {"SOON": 91.5, "LATER": 91.5})
    cands = select_trade_candidates(
        markets, estimates=est, portfolio_fraction=1 / 3, min_seconds_to_close=300, now=NOW)
    assert {c.market.ticker for c in cands} == {"LATER"}


def test_best_candidate_maximizes_growth():
    markets = [
        mk("SMALL", yes_bid=93, yes_ask=94),
        mk("BIG", yes_bid=89, yes_ask=90),
        mk("Z", yes_bid=2, yes_ask=3),
    ]
    # Pre-renormalized estimates passed directly: SMALL has a 2.6c gross edge,
    # BIG a 6c one; growth must prefer BIG.
    est = {"SMALL": 96.6, "BIG": 96.0}
    cands = select_trade_candidates(markets, estimates=est, portfolio_fraction=1 / 3, now=NOW)
    assert {c.market.ticker for c in cands} == {"SMALL", "BIG"}
    best = best_candidate(cands)
    assert best.market.ticker == "BIG"
    assert best_candidate([]) is None


# --- position_size -----------------------------------------------------------
def test_position_size_fractional_deploys_budget_exactly():
    # $300 balance, deploy 1/3 = $100, at 90c -> 111.11 contracts ($99.999).
    assert position_size(300_00, 1 / 3, 90) == pytest.approx(111.11)
    # Small balances can trade too: 33.33c budget at 90c -> 0.37 contracts.
    assert position_size(100, 1 / 3, 90) == pytest.approx(0.37)


def test_position_size_whole_contracts_floor():
    assert position_size(300_00, 1 / 3, 90, fractional=False) == 111
    assert position_size(100, 1 / 3, 90, fractional=False) == 0


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


# --- idle_watch_summary --------------------------------------------------------
def test_idle_summary_reports_best_candidate():
    markets = [mk("A", yes_bid=91, yes_ask=92), mk("Z", yes_bid=2, yes_ask=4)]
    est = renormalized_estimates(markets, {"A": 91.5})
    text = idle_watch_summary(markets, estimates=est, portfolio_fraction=1 / 3, now=NOW)
    assert "watching 2 markets" in text
    assert "best: A YES" in text


def test_idle_summary_reports_closest_miss():
    markets = [mk("A", yes_bid=92, yes_ask=94), mk("Z", yes_bid=5, yes_ask=7)]
    est = renormalized_estimates(markets, {"A": 93.0})  # fair -> no candidate
    text = idle_watch_summary(markets, estimates=est, portfolio_fraction=1 / 3, now=NOW)
    assert "no side above" in text
    assert "closest: A" in text


def test_idle_summary_falls_back_to_approximate_mids():
    # No book estimates yet, but the quoted books are tight: the closest miss
    # is still reported, marked as approximate.
    markets = [mk("A", yes_bid=92, yes_ask=94), mk("Z", yes_bid=5, yes_ask=7)]
    text = idle_watch_summary(markets, estimates={}, portfolio_fraction=1 / 3, now=NOW)
    assert "closest: A" in text
    assert "~" in text


def test_idle_summary_when_books_are_uninformative():
    markets = [mk("A", yes_ask=94)]  # one-sided book: no information at all
    text = idle_watch_summary(markets, estimates={}, portfolio_fraction=1 / 3, now=NOW)
    assert "books too wide or one-sided" in text


def test_idle_summary_empty():
    text = idle_watch_summary([], estimates={}, portfolio_fraction=1 / 3, now=NOW)
    assert "watching 0 markets" in text
