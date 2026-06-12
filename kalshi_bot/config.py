"""Environment-driven configuration with safe defaults.

A fresh checkout runs against the **demo** exchange in **paper** (dry-run)
mode and cannot spend real money. Live trading requires three explicit
opt-ins: ``KALSHI_ENV=prod``, ``DRY_RUN=false`` and
``LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISKS``.

Every knob has a sensible default so the only *required* user input for a
live deployment is API credentials.  Prices in env vars are denominated in
cents for readability; internally everything is integer micro-dollars.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .money import cents

try:  # optional convenience: load a local .env if python-dotenv is present
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


PROD_API_BASE = "https://external-api.kalshi.com/trade-api/v2"
DEMO_API_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"

LIVE_ACK_PHRASE = "I_UNDERSTAND_THE_RISKS"


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _get_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw.strip() if raw and raw.strip() else default


def _get_micro_from_cents(name: str, default_cents: float) -> int:
    return cents(_get_float(name, default_cents))


@dataclass
class Config:
    # --- environment / auth -------------------------------------------------
    env: str = "demo"
    api_base: str = DEMO_API_BASE
    api_key_id: Optional[str] = None
    private_key_path: Optional[str] = None
    private_key_pem: Optional[str] = None

    # --- safety --------------------------------------------------------------
    dry_run: bool = True                     # paper trading (no real orders)
    live_ack: str = ""                       # must equal LIVE_ACK_PHRASE to go live
    paper_cash: int = 1_000 * 1_000_000      # $1,000 starting paper bankroll

    # --- persistence / logging ------------------------------------------------
    state_db_path: str = "state/kalshi_bot.sqlite3"
    log_level: str = "INFO"

    # --- timing ----------------------------------------------------------------
    poll_seconds: float = 10.0               # main loop cadence
    universe_refresh_seconds: float = 120.0  # open-events cache refresh
    universe_pages_per_refresh: int = 5      # events pages (200 ea) per refresh
    longshot_interval_seconds: float = 600.0
    snapshot_interval_seconds: float = 60.0
    fills_lookback_seconds: int = 24 * 3600  # reconcile window on startup

    # --- API budget -------------------------------------------------------------
    read_rps: float = 8.0
    write_rps: float = 4.0
    request_timeout: float = 10.0

    # --- fee model (basis points of C * P * (1-P)) -------------------------------
    taker_fee_bps: int = 700
    maker_fee_bps: int = 175

    # --- portfolio risk -----------------------------------------------------------
    max_total_exposure_frac: float = 0.80    # of equity, all positions marked
    max_market_exposure_frac: float = 0.05
    max_event_exposure_frac: float = 0.08
    max_series_exposure_frac: float = 0.20
    daily_loss_halt_frac: float = 0.05       # halt entries for the day
    max_drawdown_halt_frac: float = 0.15     # halt until manual `resume`
    kelly_fraction: float = 0.25             # quarter-Kelly sizing
    min_entry_price: int = cents(2)          # never buy below this
    max_entry_price: int = cents(98)         # never buy above this
    max_order_contracts: int = 10_000

    # --- strategy enables + equity budget fractions ----------------------------------
    arb_enabled: bool = True
    arb_budget_frac: float = 0.30
    longshot_enabled: bool = True
    longshot_budget_frac: float = 0.40
    mm_enabled: bool = True
    mm_budget_frac: float = 0.20

    # --- arbitrage ---------------------------------------------------------------------
    arb_min_set_profit: int = cents(0.6)     # net of fees, per 1-contract set
    arb_min_total_profit: int = cents(25)    # skip dust opportunities
    arb_depth_shave: float = 0.8             # take this fraction of visible depth
    arb_max_legs: int = 30
    arb_max_cost_frac: float = 0.5           # of arb budget, per opportunity
    arb_exhaustive_bid_floor: float = 0.97   # sum-of-bids proxy for exhaustiveness
    arb_repair_slippage: int = cents(1)

    # --- longshot / favorite harvesting ---------------------------------------------------
    longshot_min_price: int = cents(90)
    longshot_max_price: int = cents(97)
    longshot_edge: int = cents(1.0)          # assumed mispricing of the favorite
    longshot_min_ev: int = cents(0.20)       # required EV/contract after fees
    longshot_max_days_to_close: float = 14.0
    longshot_min_hours_to_close: float = 1.0
    longshot_min_volume_24h: int = 100
    longshot_per_market_frac: float = 0.02   # of equity

    # --- market making -------------------------------------------------------------------
    mm_top_n: int = 8
    mm_min_volume_24h: int = 2_000
    mm_min_spread: int = cents(3)
    mm_band_low: int = cents(12)
    mm_band_high: int = cents(88)
    mm_min_hours_to_close: float = 4.0
    mm_quote_frac: float = 0.25              # of per-market inventory cap, per quote
    mm_skew_ticks: int = 2                   # quote shift at full inventory
    mm_requote_ticks: int = 1                # tolerate drift up to this before requoting
    mm_post_only: bool = True

    cancel_orders_on_exit: bool = True

    # ------------------------------------------------------------------------
    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id and (self.private_key_path or self.private_key_pem))

    @property
    def live_trading(self) -> bool:
        """True only when real prod orders are both requested and acknowledged."""
        return (not self.dry_run) and self.env == "prod"

    def validate(self) -> None:
        if self.env not in ("demo", "prod"):
            raise ValueError(f"KALSHI_ENV must be 'demo' or 'prod', got {self.env!r}")
        if not self.dry_run and not self.has_credentials:
            raise ValueError("DRY_RUN=false requires KALSHI_API_KEY_ID and a private key")
        if self.live_trading and self.live_ack != LIVE_ACK_PHRASE:
            raise ValueError(
                "Refusing to trade real money: set LIVE_TRADING_ACK="
                f"{LIVE_ACK_PHRASE} to confirm you accept the risk of loss"
            )
        budgets = self.arb_budget_frac + self.longshot_budget_frac + self.mm_budget_frac
        if budgets > 1.0 + 1e-9:
            raise ValueError(f"strategy budget fractions sum to {budgets:.2f} > 1.0")
        if not 0 < self.max_total_exposure_frac <= 1.0:
            raise ValueError("MAX_TOTAL_EXPOSURE_FRAC must be in (0, 1]")
        if self.longshot_min_price >= self.longshot_max_price:
            raise ValueError("LONGSHOT_MIN_PRICE_CENTS must be < LONGSHOT_MAX_PRICE_CENTS")

    @classmethod
    def from_env(cls) -> "Config":
        env = _get_str("KALSHI_ENV", "demo").lower()
        default_base = PROD_API_BASE if env == "prod" else DEMO_API_BASE
        return cls(
            env=env,
            api_base=_get_str("KALSHI_API_BASE", default_base),
            api_key_id=os.getenv("KALSHI_API_KEY_ID") or None,
            private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH") or None,
            private_key_pem=os.getenv("KALSHI_PRIVATE_KEY") or None,
            dry_run=_get_bool("DRY_RUN", True),
            live_ack=_get_str("LIVE_TRADING_ACK", ""),
            paper_cash=_get_micro_from_cents("PAPER_CASH_CENTS", 100_000),
            state_db_path=_get_str("STATE_DB_PATH", "state/kalshi_bot.sqlite3"),
            log_level=_get_str("LOG_LEVEL", "INFO").upper(),
            poll_seconds=_get_float("POLL_SECONDS", 10.0),
            universe_refresh_seconds=_get_float("UNIVERSE_REFRESH_SECONDS", 120.0),
            universe_pages_per_refresh=_get_int("UNIVERSE_PAGES_PER_REFRESH", 5),
            longshot_interval_seconds=_get_float("LONGSHOT_INTERVAL_SECONDS", 600.0),
            snapshot_interval_seconds=_get_float("SNAPSHOT_INTERVAL_SECONDS", 60.0),
            fills_lookback_seconds=_get_int("FILLS_LOOKBACK_SECONDS", 24 * 3600),
            read_rps=_get_float("READ_RPS", 8.0),
            write_rps=_get_float("WRITE_RPS", 4.0),
            request_timeout=_get_float("REQUEST_TIMEOUT", 10.0),
            taker_fee_bps=_get_int("TAKER_FEE_BPS", 700),
            maker_fee_bps=_get_int("MAKER_FEE_BPS", 175),
            max_total_exposure_frac=_get_float("MAX_TOTAL_EXPOSURE_FRAC", 0.80),
            max_market_exposure_frac=_get_float("MAX_MARKET_EXPOSURE_FRAC", 0.05),
            max_event_exposure_frac=_get_float("MAX_EVENT_EXPOSURE_FRAC", 0.08),
            max_series_exposure_frac=_get_float("MAX_SERIES_EXPOSURE_FRAC", 0.20),
            daily_loss_halt_frac=_get_float("DAILY_LOSS_HALT_FRAC", 0.05),
            max_drawdown_halt_frac=_get_float("MAX_DRAWDOWN_HALT_FRAC", 0.15),
            kelly_fraction=_get_float("KELLY_FRACTION", 0.25),
            min_entry_price=_get_micro_from_cents("MIN_ENTRY_PRICE_CENTS", 2),
            max_entry_price=_get_micro_from_cents("MAX_ENTRY_PRICE_CENTS", 98),
            max_order_contracts=_get_int("MAX_ORDER_CONTRACTS", 10_000),
            arb_enabled=_get_bool("STRAT_ARB_ENABLED", True),
            arb_budget_frac=_get_float("ARB_BUDGET_FRAC", 0.30),
            longshot_enabled=_get_bool("STRAT_LONGSHOT_ENABLED", True),
            longshot_budget_frac=_get_float("LONGSHOT_BUDGET_FRAC", 0.40),
            mm_enabled=_get_bool("STRAT_MM_ENABLED", True),
            mm_budget_frac=_get_float("MM_BUDGET_FRAC", 0.20),
            arb_min_set_profit=_get_micro_from_cents("ARB_MIN_SET_PROFIT_CENTS", 0.6),
            arb_min_total_profit=_get_micro_from_cents("ARB_MIN_TOTAL_PROFIT_CENTS", 25),
            arb_depth_shave=_get_float("ARB_DEPTH_SHAVE", 0.8),
            arb_max_legs=_get_int("ARB_MAX_LEGS", 30),
            arb_max_cost_frac=_get_float("ARB_MAX_COST_FRAC", 0.5),
            arb_exhaustive_bid_floor=_get_float("ARB_EXHAUSTIVE_BID_FLOOR", 0.97),
            arb_repair_slippage=_get_micro_from_cents("ARB_REPAIR_SLIPPAGE_CENTS", 1),
            longshot_min_price=_get_micro_from_cents("LONGSHOT_MIN_PRICE_CENTS", 90),
            longshot_max_price=_get_micro_from_cents("LONGSHOT_MAX_PRICE_CENTS", 97),
            longshot_edge=_get_micro_from_cents("LONGSHOT_EDGE_CENTS", 1.0),
            longshot_min_ev=_get_micro_from_cents("LONGSHOT_MIN_EV_CENTS", 0.20),
            longshot_max_days_to_close=_get_float("LONGSHOT_MAX_DAYS_TO_CLOSE", 14.0),
            longshot_min_hours_to_close=_get_float("LONGSHOT_MIN_HOURS_TO_CLOSE", 1.0),
            longshot_min_volume_24h=_get_int("LONGSHOT_MIN_VOLUME_24H", 100),
            longshot_per_market_frac=_get_float("LONGSHOT_PER_MARKET_FRAC", 0.02),
            mm_top_n=_get_int("MM_TOP_N", 8),
            mm_min_volume_24h=_get_int("MM_MIN_VOLUME_24H", 2_000),
            mm_min_spread=_get_micro_from_cents("MM_MIN_SPREAD_CENTS", 3),
            mm_band_low=_get_micro_from_cents("MM_BAND_LOW_CENTS", 12),
            mm_band_high=_get_micro_from_cents("MM_BAND_HIGH_CENTS", 88),
            mm_min_hours_to_close=_get_float("MM_MIN_HOURS_TO_CLOSE", 4.0),
            mm_quote_frac=_get_float("MM_QUOTE_FRAC", 0.25),
            mm_skew_ticks=_get_int("MM_SKEW_TICKS", 2),
            mm_requote_ticks=_get_int("MM_REQUOTE_TICKS", 1),
            mm_post_only=_get_bool("MM_POST_ONLY", True),
            cancel_orders_on_exit=_get_bool("CANCEL_ORDERS_ON_EXIT", True),
        )
