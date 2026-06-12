"""Client-level tests that need no network (response-shape handling)."""

from kalshi_temp_bot.kalshi_client import KalshiClient


def make_client(payload):
    client = KalshiClient(api_base="https://example.invalid/trade-api/v2")
    client._request = lambda *a, **k: payload
    return client


def test_get_orderbook_reads_fixed_point_key():
    # The fixed-point migration nests the book under "orderbook_fp"; missing
    # this key made every book parse empty (and disabled the liquidity exit).
    book = {"yes_dollars": [["0.48", "100.00"]], "no_dollars": [["0.51", "50.00"]]}
    assert make_client({"orderbook_fp": book}).get_orderbook("T") == book


def test_get_orderbook_falls_back_to_legacy_key():
    book = {"yes": [[48, 100]], "no": [[51, 50]]}
    assert make_client({"orderbook": book}).get_orderbook("T") == book


def test_get_orderbook_empty_response():
    assert make_client({}).get_orderbook("T") == {}
