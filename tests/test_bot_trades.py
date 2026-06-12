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
    return bot


def mv(ticker, *, event="E", yes_bid=91, yes_ask=92, volume=1000, close_in_s=5 * 3600):
    return MarketView(
        ticker=ticker, event_ticker=event, yes_bid=yes_bid, yes_ask=yes_ask,
        last_price=None, volume=volume,
        close_time=datetime.now(timezone.utc) + timedelta(seconds=close_in_s),
        status="open",
    )


def filler(event="E", ticker=None):
    # A complementary low bucket so the event's estimates sum to slightly less
    # than 100 (91.5 + 3): renormalization lifts the big bucket's estimate to
    # ~96.8%, well above its 92c ask -> a genuine YES edge.
    return mv(ticker or f"ZF_{event}", event=event, yes_bid=2, yes_ask=4, volume=1)


def edge_book():
    # Matches mv() defaults: bid 91 (500 deep), ask 92 (400 deep) -> microprice
    # ~91.56; with the filler the renormalized estimate is ~96.8%.
    return {"yes": [[91, 500]], "no": [[8, 400]]}


def edged_universe(bot, ticker="A", event="E"):
    market = mv(ticker, event=event)
    bot.client.books[ticker] = edge_book()
    set_markets(bot, [market, filler(event)])
    return market


def set_markets(bot, views):
    bot._market_cache = views
    bot._last_scan = time.time()


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
    # Thin edge -> the Kelly cap (~9.4%), not the 1/3 ceiling, sizes the trade.
    assert 62 <= trade.count < 63
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

    # The market re-prices: bid 80/ask 82, but the event now sums to 109 so the
    # renormalized estimate is ~74.3%. Selling at 80c nets ~78.9c -> the bid
    # overprices our side by ~4.6c (> 3c): cash out.
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


def test_winner_is_not_dumped_by_edge_exit():
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    a = edged_universe(bot)
    bot.tick()

    # Riding toward settlement: bid 98/ask 100, event sum stays sane -> the
    # estimate (~98+) exceeds the bid; no reversal, keep riding.
    a.yes_bid, a.yes_ask = 98, 100
    bot.client.books["A"] = {"yes": [[98, 100_000]], "no": [[1, 100_000]]}
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


def test_does_not_enter_market_closing_too_soon():
    bot = make_bot(client=FakeBookClient(), max_positions=1)
    a = mv("A", close_in_s=60)                # closes in 60s < the 300s floor
    bot.client.books["A"] = edge_book()
    set_markets(bot, [a, filler()])
    bot.tick()
    assert bot.trades == []
