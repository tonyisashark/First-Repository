"""Configuration for the Kalshi daily-temperature trading bot.

All knobs are read from environment variables (optionally via a local ``.env``
file).  Sensible, *safe* defaults are used so that a fresh checkout runs in
demo + dry-run mode and never risks real money until the operator explicitly
opts in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

try:  # optional dependency; the bot still runs if python-dotenv is absent
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a convenience only
    pass


# --- API hosts -------------------------------------------------------------
# "external-api" hosts are Kalshi's recommended endpoints for API traders.
PROD_API_BASE = "https://external-api.kalshi.com/trade-api/v2"
PROD_WS_BASE = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
DEMO_API_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"
DEMO_WS_BASE = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"


# Best-effort defaults for Kalshi daily *high* temperature series (one event per
# day per city; each event holds many temperature-range "bucket" markets).
# Series tickers do drift over time -- verify with `python -m kalshi_temp_bot
# list-markets --series KXHIGHNY` and override via the TEMPERATURE_SERIES env var.
DEFAULT_TEMPERATURE_SERIES = [
    "KXHIGHNY",   # New York
    "KXHIGHCHI",  # Chicago
    "KXHIGHMIA",  # Miami
    "KXHIGHAUS",  # Austin
    "KXHIGHLAX",  # Los Angeles
    "KXHIGHDEN",  # Denver
    "KXHIGHPHIL",  # Philadelphia
]


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


@dataclass
class Config:
    # --- environment / auth ---
    env: str = "demo"  # "demo" or "prod"
    api_key_id: Optional[str] = None
    private_key_path: Optional[str] = None
    private_key_pem: Optional[str] = None
    api_base: str = DEMO_API_BASE
    ws_base: str = DEMO_WS_BASE

    # --- safety ---
    # When True the bot reads live market data but never sends real orders;
    # it "paper trades" so you can watch the strategy operate risk-free.
    dry_run: bool = True
    paper_balance_cents: int = 1_000_00  # used for sizing when paper trading w/o creds

    # --- which markets ---
    temperature_series: List[str] = field(default_factory=lambda: list(DEFAULT_TEMPERATURE_SERIES))

    # --- strategy parameters ---
    buy_yes_price_cents: int = 90          # buy YES only when the ask is exactly this
    sell_yes_price_cents: int = 99         # resting sell target
    volume_threshold_ratio: float = 2.0 / 3.0   # >= 2/3 of the max-volume market
    portfolio_fraction: float = 1.0 / 3.0       # deploy 1/3 of the portfolio per trade
    # "global" -> max volume is the single highest-volume market across all monitored [default]
    # "event"  -> max volume is computed within each event (one city/day)
    max_volume_scope: str = "global"

    # --- timing ---
    poll_interval_seconds: float = 1.0     # how often the decision loop runs
    scan_interval_seconds: float = 5.0     # how often the market universe is re-fetched
    use_websocket: bool = True             # realtime price updates between REST scans
    force_sell_buffer_seconds: int = 60    # force-exit this long before market close
    min_seconds_to_close: int = 300        # don't open a trade in a market closing this soon
    buy_timeout_seconds: int = 30          # cancel an unfilled entry order after this long

    # --- order routing ---
    order_api: str = "v2"  # "v2" -> /portfolio/events/orders ; "legacy" -> /portfolio/orders

    request_timeout: float = 10.0

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id and (self.private_key_path or self.private_key_pem))

    @classmethod
    def from_env(cls) -> "Config":
        env = _get_str("KALSHI_ENV", "demo").lower()
        if env == "prod":
            default_api, default_ws = PROD_API_BASE, PROD_WS_BASE
        else:
            default_api, default_ws = DEMO_API_BASE, DEMO_WS_BASE

        series_raw = os.getenv("TEMPERATURE_SERIES", "").strip()
        series = (
            [s.strip() for s in series_raw.split(",") if s.strip()]
            if series_raw
            else list(DEFAULT_TEMPERATURE_SERIES)
        )

        return cls(
            env=env,
            api_key_id=os.getenv("KALSHI_API_KEY_ID") or None,
            private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH") or None,
            private_key_pem=os.getenv("KALSHI_PRIVATE_KEY") or None,
            api_base=_get_str("KALSHI_API_BASE", default_api),
            ws_base=_get_str("KALSHI_WS_BASE", default_ws),
            dry_run=_get_bool("DRY_RUN", True),
            paper_balance_cents=_get_int("PAPER_BALANCE_CENTS", 1_000_00),
            temperature_series=series,
            buy_yes_price_cents=_get_int("BUY_YES_PRICE_CENTS", 90),
            sell_yes_price_cents=_get_int("SELL_YES_PRICE_CENTS", 99),
            volume_threshold_ratio=_get_float("VOLUME_THRESHOLD_RATIO", 2.0 / 3.0),
            portfolio_fraction=_get_float("PORTFOLIO_FRACTION", 1.0 / 3.0),
            max_volume_scope=_get_str("MAX_VOLUME_SCOPE", "global").lower(),
            poll_interval_seconds=_get_float("POLL_INTERVAL_SECONDS", 1.0),
            scan_interval_seconds=_get_float("SCAN_INTERVAL_SECONDS", 5.0),
            use_websocket=_get_bool("USE_WEBSOCKET", True),
            force_sell_buffer_seconds=_get_int("FORCE_SELL_BUFFER_SECONDS", 60),
            min_seconds_to_close=_get_int("MIN_SECONDS_TO_CLOSE", 300),
            buy_timeout_seconds=_get_int("BUY_TIMEOUT_SECONDS", 30),
            order_api=_get_str("KALSHI_ORDER_API", "v2").lower(),
            request_timeout=_get_float("REQUEST_TIMEOUT", 10.0),
        )
