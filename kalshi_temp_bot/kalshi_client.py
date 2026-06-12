"""Thin Kalshi REST client.

Handles API-key (RSA) request signing and exposes the handful of endpoints the
bot needs: market data (public), balance & positions, and order create/cancel.

Signing scheme (per Kalshi docs):
    message   = f"{timestamp_ms}{HTTP_METHOD}{path}"   # path only, no query, no body
    signature = base64( RSA-PSS-SHA256( private_key, message ) )   # salt = digest len
Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP(ms).
"""

from __future__ import annotations

import base64
import logging
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from . import money

logger = logging.getLogger(__name__)


class KalshiAuth:
    """RSA request signer built from a Kalshi API key id + private key."""

    def __init__(self, api_key_id: str, private_key) -> None:
        self.api_key_id = api_key_id
        self._private_key = private_key

    @classmethod
    def from_pem(cls, api_key_id: str, pem_bytes: bytes, password: Optional[bytes] = None) -> "KalshiAuth":
        key = load_pem_private_key(pem_bytes, password=password)
        return cls(api_key_id, key)

    @classmethod
    def load(
        cls,
        api_key_id: str,
        private_key_path: Optional[str] = None,
        private_key_pem: Optional[str] = None,
    ) -> "KalshiAuth":
        if private_key_path:
            with open(private_key_path, "rb") as fh:
                pem = fh.read()
        elif private_key_pem:
            pem = private_key_pem.encode("utf-8")
        else:
            raise ValueError("A private key (path or PEM contents) is required")
        return cls.from_pem(api_key_id, pem)

    def _sign(self, message: str) -> str:
        signature = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def headers(self, method: str, path: str) -> Dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}"
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(message),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }


class KalshiClient:
    def __init__(
        self,
        api_base: str,
        auth: Optional[KalshiAuth] = None,
        timeout: float = 10.0,
        order_api: str = "v2",
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.auth = auth
        self.timeout = timeout
        self.order_api = order_api
        self.session = requests.Session()

    # -- low level ---------------------------------------------------------
    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        require_auth: bool = False,
    ) -> dict:
        url = self.api_base + endpoint
        # The signature covers the path only (with the /trade-api/v2 prefix),
        # never the query string.
        sign_path = urlparse(url).path
        headers = {"Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if self.auth is not None:
            headers.update(self.auth.headers(method, sign_path))
        elif require_auth:
            raise RuntimeError(f"{endpoint} requires API credentials but none are configured")

        for attempt in range(4):
            resp = self.session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=self.timeout,
            )
            if resp.status_code == 429:  # rate limited - brief backoff and retry
                wait = 0.5 * (2 ** attempt)
                logger.warning("Rate limited on %s; backing off %.1fs", endpoint, wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                logger.error(
                    "Kalshi API %s %s -> %s: %s", method, endpoint, resp.status_code, resp.text[:500]
                )
                resp.raise_for_status()
            return resp.json() if resp.text else {}
        raise RuntimeError(f"Repeatedly rate limited on {endpoint}")

    # -- market data (public) ---------------------------------------------
    def get_markets(self, series_ticker: Optional[str] = None, status: str = "open", limit: int = 200) -> List[dict]:
        markets: List[dict] = []
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"limit": limit, "status": status}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/markets", params=params)
            batch = data.get("markets", []) or []
            markets.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return markets

    def get_market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}").get("market", {})

    def get_orderbook(self, ticker: str) -> dict:
        """Full resting-order book for one market: price levels per side."""
        return self._request("GET", f"/markets/{ticker}/orderbook").get("orderbook", {}) or {}

    # -- portfolio (auth) --------------------------------------------------
    def get_balance(self) -> dict:
        return self._request("GET", "/portfolio/balance", require_auth=True)

    def get_positions(self) -> List[dict]:
        data = self._request("GET", "/portfolio/positions", require_auth=True)
        return data.get("market_positions", []) or []

    def get_position_contracts(self, ticker: str) -> float:
        for pos in self.get_positions():
            if pos.get("ticker") == ticker:
                return money.position_contracts(pos)
        return 0.0

    # -- orders (auth) -----------------------------------------------------
    def create_order(
        self,
        ticker: str,
        is_buy: bool,
        count: int,
        price_cents: Optional[int] = None,
        time_in_force: str = "good_till_canceled",
        market_order: bool = False,
    ) -> dict:
        """Place a YES order. ``is_buy`` True = buy YES, False = sell YES.

        ``market_order`` requests immediate execution at any price: a real
        ``market`` order on the legacy schema, or -- since the v2 schema requires
        a price -- an aggressive immediate-or-cancel limit at the book's edge
        (1c to sell, 99c to buy) which sweeps all resting liquidity.

        Routes to the new ``/portfolio/events/orders`` schema by default, or the
        legacy ``/portfolio/orders`` schema when ``order_api == 'legacy'``.
        """
        if self.order_api == "legacy":
            body = {
                "ticker": ticker,
                "client_order_id": str(uuid.uuid4()),
                "action": "buy" if is_buy else "sell",
                "side": "yes",
                "count": int(count),
            }
            if market_order:
                body["type"] = "market"
            else:
                body["type"] = "limit"
                body["yes_price"] = int(price_cents)
                body["time_in_force"] = time_in_force
            resp = self._request("POST", "/portfolio/orders", json_body=body, require_auth=True)
        else:
            if market_order:
                # v2 has no market type: cross the book with an IOC at the edge.
                price_cents = 99 if is_buy else 1
                time_in_force = "immediate_or_cancel"
            body = {
                "ticker": ticker,
                "client_order_id": str(uuid.uuid4()),
                "side": "bid" if is_buy else "ask",  # bid = buy YES, ask = sell YES
                "count": money.fixed_point_str(count),
                "price": money.cents_to_dollars_str(price_cents),
                "time_in_force": time_in_force,
                "self_trade_prevention_type": "taker_at_cross",
            }
            resp = self._request("POST", "/portfolio/events/orders", json_body=body, require_auth=True)
        return self._normalize_order(resp)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/orders/{order_id}", require_auth=True)

    @staticmethod
    def _normalize_order(resp: dict) -> dict:
        # Legacy nests under "order"; the v2 schema returns fields at top level.
        order = resp.get("order", resp) if isinstance(resp, dict) else {}
        remaining = order.get("remaining_count")
        if remaining is None:
            remaining = order.get("count")
        return {
            "order_id": order.get("order_id"),
            "fill_count": money.to_float(order.get("fill_count") or order.get("filled_count")),
            "remaining_count": money.to_float(remaining),
            "status": order.get("status"),
            "raw": resp,
        }
