import json

import pytest

from kalshi_bot.client import KalshiAPIError, KalshiClient, TokenBucket


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Scripted session: pops the next response, records every request."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params,
                           "json": json, "headers": headers})
        if not self.script:
            raise AssertionError("unexpected extra request")
        return self.script.pop(0)


class StubSigner:
    api_key_id = "kid"

    def __init__(self):
        self.signed_paths = []

    def headers(self, method, path):
        self.signed_paths.append((method, path))
        return {"KALSHI-ACCESS-KEY": "kid", "KALSHI-ACCESS-SIGNATURE": "sig",
                "KALSHI-ACCESS-TIMESTAMP": "0"}


def make_client(script, signer=None):
    session = FakeSession(script)
    client = KalshiClient("https://x.test/trade-api/v2", signer=signer,
                          session=session, read_rps=10_000, write_rps=10_000,
                          sleeper=lambda s: None)
    return client, session


def test_pagination_follows_cursor():
    client, session = make_client([
        FakeResponse(body={"markets": [{"ticker": "A"}], "cursor": "c1"}),
        FakeResponse(body={"markets": [{"ticker": "B"}], "cursor": ""}),
    ])
    markets = client.get_markets()
    assert [m["ticker"] for m in markets] == ["A", "B"]
    assert session.calls[1]["params"]["cursor"] == "c1"


def test_retry_on_429_then_success():
    client, session = make_client([
        FakeResponse(status_code=429),
        FakeResponse(body={"markets": [], "cursor": ""}),
    ])
    assert client.get_markets() == []
    assert len(session.calls) == 2


def test_client_error_raises_with_message():
    client, _ = make_client([
        FakeResponse(status_code=400, body={"error": {"code": "bad",
                                                      "message": "no such market"}}),
    ])
    with pytest.raises(KalshiAPIError) as err:
        client.get_market("NOPE")
    assert err.value.status == 400
    assert "no such market" in str(err.value)


def test_signature_covers_path_without_query():
    signer = StubSigner()
    client, _ = make_client(
        [FakeResponse(body={"orderbooks": []})], signer=signer)
    client.get_orderbooks(["T1", "T2"])
    method, path = signer.signed_paths[0]
    assert method == "GET"
    assert path == "/trade-api/v2/markets/orderbooks"   # no query string


def test_orderbooks_chunking():
    tickers = [f"T{i}" for i in range(150)]
    client, session = make_client([
        FakeResponse(body={"orderbooks": [{"ticker": t} for t in tickers[:100]]}),
        FakeResponse(body={"orderbooks": [{"ticker": t} for t in tickers[100:]]}),
    ])
    books = client.get_orderbooks(tickers)
    assert len(books) == 150
    assert len(session.calls[0]["params"]["tickers"]) == 100
    assert len(session.calls[1]["params"]["tickers"]) == 50


def test_markets_by_tickers_comma_joined():
    client, session = make_client([FakeResponse(body={"markets": []})])
    client.get_markets(status=None, tickers=["A", "B"])
    assert session.calls[0]["params"]["tickers"] == "A,B"


def test_order_payload_wire_format():
    payload = KalshiClient.order_payload("T", "bid", 560_000, 10,
                                         time_in_force="immediate_or_cancel")
    assert payload["price"] == "0.56"
    assert payload["count"] == "10.00"
    assert payload["side"] == "bid"
    assert payload["time_in_force"] == "immediate_or_cancel"
    assert payload["self_trade_prevention_type"] == "taker_at_cross"
    assert "post_only" not in payload and "reduce_only" not in payload
    flagged = KalshiClient.order_payload("T", "ask", 560_000, 1,
                                         post_only=True, reduce_only=True)
    assert flagged["post_only"] is True and flagged["reduce_only"] is True
    with pytest.raises(ValueError):
        KalshiClient.order_payload("T", "yes", 560_000, 1)


def test_create_order_normalization():
    client, session = make_client([FakeResponse(body={
        "order_id": "o1", "fill_count": "3.00", "remaining_count": "7.00",
        "average_fill_price": "0.55",
    })], signer=StubSigner())
    result = client.create_order(KalshiClient.order_payload("T", "bid", 550_000, 10))
    assert result["order_id"] == "o1"
    assert result["filled"] == 3 and result["remaining"] == 7
    assert result["avg_price"] == 550_000
    assert session.calls[0]["url"].endswith("/portfolio/events/orders")


def test_batch_orders_partial_failure():
    client, _ = make_client([FakeResponse(body={"orders": [
        {"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00"},
        {"error": {"message": "insufficient balance"}},
    ]})], signer=StubSigner())
    payloads = [KalshiClient.order_payload("A", "bid", 500_000, 1),
                KalshiClient.order_payload("B", "bid", 500_000, 1)]
    results = client.batch_create_orders(payloads)
    assert results[0]["filled"] == 1 and results[0]["error"] is None
    assert results[1]["error"] == "insufficient balance"


def test_auth_required_without_signer():
    client, _ = make_client([])
    with pytest.raises(KalshiAPIError):
        client.get_balance()


def test_token_bucket_throttles():
    clock = {"t": 0.0}
    sleeps = []

    def fake_clock():
        return clock["t"]

    def fake_sleep(s):
        sleeps.append(s)
        clock["t"] += s

    bucket = TokenBucket(2.0, clock=fake_clock, sleeper=fake_sleep)
    bucket.acquire()  # uses burst capacity
    bucket.acquire()
    bucket.acquire()  # now must wait ~0.5s
    assert sleeps and abs(sum(sleeps) - 0.5) < 1e-6
