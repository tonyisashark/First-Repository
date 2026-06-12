"""Tests for the trading loop: edge-based entry on either side, Kelly-capped
sizing, book-depth caps, the liquidity and edge-reversal exits, the no-liquidity
slot exemption, and holding through close (paper mode)."""

import time
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_temp_bot.bot import TradingBot, TradeState
from kalshi_temp_bot.config import Config
from kalshi_temp_bot.strategy import MarketView


class FakeClient:
    auth = None  # -> paper-balance sizing, no network
    # no get_orderbook -> no book estimates -> the bot can never trade


class FakeBookClient(FakeClient):
    """FakeClient serving a per-ticker orderbook: {ticker: {"yes": [...], "no": [...]}}."""

    def __init__(self, books=None):
        self.books = books or {}

    def get_orderbook(self, ticker):
        book = self.books.get(ticker, {})
        return {"yes": list(book.get("yes", [])), "no": list(book.get("no", []))}


def make_bot(client=None, **overrides):
    cfg = Config.from_env()
    cfg.dry_run = True
    cfg.paper_balance_cents = 300_00
    cfg.portfolio_fraction = 1 / 3
    for key, value in overrides.items():
        setattr(cfg, key, value)
    bot = TradingBot(client=client or FakeClient(), config=cfg)
    bot.scan_interval = 10_000        # tests control the market cache directly
    bot.heartbeat_interval = 10_000
    bot.book_poll_seconds = 0.0       # poll books on every tick
    bot.ewma_half_life = 0.0          # no smoothing lag in tests
    bot.min_estimate_history = 0.0    # no estimate-maturity wait in tests
    bot.min_estimate_samples = 0
    bot.startup_warmup = 0.0          # no board-warmup wait in tests
    bot.entry_spacing = 0.0           # no pacing between entries in tests
    return bot


def mv(ticker, *, event="E", yes_bid=91, yes_ask=92, volume=1000, close_in_s=5 * 3600):
    return MarketView(
        ticker=ticker, event_ticker=event, yes_bid=yes_bid, yes_ask=yes_ask,
        last_price=None, volume=volume,
        close_time=datetime.now(timezone.utc) + timedelta(seconds=close_in_s),
        status="open",
    )


def filler(event="E", ticker=None):
    # A complementary low bucket so the event's asks sum to 95 (< 100: buying
    # the whole event at ask beats its certain payout): renormalization lifts
    # the big bucket's estimate to ~96.4%, above its 92c ask -> a YES edge of
    # ~3.9c, clearing the scaled bar (~3.25c at a 1c spread, 5h to close).
    return mv(ticker or f"ZF_{event}", event=event, yes_bid=2, yes_ask=3, volume=1)


def edge_book():
    # Matches mv() defaults: bid 91 (500 deep), ask 92 (400 deep) -> microprice
    # ~91.56; with the filler the renormalized estimate is ~96.4%.
    return {"yes": [[91, 500]], "no": [[8, 400]]}


def edged_universe(bot, ticker="A", event="E"):
    market = mv(ticker, event=event)
    bot.client.books[ticker] = edge_book()
    set_markets(bot, [market, filler(event)])
    return market


def set_markets(bot, views):
    bot._market_cache = views
    bot._last_scan = time.time()


def test_immature_estimate_does_not_trade():
    # A first book snapshot seeds the estimate outright; it must not be able
    # to buy a position by itself -- only a repeated reading may trade.
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    bot.min_estimate_samples = 3
    edged_universe(bot)
    bot.tick()                                # 1 sample: edge visible, no trade
    assert bot.trades == []
    bot.tick()                                # 2 samples: still warming up
    assert bot.trades == []
    bot.tick()                                # 3 samples: mature -> trades
    assert [t.ticker for t in bot.trades] == ["A"]


def test_background_coverage_estimates_far_from_edge_markets():
    # Both buckets sit ~10c below the edge bar (well outside the prefilter
    # slack), so neither earns a "hot" fetch. Background coverage must spend
    # the leftover budget on them anyway: estimates need to exist (and be
    # maturing) *before* a market drifts toward an edge, not after.
    client = FakeBookClient({
        "W": {"yes": [[40, 100]], "no": [[44, 100]]},   # bid 40 / ask 56
        "X": {"yes": [[42, 100]], "no": [[42, 100]]},   # bid 42 / ask 58
    })
    bot = make_bot(client=client, max_positions=1)
    set_markets(bot, [
        mv("W", yes_bid=40, yes_ask=56),
        mv("X", yes_bid=42, yes_ask=58),                # same event: no renorm
    ])
    bot.tick()
    assert bot.trades == []                             # no edge anywhere
    assert {"W", "X"} <= set(bot._fresh_micro())        # but both estimated


def test_background_coverage_waits_for_its_cadence():
    # Background polls run on the slow cadence; a just-polled market must not
    # be re-fetched every tick (that budget belongs to near-edge markets).
    client = FakeBookClient({"W": {"yes": [[40, 100]], "no": [[44, 100]]}})
    bot = make_bot(client=client, max_positions=1)
    bot.book_poll_seconds = 10_000                      # isolate the slow path
    bot.background_poll_seconds = 10_000
    set_markets(bot, [
        mv("W", yes_bid=40, yes_ask=56),
        mv("X", yes_bid=42, yes_ask=58),                # same event: no renorm
    ])
    bot._book_polled_at["W"] = time.time()              # polled moments ago
    bot.tick()
    assert "W" not in bot._fresh_micro()


def wide_maker_universe(bot):
    # Book 40/56 with depth stacked on the bid: microprice ~55.2, so the ask
    # (56 + 1.7c fee) shows NO taker edge, but a bid resting at 41 has ~14c of
    # fee-free edge -- well above even the wide-book bar (~10.8c).
    a = mv("A", yes_bid=40, yes_ask=56)
    z = mv("Z", yes_bid=42, yes_ask=58)            # same event: no renorm
    bot.client.books["A"] = {"yes": [[40, 2000]], "no": [[44, 100]]}
    set_markets(bot, [a, z])
    return a


def test_maker_entry_rests_inside_the_spread():
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    a = wide_maker_universe(bot)
    bot.tick()
    trade = bot.trades[0]
    assert (trade.maker, trade.state, trade.buy_price) == (True, TradeState.BUYING, 41)

    # The market trades down through the resting bid -> paper fill, fee-free.
    a.yes_ask = 41
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING
    assert bot.trades[0].cost_cents == 41.0        # no taker fee as a maker
    assert bot._paper_balance == pytest.approx(300_00 - 41.0 * bot.trades[0].count)


def test_correctly_priced_resting_bid_is_left_to_work():
    # Cancelling a bid that is already at the right level only to repost it
    # would lose queue position (live) and spam the log: the expired timer
    # just re-arms while the level stays right.
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    wide_maker_universe(bot)
    bot.maker_buy_timeout = 0.0                    # expire every tick
    bot.tick()
    first = bot.trades[0]
    bot.tick()
    bot.tick()
    assert bot.trades[0] is first                  # same order, never churned
    assert first.buy_price == 41


def test_resting_bid_reprices_when_the_book_moves():
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    a = wide_maker_universe(bot)
    bot.maker_buy_timeout = 0.0
    bot.tick()
    assert bot.trades[0].buy_price == 41
    # The bid moves up underneath us -- in the quote feed AND the book (the
    # right-level check accepts either source vouching for the old level).
    a.yes_bid = 44
    bot.client.books["A"] = {"yes": [[44, 2000]], "no": [[44, 100]]}
    bot.tick()                                     # off-level -> cancel + repost
    assert bot.trades[0].buy_price == 45           # fresh bid one tick inside


def test_maker_bid_fills_when_a_trade_prints_at_its_level():
    # A print at/below the resting level means a seller crossed down to it:
    # paper mode must credit the fill even though the quotes never moved.
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    a = wide_maker_universe(bot)
    bot.tick()
    assert bot.trades[0].buy_price == 41
    a.last_price, a.volume = 41, a.volume + 10
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING
    assert bot.trades[0].cost_cents == 41.0        # maker: fee-free


def test_resting_bid_is_pulled_when_the_estimate_collapses():
    # The book repricing downward through our level would FILL the stale bid
    # (adverse selection); the estimate seeing the move first must cancel it
    # before that happens.
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    wide_maker_universe(bot)
    bot.tick()
    assert bot.trades and bot.trades[0].buy_price == 41

    # Book pressure flips: microprice drops to ~39.5, below the 41c bid.
    bot.client.books["A"] = {"yes": [[30, 2000]], "no": [[60, 100]]}
    bot.tick()                                     # this tick refreshes the book
    bot.tick()                                     # ... and this one acts on it
    # The stale YES bid is gone (the freed slot may immediately repost on the
    # side the new estimate actually favours -- here NO).
    assert not any(t.side == "yes" for t in bot.trades)


def test_taker_edge_preempts_a_resting_maker_bid():
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    a = wide_maker_universe(bot)
    bot.tick()
    assert bot.trades[0].maker                     # slot used by a resting bid

    # A genuine taker edge appears in another event: yield the slot to it.
    b = mv("B", event="E2")
    bot.client.books["B"] = edge_book()
    set_markets(bot, [a, mv("Z", yes_bid=42, yes_ask=58), b, filler("E2")])
    bot.tick()
    assert [(t.ticker, t.maker, t.state) for t in bot.trades] == [
        ("B", False, TradeState.HOLDING)
    ]


def test_startup_warmup_blocks_immediate_entries():
    # Right after starting, per-ticker estimates can be "mature" while the
    # board-wide renormalization picture is still half-built -- the bot must
    # watch before it trades.
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    bot.startup_warmup = 10_000
    edged_universe(bot)
    bot.tick()
    assert bot.trades == []


def test_entries_are_paced_not_burst():
    # Two genuine-looking edges in different events: deploy into ONE, then
    # wait out the spacing window before committing the next slice. A burst
    # of simultaneous edges is the signature of a systematic estimate error.
    bot = make_bot(client=FakeBookClient(), max_positions=3)
    bot.entry_spacing = 10_000
    a = mv("A")
    b = mv("B", event="E2")
    bot.client.books["A"] = edge_book()
    bot.client.books["B"] = edge_book()
    set_markets(bot, [a, filler(), b, filler("E2")])
    bot.tick()
    assert len(bot.trades) == 1
    bot.tick()                                     # still inside the window
    assert len(bot.trades) == 1


def test_no_orderbook_capability_means_no_trades():
    bot = make_bot(client=FakeClient(), max_positions=1)
    set_markets(bot, [mv("A"), filler()])
    bot.tick()
    assert bot.trades == []  # estimates require book data; without it, never trade


def test_enters_underpriced_yes_at_the_ask():
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    edged_universe(bot)
    bot.tick()
    assert len(bot.trades) == 1
    trade = bot.trades[0]
    assert (trade.ticker, trade.side, trade.state) == ("A", "yes", TradeState.HOLDING)
    assert trade.buy_price == 92          # taker at the ask, never above it
    # Fractional sizing deploys 1/3 of $300 almost exactly: $100/0.92 = 108.69
    # contracts (Kelly ~0.58 doesn't bind). 0.01-contract granularity.
    assert trade.count == pytest.approx(108.69)


def test_enters_no_side_when_yes_is_overpriced():
    # Book 55/57 with estimate renormalized down to ~48.3%: NO at 45c has a
    # ~51.7% chance -> ~+5c net edge on the NO side.
    client = FakeBookClient({"A": {"yes": [[55, 5000]], "no": [[43, 5000]]}})
    bot = make_bot(client=client, max_positions=1)
    set_markets(bot, [
        mv("A", yes_bid=55, yes_ask=57),
        mv("Z", yes_bid=59, yes_ask=61),   # sibling bucket -> event sums to 116
    ])
    bot.tick()
    assert len(bot.trades) == 1
    trade = bot.trades[0]
    assert (trade.ticker, trade.side) == ("A", "no")
    assert trade.buy_price == 45          # NO ask = 100 - yes_bid
    # Thin edge -> the Kelly cap (~7.8%), not the 1/3 ceiling, sizes the trade:
    # est = 56 * 100/114 (bid-sum renorm), p_no = 100 - est, cost = 45 + fee.
    from kalshi_temp_bot.strategy import kelly_fraction, taker_fee_cents
    p_no = 100 - 56 * 100 / 114
    kelly = kelly_fraction(p_no, 45 + taker_fee_cents(45))
    assert kelly < 1 / 3
    expected = int((300_00 * kelly / 45) * 100) / 100
    assert trade.count == pytest.approx(expected, abs=0.01)
    assert trade.count == round(trade.count, 2)  # 0.01-contract granularity


def test_size_is_capped_by_entry_side_book_depth():
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    edged_universe(bot)
    bot.client.books["A"] = {"yes": [[91, 500]], "no": [[8, 50]]}  # only 50 to buy
    bot.tick()
    assert bot.trades[0].count == 50      # budget said 108; the book said 50


def test_respects_max_positions():
    bot = make_bot(client=FakeBookClient(), max_positions=2)
    a = edged_universe(bot, "A", "E")
    b = mv("B", event="F")
    bot.client.books["B"] = edge_book()
    set_markets(bot, [a, filler("E"), b, filler("F")])
    bot.tick()  # opens one
    bot.tick()  # opens the other
    bot.tick()  # at cap -> no third entry attempt
    assert sorted(t.ticker for t in bot.trades) == ["A", "B"]
    assert all(t.state is TradeState.HOLDING for t in bot.trades)


def test_one_position_per_event():
    # An overpriced event (bids sum to 114) gives BOTH buckets a NO edge --
    # but they stem from one event-level estimate, one failure mode. Only one
    # may be held at a time even with free slots.
    client = FakeBookClient({
        "A": {"yes": [[55, 5000]], "no": [[43, 5000]]},
        "Z": {"yes": [[59, 5000]], "no": [[39, 5000]]},
    })
    bot = make_bot(client=client, max_positions=5)
    set_markets(bot, [mv("A", yes_bid=55, yes_ask=57), mv("Z", yes_bid=59, yes_ask=61)])
    bot.tick()
    bot.tick()
    assert len(bot.trades) == 1
    assert bot.trades[0].event == "E"


def test_never_double_enters_same_market():
    bot = make_bot(client=FakeBookClient(), max_positions=5)
    edged_universe(bot)
    bot.tick()
    bot.tick()
    bot.tick()
    assert [t.ticker for t in bot.trades] == ["A"]


def test_no_liquidity_position_does_not_consume_a_slot():
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    a = edged_universe(bot)
    bot.tick()
    assert [t.ticker for t in bot.trades] == ["A"]

    a.yes_bid = 0                             # held but no bid -> can't exit
    assert bot._occupied_slots(bot._market_cache) == 0   # A doesn't occupy a slot

    # A new candidate (different event) can still be opened despite max_positions=1.
    b = mv("B", event="F")
    bot.client.books["B"] = edge_book()
    set_markets(bot, [a, filler("E"), b, filler("F")])
    bot.tick()
    assert sorted(t.ticker for t in bot.trades) == ["A", "B"]


def test_liquidity_exit_when_exit_depth_runs_low():
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    edged_universe(bot)
    bot.tick()                                # enters 108 YES @ 92c
    count = bot.trades[0].count
    bot.tick()                                # deep book -> keep holding
    assert bot.trades[0].state is TradeState.HOLDING

    # Exit-side (YES bids) depth collapses to <= 2x the position: sell now,
    # while the book can still fill the exit.
    bot.client.books["A"] = {"yes": [[91, count]], "no": [[8, 400]]}
    bot.tick()
    assert bot.trades == []

    # Re-entry cooldown: the market still shows an edge, but the bot just had
    # to flee its liquidity -- it must not buy straight back in.
    bot.tick()
    assert bot.trades == []


def test_no_liquidity_exit_into_an_empty_book():
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    a = edged_universe(bot)
    bot.tick()
    bot.client.books["A"] = {"yes": [], "no": [[8, 400]]}   # bids vanished entirely
    a.yes_bid = 0
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING  # no doomed sell attempted


def test_edge_reversal_exit_cashes_out_an_overpriced_bid():
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    a = edged_universe(bot)
    z = bot._market_cache[1]
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING

    # The market re-prices: bid 80/ask 82, but the event's bids now sum to 107
    # so the renormalized estimate is ~75.7%. Selling at 80c nets ~78.9c -> the
    # bid overprices our side by ~3.2c (> 3c): cash out.
    a.yes_bid, a.yes_ask = 80, 82
    z.yes_bid, z.yes_ask = 27, 29
    bot.client.books["A"] = {"yes": [[80, 100_000]], "no": [[18, 100_000]]}
    bot.tick()
    assert bot.trades == []

    # The same disagreement makes NO at 20c look attractive, but flipping
    # straight back into the market we just exited would churn fees on what
    # may be estimate noise: the cooldown blocks it.
    bot.tick()
    assert bot.trades == []


def test_take_profit_harvests_a_converged_winner():
    # The strategy cashes several edges a day from one slot: once the market
    # converges and pays the edge (bid nets a real gain over the all-in cost,
    # holding adds < 1c), the position is harvested -- first as a fee-free
    # offer resting inside the spread, filled when the bid rises to it.
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    a = edged_universe(bot)
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING  # bought at 92c (taker)

    a.yes_bid, a.yes_ask = 98, 100
    bot.client.books["A"] = {"yes": [[98, 100_000]], "no": [[1, 100_000]]}
    bot.tick()
    trade = bot.trades[0]
    assert trade.state is TradeState.OFFERING  # resting one tick inside the spread
    assert trade.exit_offer_price == 99

    a.yes_bid = 99                            # bid rises to the offer: filled
    bot.tick()
    assert bot.trades == []                   # position closed, slot free
    assert "A" not in bot._exited_at          # harvest -> no re-entry cooldown
    # Ledger: bought 108.69 @ 92c + 0.52c fee, sold @ 99c fee-free.
    assert bot._paper_pnl == pytest.approx(108.69 * (99 - 92.5152), rel=1e-3)
    assert bot._paper_closed == 1


def test_unconverged_position_is_held_not_harvested():
    # Bid only 1c above entry: selling would lock < 1c and abandon the bulk of
    # the edge -- keep holding until the market actually pays.
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    a = edged_universe(bot)
    bot.tick()

    a.yes_bid, a.yes_ask = 93, 94
    bot.client.books["A"] = {"yes": [[93, 500]], "no": [[6, 400]]}
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING


def test_position_rides_through_market_close():
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    a = edged_universe(bot)
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING

    a.close_time = datetime.now(timezone.utc) + timedelta(seconds=10)  # closing now
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING  # no forced sell

    set_markets(bot, [filler()])              # market gone from the scan entirely
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING  # kept; settles on its own


def test_paper_position_settles_after_market_leaves_the_scan():
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    a = edged_universe(bot)
    bot.tick()

    # A single missing scan (e.g. one failed series fetch) must not settle it...
    set_markets(bot, [filler()])
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING
    set_markets(bot, [a, filler()])           # back in the scan -> counter resets
    bot.tick()
    assert bot.trades[0].missing_scans == 0

    # ...but consistently gone means closed/settled: the paper position clears.
    set_markets(bot, [filler()])
    bot.tick()
    bot.tick()
    bot.tick()
    assert bot.trades == []


def test_does_not_enter_market_closing_too_soon():
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    a = mv("A", close_in_s=60)                # closes in 60s < the 300s floor
    bot.client.books["A"] = edge_book()
    set_markets(bot, [a, filler()])
    bot.tick()
    assert bot.trades == []
