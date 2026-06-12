"""Tests for multi-position management: max positions, the no-liquidity slot
exemption, the stop-loss and liquidity-aware exits, and holding through close
(paper mode)."""

import time
from datetime import datetime, timedelta, timezone

from kalshi_temp_bot.bot import TradingBot, TradeState
from kalshi_temp_bot.config import Config
from kalshi_temp_bot.strategy import MarketView

NOW = datetime.now(timezone.utc)


class FakeClient:
    auth = None  # -> paper-balance sizing, no network
    # no get_orderbook -> microprice estimation and the liquidity exit are skipped


class FakeOrderbookClient(FakeClient):
    """FakeClient that also serves a fixed orderbook for every ticker."""

    def __init__(self, yes_levels, no_levels=None):
        self.yes_levels = yes_levels
        self.no_levels = no_levels or []

    def get_orderbook(self, ticker):
        return {"yes": list(self.yes_levels), "no": list(self.no_levels)}


def make_bot(client=None, **overrides):
    cfg = Config.from_env()
    cfg.dry_run = True
    cfg.scan_interval_seconds = 10_000          # we control the market cache
    cfg.heartbeat_interval_seconds = 10_000
    cfg.liquidity_poll_seconds = 0.0            # poll the book on every tick
    cfg.chance_smoothing_seconds = 0.0          # no EWMA lag in tests
    cfg.paper_balance_cents = 300_00
    cfg.buy_chance_min_cents = 90
    cfg.buy_chance_max_cents = 95
    cfg.max_spread_cents = 5
    cfg.volume_threshold_ratio = 0.0            # any positive volume qualifies
    cfg.max_volume_scope = "global"
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return TradingBot(client=client or FakeClient(), config=cfg)


def mv(ticker, *, yes_bid=92, yes_ask=94, volume=1000, close_in_s=5 * 3600):
    # Defaults: mid 93 (inside the 90-95 band), spread 2 (inside the gate).
    return MarketView(
        ticker=ticker, event_ticker="E", yes_bid=yes_bid, yes_ask=yes_ask,
        last_price=None, volume=volume,
        close_time=datetime.now(timezone.utc) + timedelta(seconds=close_in_s),
        status="open",
    )


def filler():
    # A small complementary bucket so single-market tests form a realistic
    # event whose mids sum to ~100 (93 + 7) and renormalization is benign.
    return mv("ZFILL", yes_bid=5, yes_ask=9, volume=1)


def set_markets(bot, views):
    bot._market_cache = views
    bot._last_scan = time.time()


def settle(market, *, won):
    # Drive a market to its settled rails (winner ~99, loser ~1).
    if won:
        market.yes_bid, market.yes_ask = 98, 100
    else:
        market.yes_bid, market.yes_ask = 0, 2


def test_respects_max_positions():
    bot = make_bot(max_positions=2)
    set_markets(bot, [mv("A", volume=300), mv("B", volume=200), mv("C", volume=100)])
    bot.tick()  # opens best (A)
    bot.tick()  # opens next (B)
    bot.tick()  # at cap -> no third
    assert sorted(t.ticker for t in bot.trades) == ["A", "B"]
    assert all(t.state is TradeState.HOLDING for t in bot.trades)


def test_never_double_enters_same_market():
    bot = make_bot(max_positions=5)
    set_markets(bot, [mv("A", volume=300), filler()])
    bot.tick()
    bot.tick()
    bot.tick()
    assert [t.ticker for t in bot.trades] == ["A"]


def test_entry_limit_is_capped_at_band_max():
    bot = make_bot(max_positions=1)
    a = mv("A", volume=300, yes_bid=93, yes_ask=97)  # mid 95 in band; ask above max
    set_markets(bot, [a, filler()])
    bot.tick()
    assert bot.trades[0].buy_price == 95  # capped at buy_chance_max, not the 97 ask


def test_no_liquidity_position_does_not_consume_a_slot():
    bot = make_bot(max_positions=1)
    a = mv("A", volume=300)
    set_markets(bot, [a, filler()])
    bot.tick()
    assert [t.ticker for t in bot.trades] == ["A"]

    a.yes_bid = 0                             # held but no bid -> can't exit
    assert bot._occupied_slots(bot._market_cache) == 0   # A doesn't occupy a slot

    # A new liquid candidate can still be opened despite max_positions=1.
    b = mv("B", volume=200)
    set_markets(bot, [a, b, filler()])
    bot.tick()
    assert sorted(t.ticker for t in bot.trades) == ["A", "B"]


def test_liquidity_exit_when_bid_depth_runs_low():
    client = FakeOrderbookClient(yes_levels=[[92, 100_000]], no_levels=[[6, 50_000]])
    bot = make_bot(client=client, max_positions=1, liquidity_exit_buffer=2.0)
    a = mv("A", volume=300)
    z = filler()
    set_markets(bot, [a, z])
    bot.tick()                                # enters at the 94c ask
    count = bot.trades[0].count
    bot.tick()                                # deep book -> keep holding
    assert bot.trades[0].state is TradeState.HOLDING

    client.yes_levels = [[92, count]]         # depth == position <= 2x -> sell now
    settle(a, won=True)                       # price rode up toward settlement
    settle(z, won=False)
    bot.tick()
    assert bot.trades == []                   # sold while liquidity remained


def test_no_liquidity_exit_into_an_empty_book():
    client = FakeOrderbookClient(yes_levels=[[92, 100_000]], no_levels=[[6, 50_000]])
    bot = make_bot(client=client, max_positions=1, liquidity_exit_buffer=2.0)
    set_markets(bot, [mv("A", volume=300), filler()])
    bot.tick()
    client.yes_levels = []                    # book emptied: nothing to sell into
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING  # no doomed sell attempted


def test_stop_loss_exit():
    bot = make_bot(max_positions=1, min_sell_price_cents=85)
    a = mv("A", volume=300)
    set_markets(bot, [a, filler()])
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING
    a.yes_bid, a.yes_ask = 60, 62             # bid at/below the stop -> sell
    bot.tick()
    assert bot.trades == []


def test_stop_loss_skips_worthless_position_with_no_bid():
    bot = make_bot(max_positions=1, min_sell_price_cents=85)
    a = mv("A", volume=300)
    set_markets(bot, [a, filler()])
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING
    settle(a, won=False)                      # worthless AND no bid to sell into
    a.yes_bid = 0
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING  # held, not "failed to sell"


def test_position_rides_through_market_close():
    bot = make_bot(max_positions=1)
    a = mv("A", volume=300)
    set_markets(bot, [a, filler()])
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING

    a.close_time = datetime.now(timezone.utc) + timedelta(seconds=10)  # closing now
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING  # no forced sell

    set_markets(bot, [filler()])              # market gone from the scan entirely
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING  # kept; settles on its own


def test_does_not_enter_market_closing_too_soon():
    bot = make_bot(max_positions=1, min_seconds_to_close=300)
    set_markets(bot, [mv("A", volume=300, close_in_s=60), filler()])  # closes in 60s
    bot.tick()
    assert bot.trades == []
