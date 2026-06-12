"""End-to-end paper-mode cycle against a scripted exchange.

A synthetic universe contains one of everything:
- a mutually exclusive range event priced for a long arbitrage,
- a 95-cent favorite for the longshot strategy,
- a liquid, wide-spread market for the market maker.

One bot cycle should: detect and execute the arb across all legs, buy the
favorite, rest two-sided quotes, snapshot equity -- all inside risk caps.
"""

import time

from kalshi_bot.bot import TradingBot
from kalshi_bot.config import Config
from kalshi_bot.money import cents
from kalshi_bot.paper import PaperBroker
from kalshi_bot.state import StateStore
from tests.conftest import book_payload, event_payload, market_payload

NOW = int(time.time())
USD = 1_000_000


class FakeClient:
    """Static exchange: one page of events, fixed books."""

    def __init__(self, events, books):
        self._events = events
        self._books = books
        self.cancelled = []

    def exchange_status(self):
        return {"trading_active": True}

    def get_events_page(self, status="open", cursor=None,
                        with_nested_markets=True, limit=200):
        return list(self._events), None

    def get_orderbooks(self, tickers):
        return {t: self._books[t] for t in tickers if t in self._books}

    def get_markets(self, status="open", tickers=None, **kwargs):
        out = []
        for event in self._events:
            for market in event["markets"]:
                if tickers is None or market["ticker"] in tickers:
                    out.append(market)
        return out


def build_universe():
    arb_markets = [
        market_payload("ARB-LO", "ARB", yes_bid=cents(28), yes_ask=cents(30),
                       volume_24h=50, strike_type="less", cap_strike=50.0),
        market_payload("ARB-MID", "ARB", yes_bid=cents(28), yes_ask=cents(30),
                       volume_24h=50, strike_type="between",
                       floor_strike=50.0, cap_strike=60.0),
        market_payload("ARB-HI", "ARB", yes_bid=cents(28), yes_ask=cents(30),
                       volume_24h=50, strike_type="greater", floor_strike=60.0),
    ]
    longshot_market = market_payload("LS-1", "LS", yes_bid=cents(93),
                                     yes_ask=cents(95), volume_24h=5_000,
                                     close_in=2 * 86_400)
    mm_market = market_payload("MM-1", "MM", yes_bid=cents(40),
                               yes_ask=cents(44), volume_24h=10_000)
    events = [
        event_payload("ARB", arb_markets, mutually_exclusive=True),
        event_payload("LS", [longshot_market], mutually_exclusive=False),
        event_payload("MM", [mm_market], mutually_exclusive=False),
    ]
    books = {
        "ARB-LO": book_payload(yes_bids=[(cents(28), 100)],
                               no_bids=[(cents(70), 100)]),
        "ARB-MID": book_payload(yes_bids=[(cents(28), 100)],
                                no_bids=[(cents(70), 100)]),
        "ARB-HI": book_payload(yes_bids=[(cents(28), 100)],
                               no_bids=[(cents(70), 100)]),
        "LS-1": book_payload(yes_bids=[(cents(93), 50)],
                             no_bids=[(cents(5), 500)]),
        "MM-1": book_payload(yes_bids=[(cents(40), 300)],
                             no_bids=[(cents(56), 300)]),
    }
    return events, books


def make_bot(tmp_path):
    cfg = Config(state_db_path=str(tmp_path / "bot.sqlite3"),
                 mm_quote_frac=0.10)   # keep both quotes inside per-market cap
    events, books = build_universe()
    client = FakeClient(events, books)
    state = StateStore(cfg.state_db_path)
    paper = PaperBroker(state, cfg)
    return TradingBot(cfg, client, state, paper=paper), state, paper


def test_full_cycle_trades_all_strategies(tmp_path):
    bot, state, paper = make_bot(tmp_path)
    statuses = []
    bot.status_listener = statuses.append
    view = bot.run_cycle(now=NOW)

    # the GUI status feed fired with a complete, sane payload
    assert statuses and statuses[-1]["mode"] == "paper"
    assert statuses[-1]["equity"] > 0
    assert set(statuses[-1]["used"]) <= {"arb", "longshot", "mm", "unattributed"}
    assert statuses[-1]["halted"] is False

    positions = paper.positions()
    # arb bought all three legs in equal size, capped by the event limit
    arb_counts = {positions[t].count for t in ("ARB-LO", "ARB-MID", "ARB-HI")}
    assert len(arb_counts) == 1
    sets = arb_counts.pop()
    assert 1 <= sets <= 80
    event_spend = sum(positions[t].exposure for t in ("ARB-LO", "ARB-MID", "ARB-HI"))
    assert event_spend <= bot.cfg.max_event_exposure_frac * 1000 * USD

    # longshot bought the favorite within its 2% per-market cap
    assert positions["LS-1"].count >= 1
    assert positions["LS-1"].exposure <= bot.cfg.longshot_per_market_frac * 1000 * USD

    # market maker resting on both sides
    mm_orders = [o for o in paper.orders() if o.ticker == "MM-1"]
    assert {o.side for o in mm_orders} == {"bid", "ask"}

    # bookkeeping: snapshot exists, journal recorded the arb, attribution works
    assert state.latest_snapshot() is not None
    kinds = {row[1] for row in state.recent_journal(50)}
    assert "arb_attempt" in kinds and "longshot_entry" in kinds
    owner = state.latest_strategy_by_ticker()
    assert owner["LS-1"] == "longshot" and owner["MM-1"] == "mm"
    assert owner["ARB-LO"] == "arb"

    # cash actually left the (simulated) account
    assert paper.cash < 1000 * USD
    assert view.equity > 0


def test_second_cycle_respects_caps_and_keeps_quotes(tmp_path):
    bot, state, paper = make_bot(tmp_path)
    bot.run_cycle(now=NOW)
    orders_before = {o.order_id for o in paper.orders()}
    positions_before = {t: p.count for t, p in paper.positions().items()}

    bot.run_cycle(now=NOW + 30)

    # quotes are kept (no churn when the book is unchanged)
    assert {o.order_id for o in paper.orders()} == orders_before
    # longshot does not run again before its interval
    assert paper.positions()["LS-1"].count == positions_before["LS-1"]
    # arb may add sets but the event cap must hold
    positions = paper.positions()
    event_spend = sum(positions[t].exposure for t in ("ARB-LO", "ARB-MID", "ARB-HI"))
    equity_cap = bot.cfg.max_event_exposure_frac * 1100 * USD   # generous bound
    assert event_spend <= equity_cap


def test_settlement_compounds_the_bankroll(tmp_path):
    """Win settles -> cash grows -> the next sizing base is larger."""
    cfg = Config(state_db_path=str(tmp_path / "bot.sqlite3"),
                 arb_enabled=False, mm_enabled=False)
    ls_market = market_payload("LS-1", "LS", yes_bid=cents(93), yes_ask=cents(95),
                               volume_24h=5_000, close_in=2 * 86_400)
    events = [event_payload("LS", [ls_market], mutually_exclusive=False)]
    books = {"LS-1": book_payload(yes_bids=[(cents(93), 50)],
                                  no_bids=[(cents(5), 500)])}
    client = FakeClient(events, books)
    state = StateStore(cfg.state_db_path)
    paper = PaperBroker(state, cfg)
    bot = TradingBot(cfg, client, state, paper=paper)

    bot.run_cycle(now=NOW)
    bought = paper.positions()["LS-1"].count
    assert bought >= 1
    cash_after_entry = paper.cash
    assert cash_after_entry < 1000 * USD

    # the market resolves YES at the exchange
    ls_market["status"] = "finalized"
    ls_market["result"] = "yes"
    view = bot.run_cycle(now=NOW + 30)

    assert paper.positions() == {}
    assert paper.cash == cash_after_entry + bought * USD
    assert view.equity > 1000 * USD            # compounding base grew
    kinds = {row[1] for row in state.recent_journal(20)}
    assert "paper_settlement" in kinds


def test_kill_switch_cancels_quotes(tmp_path):
    bot, state, paper = make_bot(tmp_path)
    bot.run_cycle(now=NOW)
    assert paper.orders()
    bot.risk.kill("test")
    bot.run_cycle(now=NOW + 5)
    assert paper.orders() == []          # resting quotes cancelled on halt
    counts_before = {t: p.count for t, p in paper.positions().items()}
    bot.run_cycle(now=NOW + 10)
    assert {t: p.count for t, p in paper.positions().items()} == counts_before
