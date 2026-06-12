from kalshi_bot.execution import Executor
from kalshi_bot.money import cents
from kalshi_bot.paper import PaperBroker
from kalshi_bot.risk import RiskManager
from tests.conftest import mk, simple_book


def make_executor(state, cfg):
    risk = RiskManager(cfg, state)
    paper = PaperBroker(state, cfg)
    return Executor(cfg, state, risk, paper=paper), risk, paper


def test_place_records_order_and_snaps_price(state, cfg):
    executor, _, paper = make_executor(state, cfg)
    market = mk("T-E-M")
    book = simple_book(cents(40), cents(45), depth=100)
    result = executor.place(strategy="mm", market=market, side="bid",
                            price=cents(39) + 4_321, count=10,
                            post_only=True, book=book)
    assert result["error"] is None
    row = state.order_row(result["order_id"])
    assert row is not None
    assert row[2] == "mm" and row[3] == "bid"
    assert row[4] == cents(39)            # snapped down onto the cent grid
    assert paper.orders()[0].yes_price == cents(39)


def test_entry_price_band_enforced(state, cfg):
    executor, _, _ = make_executor(state, cfg)
    market = mk("T")
    too_high = executor.place(strategy="longshot", market=market, side="bid",
                              price=cents(99), count=5,
                              time_in_force="immediate_or_cancel")
    assert "ceiling" in too_high["error"]
    reduce_ok = executor.place(strategy="kill", market=market, side="ask",
                               price=cents(99), count=5, reduce_only=True,
                               time_in_force="immediate_or_cancel")
    # exits are exempt from the entry band; this fails only on empty position
    assert "reduce" in (reduce_ok["error"] or "")


def test_halt_blocks_entries_but_not_reduce(state, cfg):
    executor, risk, _ = make_executor(state, cfg)
    risk.kill("test halt")
    market = mk("T")
    book = simple_book(cents(40), cents(45))
    blocked = executor.place(strategy="arb", market=market, side="bid",
                             price=cents(45), count=5, book=book,
                             time_in_force="immediate_or_cancel")
    assert "halted" in blocked["error"]

    # seed a position so reduce-only has something to reduce
    state.paper_set_position("T", 5, 0)
    allowed = executor.place(strategy="kill", market=market, side="ask",
                             price=cents(40), count=5, reduce_only=True,
                             book=book, time_in_force="immediate_or_cancel")
    assert allowed["error"] is None and allowed["filled"] == 5


def test_cancel_marks_state(state, cfg):
    executor, _, paper = make_executor(state, cfg)
    market = mk("T")
    book = simple_book(cents(40), cents(45))
    result = executor.place(strategy="mm", market=market, side="bid",
                            price=cents(39), count=10, post_only=True, book=book)
    order_id = result["order_id"]
    assert executor.cancel(order_id)
    assert state.order_row(order_id)[6] == "canceled"
    assert paper.orders() == []


def test_batch_place_paper_falls_back_to_sequential(state, cfg):
    executor, _, paper = make_executor(state, cfg)
    markets = [mk("A-E-M", event_ticker="A-E"), mk("B-E-M", event_ticker="B-E")]
    books = {m.ticker: simple_book(cents(30), cents(33), depth=50) for m in markets}
    legs = [{"market": m, "side": "bid", "price": cents(33), "count": 5,
             "time_in_force": "immediate_or_cancel", "book": books[m.ticker]}
            for m in markets]
    results = executor.batch_place("arb", legs)
    assert [r["filled"] for r in results] == [5, 5]
    assert len(paper.positions()) == 2
