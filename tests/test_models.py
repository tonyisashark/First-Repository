from kalshi_bot.models import Fill, Market, Order, OrderBook
from kalshi_bot.money import cents
from tests.conftest import NOW, book_payload, market_payload


def test_market_parses_modern_payload():
    m = Market.from_payload(market_payload(
        "KXHIGHNY-26JUN12-B85", event_ticker="KXHIGHNY-26JUN12",
        yes_bid=cents(41), yes_ask=cents(44), close_in=3600, volume_24h=777))
    assert m.ticker == "KXHIGHNY-26JUN12-B85"
    assert m.yes_bid == 410_000 and m.yes_ask == 440_000
    assert m.no_bid == 560_000              # complement included by builder
    assert m.spread == cents(3)
    assert m.mid == 425_000
    assert m.volume_24h == 777
    assert m.tradeable
    assert m.seconds_to_close(NOW) == 3600
    assert m.notional == 1_000_000


def test_market_legacy_cents_fallback():
    m = Market.from_payload({"ticker": "T", "status": "active",
                             "yes_bid": 41, "yes_ask": 44, "volume_24h": 5})
    assert m.yes_bid == 410_000 and m.yes_ask == 440_000
    assert m.volume_24h == 5


def test_tick_snap_with_price_ranges():
    payload = market_payload("T", yes_bid=cents(4))
    payload["price_ranges"] = [
        {"start": "0.00", "end": "0.05", "step": "0.001"},
        {"start": "0.05", "end": "1.00", "step": "0.01"},
    ]
    m = Market.from_payload(payload)
    assert m.tick_at(cents(3)) == 1_000          # 0.1c ticks below 5c
    assert m.tick_at(cents(50)) == 10_000
    assert m.snap(123_456, up=False) == 120_000  # 12.3456c -> 12c
    assert m.snap(123_456, up=True) == 130_000
    assert m.snap(31_500, up=False) == 31_000    # sub-cent grid in force
    # defaults to 1c grid without ranges
    m2 = Market.from_payload(market_payload("T2"))
    assert m2.snap(123_456, up=True) == 130_000


def test_orderbook_unified_semantics():
    book = OrderBook.from_payload(book_payload(
        yes_bids=[(cents(40), 100), (cents(39), 50)],
        no_bids=[(cents(55), 80), (cents(54), 10)],   # implies YES asks at 45/46
    ))
    assert book.best_bid("yes") == cents(40)
    assert book.best_ask("yes") == cents(45)
    assert book.best_bid("no") == cents(55)
    assert book.best_ask("no") == cents(60)           # 100 - best yes bid
    assert book.depth_at_or_better("yes", cents(45)) == 80
    assert book.depth_at_or_better("yes", cents(46)) == 90
    cost, got = book.cost_to_buy("yes", 90)
    assert got == 90
    assert cost == cents(45) * 80 + cents(46) * 10


def test_orderbook_legacy_cents_payload():
    book = OrderBook.from_payload({"orderbook": {"yes": [[40, 100]], "no": [[55, 80]]}})
    assert book.best_bid("yes") == cents(40)
    assert book.best_ask("yes") == cents(45)


def test_order_normalization_v2_and_legacy():
    v2 = Order.from_payload({"order_id": "o1", "ticker": "T", "side": "bid",
                             "price_dollars": "0.41", "remaining_count_fp": "7.00",
                             "status": "resting"})
    assert (v2.side, v2.yes_price, v2.remaining) == ("bid", 410_000, 7)

    legacy_no_buy = Order.from_payload({"order_id": "o2", "ticker": "T",
                                        "side": "no", "action": "buy",
                                        "no_price": 60, "remaining_count": 3})
    # buying NO at 60c == offering YES at 40c
    assert (legacy_no_buy.side, legacy_no_buy.yes_price) == ("ask", 400_000)

    legacy_yes_sell = Order.from_payload({"order_id": "o3", "ticker": "T",
                                          "side": "yes", "action": "sell",
                                          "yes_price": 70, "remaining_count": 2})
    assert (legacy_yes_sell.side, legacy_yes_sell.yes_price) == ("ask", 700_000)


def test_fill_normalization():
    fill = Fill.from_payload({
        "fill_id": "f1", "order_id": "o1", "ticker": "T",
        "outcome_side": "no", "action": "buy", "count_fp": "5.00",
        "no_price_dollars": "0.60", "is_taker": True, "fee_cost": "0.02",
        "ts": 1_700_000_000,
    })
    assert fill.book_side == "ask"            # buying NO == selling YES
    assert fill.yes_price == 400_000
    assert fill.count == 5 and fill.is_taker and fill.fee == 20_000
    direct = Fill.from_payload({"fill_id": "f2", "book_side": "bid",
                                "yes_price_dollars": "0.55", "count_fp": "1.00"})
    assert direct.book_side == "bid" and direct.yes_price == 550_000
