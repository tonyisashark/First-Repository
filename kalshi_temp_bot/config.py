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

    load_dotenv()  # a .env in the working directory (handy for development)
    try:
        from .paths import env_file_path

        _user_env = env_file_path()
        if _user_env.exists():
            # The installed app / GUI persists settings here; let it win.
            load_dotenv(_user_env, override=True)
    except Exception:  # pragma: no cover - defensive
        pass
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
    # Buy when the *estimated* chance falls inside this band (cents; 1c = 1%).
    # The estimate is an EWMA-smoothed, depth-weighted book midpoint
    # (microprice), renormalized across the event's buckets -- a stabler read of
    # "what the market believes" than the displayed last-trade chance.
    buy_chance_min_cents: int = 90
    buy_chance_max_cents: int = 95
    # Reject markets whose bid/ask spread exceeds this -- a wide book carries no
    # real probability information.
    max_spread_cents: int = 5
    # EWMA half-life for smoothing the microprice (seconds).
    chance_smoothing_seconds: float = 30.0
    # Exit on liquidity, not price: force-sell while the book still has enough
    # bid depth to fill the position. Triggers when the total resting YES-bid
    # quantity falls to/below ``liquidity_exit_buffer x position size``.
    liquidity_exit_buffer: float = 2.0
    liquidity_poll_seconds: float = 5.0    # how often to poll the order book per position
    # Stop-loss: if the YES bid falls to or below this, sell the position. 0 = off.
    # Never fires when there is no bid at all (nothing to sell into).
    min_sell_price_cents: int = 0
    # Maximum number of concurrent positions. A position whose market has no exit
    # liquidity (no YES bid) does NOT count against this cap.
    max_positions: int = 1
    volume_threshold_ratio: float = 2.0 / 3.0   # >= 2/3 of the max-volume market
    portfolio_fraction: float = 1.0 / 3.0       # deploy 1/3 of the portfolio per trade
    # "global" -> max volume is the single highest-volume market across all monitored [default]
    # "event"  -> max volume is computed within each event (one city/day)
    max_volume_scope: str = "global"

    # --- timing ---
    poll_interval_seconds: float = 1.0     # how often the decision loop runs
    scan_interval_seconds: float = 5.0     # how often the market universe is re-fetched
    use_websocket: bool = True             # realtime price updates between REST scans
    min_seconds_to_close: int = 300        # don't open a trade in a market closing this soon
    buy_timeout_seconds: int = 30          # cancel an unfilled entry order after this long
    heartbeat_interval_seconds: float = 30.0  # how often to log an "I'm alive" status line

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
            # BUY_CHANCE_CENTS / BUY_YES_PRICE_CENTS are legacy single-value
            # names; when present they seed both ends of the band.
            buy_chance_min_cents=_get_int(
                "BUY_CHANCE_MIN_CENTS",
                _get_int("BUY_CHANCE_CENTS", _get_int("BUY_YES_PRICE_CENTS", 90)),
            ),
            buy_chance_max_cents=_get_int(
                "BUY_CHANCE_MAX_CENTS",
                _get_int("BUY_CHANCE_CENTS", _get_int("BUY_YES_PRICE_CENTS", 95)),
            ),
            max_spread_cents=_get_int("MAX_SPREAD_CENTS", 5),
            chance_smoothing_seconds=_get_float("CHANCE_SMOOTHING_SECONDS", 30.0),
            liquidity_exit_buffer=_get_float("LIQUIDITY_EXIT_BUFFER", 2.0),
            liquidity_poll_seconds=_get_float("LIQUIDITY_POLL_SECONDS", 5.0),
            min_sell_price_cents=_get_int("MIN_SELL_PRICE_CENTS", 0),
            max_positions=_get_int("MAX_POSITIONS", 1),
            volume_threshold_ratio=_get_float("VOLUME_THRESHOLD_RATIO", 2.0 / 3.0),
            portfolio_fraction=_get_float("PORTFOLIO_FRACTION", 1.0 / 3.0),
            max_volume_scope=_get_str("MAX_VOLUME_SCOPE", "global").lower(),
            poll_interval_seconds=_get_float("POLL_INTERVAL_SECONDS", 1.0),
            scan_interval_seconds=_get_float("SCAN_INTERVAL_SECONDS", 5.0),
            use_websocket=_get_bool("USE_WEBSOCKET", True),
            min_seconds_to_close=_get_int("MIN_SECONDS_TO_CLOSE", 300),
            buy_timeout_seconds=_get_int("BUY_TIMEOUT_SECONDS", 30),
            heartbeat_interval_seconds=_get_float("HEARTBEAT_INTERVAL_SECONDS", 30.0),
            order_api=_get_str("KALSHI_ORDER_API", "v2").lower(),
            request_timeout=_get_float("REQUEST_TIMEOUT", 10.0),
        )


def save_settings(env_values: dict) -> str:
    """Persist GUI settings as a ``.env`` in the per-user config directory.

    Returns the path written. Values are written verbatim as ``KEY=VALUE`` lines.
    """
    from .paths import ensure_config_dir, env_file_path

    ensure_config_dir()
    path = env_file_path()
    lines = ["# Saved by the Kalshi Temperature Bot GUI -- edit with care.", ""]
    for key, value in env_values.items():
        lines.append(f"{key}={'' if value is None else value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)
