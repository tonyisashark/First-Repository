"""Unit tests for unit/money conversion helpers."""

from kalshi_temp_bot import money


def test_dollars_to_cents():
    assert money.dollars_to_cents("0.90") == 90
    assert money.dollars_to_cents("0.99") == 99
    assert money.dollars_to_cents("0.01") == 1
    assert money.dollars_to_cents(None) is None


def test_cents_to_dollars_str():
    assert money.cents_to_dollars_str(90) == "0.90"
    assert money.cents_to_dollars_str(99) == "0.99"
    assert money.cents_to_dollars_str(1) == "0.01"


def test_fixed_point_str():
    assert money.fixed_point_str(10) == "10.00"
    assert money.fixed_point_str(111) == "111.00"


def test_market_price_cents_prefers_dollars_field():
    market = {"yes_ask_dollars": "0.90", "yes_ask": 12}
    assert money.market_price_cents(market, "yes_ask") == 90


def test_market_price_cents_legacy_cents_fallback():
    market = {"yes_ask": 90}
    assert money.market_price_cents(market, "yes_ask") == 90


def test_market_price_cents_missing():
    assert money.market_price_cents({}, "yes_ask") is None


def test_market_volume():
    assert money.market_volume({"volume_fp": "33896.00"}) == 33896.0
    assert money.market_volume({"volume": 100}) == 100.0
    assert money.market_volume({}) == 0.0


def test_orderbook_summary_best_prices_and_totals():
    book = {"yes": [[90, 100], [91, 50]], "no": [[7, 200], [8, 40]]}
    s = money.orderbook_summary(book)
    assert s["bid"] == 91                      # best resting YES bid
    assert s["ask"] == 92                      # 100 - best NO bid (8)
    assert s["yes_total"] == 150.0
    assert s["no_total"] == 240.0


def test_orderbook_summary_decay_weights_depth_near_the_touch():
    # 50 at the touch + 100 one decay-width (3c) away -> 50 + 100/2 = 100.
    book = {"yes": [[91, 50], [88, 100]], "no": []}
    s = money.orderbook_summary(book, decay_cents=3.0)
    assert s["bid_eff"] == 100.0
    assert s["ask"] is None and s["ask_eff"] == 0.0


def test_orderbook_summary_empty():
    s = money.orderbook_summary({})
    assert s["bid"] is None and s["ask"] is None
    assert s["yes_total"] == 0.0 and s["no_total"] == 0.0


def test_position_contracts_signed():
    assert money.position_contracts({"position_fp": "10.00"}) == 10.0
    assert money.position_contracts({"position": -5}) == -5.0


def test_balance_cents():
    assert money.balance_cents({"balance": 30000}) == 30000
    assert money.balance_cents({"balance_dollars": "300.00"}) == 30000
    assert money.balance_cents({}) == 0
