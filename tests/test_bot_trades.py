"""Tests for multi-position management: max positions, the no-liquidity slot
exemption, the stop-loss, take-profit and close-deadline exits (paper mode)."""

import time
from datetime import datetime, timedelta, timezone

from kalshi_temp_bot.bot import TradingBot, TradeState
from kalshi_temp_bot.config import Config
from kalshi_temp_bot.strategy import MarketView

NOW = datetime.now(timezone.utc)


class FakeClient:
    auth = None  # -> paper-balance sizing, no network


def make_bot(**overrides):
    cfg = Config.from_env()
    cfg.dry_run = True
    cfg.scan_interval_seconds = 10_000          # we control the market cache
    cfg.heartbeat_interval_seconds = 10_000
    cfg.paper_balance_cents = 300_00
    cfg.volume_threshold_ratio = 0.0            # any positive volume qualifies
    cfg.max_volume_scope = "global"
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return TradingBot(client=FakeClient(), config=cfg)


def mv(ticker, *, yes_ask=90, yes_bid=80, volume=1000, close_in_s=5 * 3600):
    return MarketView(
        ticker=ticker, event_ticker="E", yes_bid=yes_bid, yes_ask=yes_ask,
        last_price=85, volume=volume,
        close_time=datetime.now(timezone.utc) + timedelta(seconds=close_in_s),
        status="open",
    )


def set_markets(bot, views):
    bot._market_cache = views
    bot._last_scan = time.time()


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
    set_markets(bot, [mv("A", volume=300)])
    bot.tick()
    bot.tick()
    bot.tick()
    assert [t.ticker for t in bot.trades] == ["A"]


def test_no_liquidity_position_does_not_consume_a_slot():
    bot = make_bot(max_positions=1)
    a = mv("A", volume=300, yes_bid=0)        # held but no bid -> can't exit
    set_markets(bot, [a])
    bot.tick()
    assert [t.ticker for t in bot.trades] == ["A"]
    assert bot._occupied_slots(bot._market_cache) == 0   # A doesn't occupy a slot

    # A new liquid candidate can still be opened despite max_positions=1.
    b = mv("B", volume=200, yes_bid=80)
    set_markets(bot, [a, b])
    bot.tick()
    assert sorted(t.ticker for t in bot.trades) == ["A", "B"]


def test_take_profit_exit():
    bot = make_bot(max_positions=1)
    a = mv("A", volume=300, yes_bid=80)
    set_markets(bot, [a])
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING
    a.yes_bid, a.yes_ask = 99, 100            # bid hits 99c target (market settling)
    bot.tick()
    assert bot.trades == []                   # sold -> slot freed


def test_stop_loss_exit():
    bot = make_bot(max_positions=1, min_sell_price_cents=85)
    a = mv("A", volume=300, yes_bid=88)
    set_markets(bot, [a])
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING
    a.yes_bid, a.yes_ask = 85, 86             # bid at/below the stop -> sell
    bot.tick()
    assert bot.trades == []


def test_force_sell_before_close():
    bot = make_bot(max_positions=1)
    a = mv("A", volume=300, yes_bid=80)
    set_markets(bot, [a])
    bot.tick()
    assert bot.trades[0].state is TradeState.HOLDING
    a.close_time = datetime.now(timezone.utc) + timedelta(seconds=30)  # within 60s buffer
    bot.tick()
    assert bot.trades == []                   # flattened before close


def test_does_not_enter_market_closing_too_soon():
    bot = make_bot(max_positions=1, min_seconds_to_close=300)
    set_markets(bot, [mv("A", volume=300, close_in_s=60)])  # closes in 60s
    bot.tick()
    assert bot.trades == []
