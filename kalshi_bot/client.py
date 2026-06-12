"""Rate-limited, retrying Kalshi REST client (trade-api/v2).

The client is deliberately dumb: it signs, throttles, retries, paginates and
returns raw dicts. Typed parsing lives in :mod:`kalshi_bot.models` so this
layer stays trivial to fake in tests.

Verified against docs.kalshi.com (June 2026):
- reads:  /exchange/status, /markets, /markets/{t}, /markets/orderbooks,
          /markets/{t}/orderbook, /events, /events/{t}, /portfolio/balance,
          /portfolio/positions, /portfolio/orders, /portfolio/fills
- writes: POST /portfolio/events/orders            (side: bid|ask, price: dollars)
          POST /portfolio/events/orders/batched
          DELETE /portfolio/events/orders/{order_id}
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import requests

from .auth import KalshiSigner
from .money import count_to_fp, micro_to_usd_str

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class KalshiAPIError(RuntimeError):
    def __init__(self, status: int, message: str, payload: Optional[dict] = None):
        super().__init__(f"Kalshi API {status}: {message}")
        self.status = status
        self.message = message
        self.payload = payload or {}


class TokenBucket:
    """Simple blocking token bucket (thread-unsafe; the bot is single-threaded)."""

    def __init__(
        self,
        rate_per_s: float,
        capacity: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = max(rate_per_s, 0.1)
        self.capacity = capacity if capacity is not None else max(1.0, self.rate)
        self.tokens = self.capacity
        self._clock = clock
        self._sleep = sleeper
        self._last = clock()

    def acquire(self, n: float = 1.0) -> None:
        while True:
            now = self._clock()
            self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
            self._last = now
            if self.tokens >= n:
                self.tokens -= n
                return
            self._sleep((n - self.tokens) / self.rate)


class KalshiClient:
    def __init__(
        self,
        api_base: str,
        signer: Optional[KalshiSigner] = None,
        session: Optional[Any] = None,
        read_rps: float = 8.0,
        write_rps: float = 4.0,
        timeout: float = 10.0,
        max_attempts: int = 5,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.signer = signer
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._sleep = sleeper
        self._read_bucket = TokenBucket(read_rps, clock=clock, sleeper=sleeper)
        self._write_bucket = TokenBucket(write_rps, clock=clock, sleeper=sleeper)

    # ------------------------------------------------------------------ core
    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        require_auth: bool = False,
    ) -> dict:
        url = self.api_base + endpoint
        is_write = method.upper() in ("POST", "DELETE", "PUT", "PATCH")
        bucket = self._write_bucket if is_write else self._read_bucket

        if require_auth and self.signer is None:
            raise KalshiAPIError(0, f"{endpoint} requires API credentials but none are configured")

        last_error = "unknown error"
        for attempt in range(self.max_attempts):
            bucket.acquire()
            headers = {"Accept": "application/json"}
            if self.signer is not None:
                # Signature covers the path (with /trade-api/v2 prefix), no query.
                headers.update(self.signer.headers(method, urlparse(url).path))
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body,
                    headers=headers, timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                self._backoff(attempt, endpoint, last_error)
                continue

            if resp.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {resp.status_code}"
                self._backoff(attempt, endpoint, last_error, rate_limited=resp.status_code == 429)
                continue
            if resp.status_code >= 400:
                raise KalshiAPIError(resp.status_code, _error_message(resp), _safe_json(resp))
            return _safe_json(resp)

        raise KalshiAPIError(0, f"giving up on {method} {endpoint} after "
                                f"{self.max_attempts} attempts ({last_error})")

    def _backoff(self, attempt: int, endpoint: str, reason: str, rate_limited: bool = False) -> None:
        base = 1.0 if rate_limited else 0.5
        wait = base * (2 ** attempt) + random.uniform(0, 0.25)
        logger.warning("%s on %s; retrying in %.1fs", reason, endpoint, wait)
        self._sleep(wait)

    def _paged(
        self,
        endpoint: str,
        key: str,
        params: Optional[dict] = None,
        limit: int = 200,
        max_pages: int = 50,
    ) -> List[dict]:
        items: List[dict] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            page_params: Dict[str, Any] = dict(params or {})
            page_params["limit"] = limit
            if cursor:
                page_params["cursor"] = cursor
            data = self._request("GET", endpoint, params=page_params)
            batch = data.get(key) or []
            items.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return items

    # ----------------------------------------------------------- market data
    def exchange_status(self) -> dict:
        return self._request("GET", "/exchange/status")

    def get_markets(
        self,
        status: Optional[str] = "open",
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        tickers: Optional[Iterable[str]] = None,
        min_close_ts: Optional[int] = None,
        max_pages: int = 10,
        limit: int = 1000,
    ) -> List[dict]:
        params: Dict[str, Any] = {}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts
        if tickers:
            out: List[dict] = []
            batch: List[str] = []
            for t in tickers:
                batch.append(t)
                if len(batch) == 100:
                    out.extend(self._markets_by_tickers(batch, params))
                    batch = []
            if batch:
                out.extend(self._markets_by_tickers(batch, params))
            return out
        return self._paged("/markets", "markets", params, limit=limit, max_pages=max_pages)

    def _markets_by_tickers(self, batch: List[str], params: dict) -> List[dict]:
        p = dict(params)
        p["tickers"] = ",".join(batch)
        p["limit"] = max(len(batch), 100)
        data = self._request("GET", "/markets", params=p)
        return data.get("markets") or []

    def get_market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}").get("market", {})

    def get_settled_markets(self, min_settled_ts: int, max_pages: int = 10) -> List[dict]:
        params = {"status": "settled", "min_settled_ts": min_settled_ts}
        return self._paged("/markets", "markets", params, limit=1000, max_pages=max_pages)

    def get_orderbook(self, ticker: str, depth: int = 0) -> dict:
        params = {"depth": depth} if depth else None
        return self._request("GET", f"/markets/{ticker}/orderbook", params=params)

    def get_orderbooks(self, tickers: List[str]) -> Dict[str, dict]:
        """Batch orderbooks (<=100 tickers per request) -> {ticker: payload}."""
        books: Dict[str, dict] = {}
        for i in range(0, len(tickers), 100):
            chunk = tickers[i : i + 100]
            data = self._request("GET", "/markets/orderbooks", params={"tickers": chunk})
            for entry in data.get("orderbooks") or []:
                books[entry.get("ticker", "")] = entry
        return books

    def get_events_page(
        self,
        status: str = "open",
        cursor: Optional[str] = None,
        with_nested_markets: bool = True,
        limit: int = 200,
    ) -> tuple[List[dict], Optional[str]]:
        params: Dict[str, Any] = {"status": status, "limit": limit}
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        if cursor:
            params["cursor"] = cursor
        data = self._request("GET", "/events", params=params)
        return data.get("events") or [], (data.get("cursor") or None)

    def get_event(self, event_ticker: str, with_nested_markets: bool = True) -> dict:
        params = {"with_nested_markets": "true"} if with_nested_markets else None
        return self._request("GET", f"/events/{event_ticker}", params=params)

    # ------------------------------------------------------------- portfolio
    def get_balance(self) -> dict:
        return self._request("GET", "/portfolio/balance", require_auth=True)

    def get_positions(self) -> List[dict]:
        items: List[dict] = []
        cursor: Optional[str] = None
        for _ in range(20):
            params: Dict[str, Any] = {"limit": 1000, "count_filter": "position"}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/portfolio/positions", params=params, require_auth=True)
            batch = data.get("market_positions") or []
            items.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return items

    def get_orders(self, status: str = "resting", ticker: Optional[str] = None) -> List[dict]:
        params: Dict[str, Any] = {"status": status}
        if ticker:
            params["ticker"] = ticker
        return self._paged_auth("/portfolio/orders", "orders", params)

    def get_fills(self, min_ts: Optional[int] = None, max_pages: int = 10) -> List[dict]:
        params: Dict[str, Any] = {}
        if min_ts is not None:
            params["min_ts"] = min_ts
        return self._paged_auth("/portfolio/fills", "fills", params, limit=1000, max_pages=max_pages)

    def _paged_auth(self, endpoint: str, key: str, params: dict,
                    limit: int = 200, max_pages: int = 20) -> List[dict]:
        items: List[dict] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            page_params = dict(params)
            page_params["limit"] = limit
            if cursor:
                page_params["cursor"] = cursor
            data = self._request("GET", endpoint, params=page_params, require_auth=True)
            batch = data.get(key) or []
            items.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return items

    # ----------------------------------------------------------------- orders
    @staticmethod
    def order_payload(
        ticker: str,
        side: str,                      # "bid" (buy YES) | "ask" (sell/short YES = buy NO)
        price_micro: int,
        count: int,
        time_in_force: str = "good_till_canceled",
        post_only: bool = False,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> dict:
        if side not in ("bid", "ask"):
            raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")
        payload = {
            "ticker": ticker,
            "side": side,
            "count": count_to_fp(count),
            "price": micro_to_usd_str(price_micro),
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if post_only:
            payload["post_only"] = True
        if reduce_only:
            payload["reduce_only"] = True
        return payload

    def create_order(self, payload: dict) -> dict:
        resp = self._request("POST", "/portfolio/events/orders",
                             json_body=payload, require_auth=True)
        return _normalize_order_response(resp, payload)

    def batch_create_orders(self, payloads: List[dict]) -> List[dict]:
        resp = self._request("POST", "/portfolio/events/orders/batched",
                             json_body={"orders": payloads}, require_auth=True)
        rows = resp.get("orders") or []
        return [_normalize_order_response(row, payloads[i] if i < len(payloads) else {})
                for i, row in enumerate(rows)]

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/events/orders/{order_id}",
                             require_auth=True)


def _normalize_order_response(resp: dict, request_payload: dict) -> dict:
    from .money import fp_to_count, usd_to_micro  # local to avoid cycle at import

    body = resp.get("order", resp) if isinstance(resp, dict) else {}
    error = body.get("error") or resp.get("error")
    return {
        "order_id": body.get("order_id"),
        "client_order_id": body.get("client_order_id") or request_payload.get("client_order_id"),
        "filled": fp_to_count(body.get("fill_count"), 0),
        "remaining": fp_to_count(body.get("remaining_count"), 0),
        "avg_price": usd_to_micro(body.get("average_fill_price")),
        "fee": usd_to_micro(body.get("average_fee_paid")) or 0,
        "error": (error or {}).get("message") if isinstance(error, dict) else error,
        "raw": resp,
    }


def _safe_json(resp) -> dict:
    try:
        return resp.json() if resp.text else {}
    except ValueError:
        return {}


def _error_message(resp) -> str:
    data = _safe_json(resp)
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or resp.text[:200])
    if isinstance(err, str):
        return err
    return (resp.text or "")[:200]
