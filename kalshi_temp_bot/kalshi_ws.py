"""Realtime market-data over Kalshi's WebSocket ``ticker`` channel.

This is an *optional accelerator*: it keeps an in-memory cache of the freshest
bid/ask/volume per market so the decision loop can react faster than the REST
scan interval.  If the ``websockets`` package is missing or the socket drops,
the bot transparently falls back to REST polling -- correctness never depends on
the socket being up.

Runs its own asyncio loop on a daemon thread; the cache is guarded by a lock so
the synchronous bot loop can read it safely.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Callable, Dict, Iterable, Optional
from urllib.parse import urlparse

from . import money
from .kalshi_client import KalshiAuth

try:
    import websockets
except ImportError:  # pragma: no cover - optional dependency
    websockets = None  # type: ignore

logger = logging.getLogger(__name__)


class MarketDataCache:
    """Thread-safe latest-value cache keyed by market ticker (prices in cents)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, dict] = {}

    def update(self, ticker: str, **fields) -> None:
        clean = {k: v for k, v in fields.items() if v is not None}
        if not clean:
            return
        with self._lock:
            entry = self._data.setdefault(ticker, {})
            entry.update(clean)
            entry["_ts"] = time.time()

    def get(self, ticker: str) -> dict:
        with self._lock:
            return dict(self._data.get(ticker, {}))


class KalshiWebSocket:
    def __init__(
        self,
        ws_base: str,
        auth: KalshiAuth,
        cache: MarketDataCache,
        tickers_provider: Callable[[], Iterable[str]],
        reconnect_delay: float = 3.0,
    ) -> None:
        self.ws_base = ws_base
        self.auth = auth
        self.cache = cache
        self.tickers_provider = tickers_provider
        self.reconnect_delay = reconnect_delay
        self._sign_path = urlparse(ws_base).path
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._msg_id = 0

    def start(self) -> None:
        if websockets is None:
            logger.warning("`websockets` not installed -- realtime updates disabled (REST only)")
            return
        if self.auth is None:
            logger.warning("No credentials -- WebSocket disabled (using public REST polling)")
            return
        self._thread = threading.Thread(target=self._run, name="kalshi-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- internals ---------------------------------------------------------
    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("WebSocket thread exited: %s", exc)

    async def _main(self) -> None:
        while not self._stop.is_set():
            try:
                headers = self.auth.headers("GET", self._sign_path)
                conn = self._connect(headers)
                async with conn as ws:
                    await self._subscribe(ws)
                    await self._consume(ws)
            except Exception as exc:
                if self._stop.is_set():
                    break
                logger.warning("WebSocket error (%s); reconnecting in %.0fs", exc, self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)

    def _connect(self, headers: dict):
        kwargs = dict(ping_interval=10, ping_timeout=10, close_timeout=5)
        # `additional_headers` (websockets >= 12) vs `extra_headers` (older).
        try:
            return websockets.connect(self.ws_base, additional_headers=headers, **kwargs)
        except TypeError:
            return websockets.connect(self.ws_base, extra_headers=headers, **kwargs)

    async def _subscribe(self, ws) -> None:
        tickers = list(self.tickers_provider())
        if not tickers:
            return
        self._msg_id += 1
        message = {
            "id": self._msg_id,
            "cmd": "subscribe",
            "params": {"channels": ["ticker"], "market_tickers": tickers},
        }
        await ws.send(json.dumps(message))
        logger.info("WebSocket subscribed to ticker channel for %d markets", len(tickers))

    async def _consume(self, ws) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            try:
                message = json.loads(raw)
            except (ValueError, TypeError):
                continue
            self._handle(message)

    def _handle(self, message: dict) -> None:
        if message.get("type") != "ticker":
            return
        msg = message.get("msg", {})
        ticker = msg.get("market_ticker") or msg.get("ticker")
        if not ticker:
            return
        self.cache.update(
            ticker,
            yes_bid=money.market_price_cents(msg, "yes_bid"),
            yes_ask=money.market_price_cents(msg, "yes_ask"),
            last_price=money.market_price_cents(msg, "price"),
            volume=money.market_volume(msg),
        )
