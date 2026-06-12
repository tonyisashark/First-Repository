from kalshi_bot.models import OrderBook
from kalshi_bot.money import cents
from kalshi_bot.strategies.arbitrage import (covers_real_line, find_candidates,
                                             plan_opportunity)
from tests.conftest import NOW, book_payload, ev, market_payload


def range_event(event="EV-R", asks=(30, 30, 30), bids=(28, 28, 28), depth=100):
    """Three-bucket mutually exclusive event tiling the real line."""
    payloads = [
        market_payload(f"{event}-LO", event, yes_bid=cents(bids[0]),
                       yes_ask=cents(asks[0]), strike_type="less", cap_strike=50.0),
        market_payload(f"{event}-MID", event, yes_bid=cents(bids[1]),
                       yes_ask=cents(asks[1]), strike_type="between",
                       floor_strike=50.0, cap_strike=60.0),
        market_payload(f"{event}-HI", event, yes_bid=cents(bids[2]),
                       yes_ask=cents(asks[2]), strike_type="greater",
                       floor_strike=60.0),
    ]
    event_obj = ev(event, payloads)
    books = {}
    for market, ask, bid in zip(event_obj.markets, asks, bids):
        books[market.ticker] = OrderBook.from_payload(book_payload(
            yes_bids=[(cents(bid), depth)],
            no_bids=[(cents(100 - ask), depth)],
        ))
    return event_obj, books


def test_covers_real_line():
    good = range_event()[0]
    assert covers_real_line(good.markets)

    gap = ev("EV-G", [
        market_payload("G-LO", "EV-G", strike_type="less", cap_strike=50.0),
        market_payload("G-HI", "EV-G", strike_type="greater", floor_strike=55.0),
    ])
    assert not covers_real_line(gap.markets)

    integer_buckets = ev("EV-I", [
        market_payload("I-LO", "EV-I", strike_type="less", cap_strike=50.0),
        market_payload("I-MID", "EV-I", strike_type="between",
                       floor_strike=51.0, cap_strike=52.0),
        market_payload("I-HI", "EV-I", strike_type="greater", floor_strike=53.0),
    ])
    assert covers_real_line(integer_buckets.markets)  # 1-unit gaps tolerated

    no_top = ev("EV-T", [
        market_payload("T-LO", "EV-T", strike_type="less", cap_strike=50.0),
        market_payload("T-MID", "EV-T", strike_type="between",
                       floor_strike=50.0, cap_strike=60.0),
    ])
    assert not covers_real_line(no_top.markets)


def test_find_candidates_filters(cfg):
    cheap, _ = range_event("EV-CHEAP", asks=(30, 30, 30))
    fair, _ = range_event("EV-FAIR", asks=(40, 35, 27), bids=(38, 33, 25))
    rich, _ = range_event("EV-RICH", asks=(45, 40, 38), bids=(43, 38, 36))
    not_mx = ev("EV-NMX", [market_payload("N-1", "EV-NMX", yes_bid=cents(20),
                                          yes_ask=cents(22)),
                           market_payload("N-2", "EV-NMX", yes_bid=cents(20),
                                          yes_ask=cents(22))],
                mutually_exclusive=False)
    closing, _ = range_event("EV-SOON")
    for market in closing.markets:
        market.close_ts = NOW + 30

    out = find_candidates([cheap, fair, rich, not_mx, closing], NOW, cfg)
    tickers = [event.event_ticker for event in out]
    assert "EV-CHEAP" in tickers          # long arb margin 10c
    assert "EV-RICH" in tickers           # short arb margin 17c
    assert "EV-FAIR" not in tickers       # asks sum 102, bids sum 96: no margin
    assert "EV-NMX" not in tickers
    assert "EV-SOON" not in tickers
    assert tickers[0] == "EV-RICH"        # biggest margin first


def test_plan_long_set_math(cfg):
    event, books = range_event(asks=(30, 30, 30), depth=50)
    opp = plan_opportunity(event, books, cfg)
    assert opp is not None and opp.direction == "long"
    assert opp.sets == 40                              # 50 * 0.8 shave
    # cost 40*90c = $36 ; fees 3 x ceil(.07*40*.3*.7)=3*$0.59 ; payout $40
    assert opp.total_profit == 40_000_000 - 36_000_000 - 3 * 590_000
    assert opp.profit_per_set == opp.total_profit // 40
    assert opp.capital_per_set == (36_000_000 + 3 * 590_000 + 39) // 40


def test_plan_short_set_math(cfg):
    event, books = range_event(asks=(44, 38, 32), bids=(42, 36, 30), depth=50)
    opp = plan_opportunity(event, books, cfg)
    assert opp is not None and opp.direction == "short"
    assert opp.sets == 40
    # margin: bids sum 108c -> 8c per set, minus taker fees on each leg
    fees = 690_000 + 650_000 + 590_000
    assert opp.total_profit == 8 * 40 * 10_000 - fees
    # capital = NO collateral: (58+64+70)c * 40 sets + fees
    assert opp.total_capital >= (cents(58) + cents(64) + cents(70)) * 40


def test_long_requires_exhaustiveness(cfg):
    event, books = range_event(asks=(30, 30, 30), bids=(10, 10, 10))
    for market in event.markets:
        market.strike_type = ""        # destroy structural proof
        market.floor_strike = None
        market.cap_strike = None
    # bids sum to 30c < 97c floor: no consensus an outcome must hit -> no long
    assert plan_opportunity(event, books, cfg) is None

    event2, books2 = range_event(asks=(35, 33, 31), bids=(34, 32, 31))
    for market in event2.markets:
        market.strike_type = ""
        market.floor_strike = None
        market.cap_strike = None
    # bids sum 97c: consensus proxy satisfied, asks sum 99c -> tiny long arb
    opp = plan_opportunity(event2, books2, cfg)
    assert opp is None or opp.direction == "long"   # fee-gated, never bogus short


def test_plan_respects_min_profit(cfg):
    # asks sum to 99c: 1c gross per set, fees eat it -> no opportunity
    event, books = range_event(asks=(34, 33, 32), depth=50)
    assert plan_opportunity(event, books, cfg) is None


def test_one_sided_book_blocks_long(cfg):
    event, books = range_event(asks=(30, 30, 30))
    empty = OrderBook.from_payload(book_payload(yes_bids=[(cents(28), 100)]))
    books[event.markets[1].ticker] = empty             # no asks on middle leg
    opp = plan_opportunity(event, books, cfg)
    assert opp is None or opp.direction == "short"
