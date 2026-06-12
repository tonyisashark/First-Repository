"""Construction helpers shared by the CLI and the GUI.

Builds the auth signer, REST client and a fully wired :class:`TradingBot`
(including the realtime WebSocket feed when credentials are available) from a
:class:`Config`.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from .bot import TradingBot
from .config import Config
from .kalshi_client import KalshiAuth, KalshiClient
from .kalshi_ws import KalshiWebSocket, MarketDataCache

logger = logging.getLogger(__name__)


def build_auth(cfg: Config) -> Optional[KalshiAuth]:
    if not cfg.has_credentials:
        return None
    try:
        return KalshiAuth.load(cfg.api_key_id, cfg.private_key_path, cfg.private_key_pem)
    except Exception as exc:  # noqa: BLE001 - surfaced to caller via None
        logger.error("Failed to load API credentials: %s", exc)
        return None


def build_client(cfg: Config, auth: Optional[KalshiAuth]) -> KalshiClient:
    return KalshiClient(
        api_base=cfg.api_base,
        auth=auth,
        timeout=cfg.request_timeout,
        order_api=cfg.order_api,
    )


def build_bot(cfg: Config) -> Tuple[TradingBot, Optional[KalshiAuth]]:
    auth = build_auth(cfg)
    client = build_client(cfg, auth)
    bot = TradingBot(client=client, config=cfg, cache=None, ws=None)

    if auth is not None:
        cache = MarketDataCache()
        ws = KalshiWebSocket(
            ws_base=cfg.ws_base,
            auth=auth,
            cache=cache,
            tickers_provider=bot.current_tickers,
        )
        bot.cache = cache
        bot.ws = ws
    return bot, auth
