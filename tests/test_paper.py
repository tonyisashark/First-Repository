from kalshi_bot.models import OrderBook
from kalshi_bot.money import cents
from kalshi_bot.paper import PaperBroker
from tests.conftest import book_payload, mk, simple_book

USD = 1_000_000


def broker(state, cfg) -> PaperBroker:
    return PaperBroker(state, cfg)


def test_taker_buy_sweeps_levels_with_fees(state, cfg):
    b = broker(state, cfg)
    book = OrderBook.from_payload(book_payload(
        no_bids=[(cents(55), 80), (cents(54), 10)]))  # YES asks 45c x80, 46c x10
    result = b.place(ticker="T", side="bid", price=cents(46), count=90,
                     time_in_force="immediate_or_cancel", book=book)
    assert result["error"] is None
    assert result["filled"] == 90 and result["remaining"] == 0
    cost = cents(45) * 80 + cents(46) * 10
    assert result["avg_price"] == cost // 90
    # fees: ceil(.07*80*.45*.55)= $1.39 ; ceil(.07*10*.46*.54)= $0.18
    assert result["fee"] == 1_390_000 + 180_000
    assert b.positions()["T"].count == 90
    assert b.cash == 1000 * USD - cost - result["fee"]


def test_short_round_trip_nets_cash_correctly(state, cfg):
    b = broker(state, cfg)
    sell_book = OrderBook.from_payload(book_payload(yes_bids=[(cents(40), 50)]))
    b.place(ticker="T", side="ask", price=cents(40), count=10,
            time_in_force="immediate_or_cancel", book=sell_book)
    assert b.positions()["T"].count == -10
    # opened NO at 60c: cash down 10*$0.60 + fee ceil(.07*10*.4*.6)=$0.17
    assert b.cash == 1000 * USD - 6_000_000 - 170_000

    buy_book = OrderBook.from_payload(book_payload(no_bids=[(cents(70), 50)]))
    b.place(ticker="T", side="bid", price=cents(30), count=10,
            time_in_force="immediate_or_cancel", book=buy_book)
    assert "T" not in b.positions()
    # closed at 30c: collateral back 10*$0.70, fee ceil(.07*10*.3*.7)=$0.15
    assert b.cash == 1000 * USD - 6_000_000 - 170_000 + 7_000_000 - 150_000


def test_settlement_pays_winners(state, cfg):
    b = broker(state, cfg)
    book = OrderBook.from_payload(book_payload(no_bids=[(cents(10), 100)]))
    b.place(ticker="W", side="bid", price=cents(90), count=5,
            time_in_force="immediate_or_cancel", book=book)
    cash_before = b.cash
    market = mk("W", status="finalized", result="yes")
    b.sync_with_market(market, None)
    assert b.cash == cash_before + 5 * USD
    assert b.positions() == {}

    b.place(ticker="L", side="bid", price=cents(90), count=5,
            time_in_force="immediate_or_cancel", book=book)
    cash_before = b.cash
    b.sync_with_market(mk("L", status="finalized", result="no"), None)
    assert b.cash == cash_before  # YES position worthless
    assert b.positions() == {}


def test_post_only_rejects_crossing_and_rests_otherwise(state, cfg):
    b = broker(state, cfg)
    book = simple_book(yes_bid=cents(40), yes_ask=cents(45))
    crossed = b.place(ticker="T", side="bid", price=cents(45), count=10,
                      post_only=True, book=book)
    assert crossed["error"] and crossed["order_id"]
    assert b.orders() == []

    resting = b.place(ticker="T", side="bid", price=cents(39), count=10,
                      post_only=True, book=book)
    assert resting["error"] is None
    orders = b.orders()
    assert len(orders) == 1 and orders[0].yes_price == cents(39)
    assert b.cash == 1000 * USD  # no cash moves while resting


def test_maker_fill_when_book_crosses(state, cfg):
    b = broker(state, cfg)
    quiet = simple_book(yes_bid=cents(40), yes_ask=cents(45))
    b.place(ticker="T", side="bid", price=cents(42), count=10,
            post_only=True, book=quiet, strategy="mm")
    crossing = OrderBook.from_payload(book_payload(
        no_bids=[(cents(59), 6)]))  # YES ask drops to 41c with 6 contracts
    b.sync_with_market(mk("T"), crossing)
    pos = b.positions()["T"]
    assert pos.count == 6
    # filled at our 42c price, maker fee: ceil(.0175*6*.42*.58) = $0.03
    assert b.cash == 1000 * USD - 6 * cents(42) - 30_000
    assert b.orders()[0].remaining == 4


def test_reduce_only_caps_to_position(state, cfg):
    b = broker(state, cfg)
    book = OrderBook.from_payload(book_payload(no_bids=[(cents(60), 100)]))
    b.place(ticker="T", side="bid", price=cents(40), count=5,
            time_in_force="immediate_or_cancel", book=book)
    sell_book = OrderBook.from_payload(book_payload(yes_bids=[(cents(50), 100)]))
    result = b.place(ticker="T", side="ask", price=cents(50), count=99,
                     time_in_force="immediate_or_cancel", reduce_only=True,
                     book=sell_book)
    assert result["filled"] == 5
    assert "T" not in b.positions()
    nothing = b.place(ticker="T", side="ask", price=cents(50), count=1,
                      time_in_force="immediate_or_cancel", reduce_only=True,
                      book=sell_book)
    assert nothing["error"]


def test_fok_is_all_or_nothing(state, cfg):
    b = broker(state, cfg)
    book = OrderBook.from_payload(book_payload(no_bids=[(cents(60), 5)]))
    result = b.place(ticker="T", side="bid", price=cents(40), count=10,
                     time_in_force="fill_or_kill", book=book)
    assert result["error"] and result["filled"] == 0
    assert b.cash == 1000 * USD and b.positions() == {}


def test_gtc_remainder_rests_and_cancel_works(state, cfg):
    b = broker(state, cfg)
    book = OrderBook.from_payload(book_payload(no_bids=[(cents(60), 4)]))
    result = b.place(ticker="T", side="bid", price=cents(40), count=10,
                     time_in_force="good_till_canceled", book=book)
    assert result["filled"] == 4 and result["remaining"] == 6
    orders = b.orders()
    assert len(orders) == 1 and orders[0].remaining == 6
    assert b.cancel(orders[0].order_id)
    assert b.orders() == []
