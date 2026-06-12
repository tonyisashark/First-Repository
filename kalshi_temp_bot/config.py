"""Configuration for the Kalshi daily-temperature trading bot.

Only the knobs an operator genuinely needs are exposed; everything about the
probability model, edge thresholds and timing is self-tuned in
:mod:`kalshi_temp_bot.strategy` / :mod:`kalshi_temp_bot.bot`.

All settings are read from environment variables (optionally via a local
``.env`` file). Sensible, *safe* defaults are used so that a fresh checkout
runs in demo + dry-run mode and never risks real money until the operator
explicitly opts in.
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

    # --- risk appetite ---
    # Bankroll fraction deployed per trade. This is a *ceiling*: each trade is
    # additionally capped at its own Kelly fraction, so a thin edge deploys less.
    portfolio_fraction: float = 1.0 / 3.0
    # Maximum number of concurrent positions. A position whose market has no
    # exit liquidity (no bid on its side) does NOT count against this cap.
    max_positions: int = 1

    # --- plumbing (env-only; rarely needed) ---
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
            portfolio_fraction=_get_float("PORTFOLIO_FRACTION", 1.0 / 3.0),
            max_positions=_get_int("MAX_POSITIONS", 1),
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
