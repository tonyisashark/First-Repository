"""Settings schema + persistence for the GUI (no tkinter imports here).

The GUI edits a flat dict of env-var strings, persisted as a ``KEY=VALUE``
file in the per-user data dir and applied to ``os.environ`` before the bot
reads :class:`kalshi_bot.config.Config`. Process environment variables that
the user set explicitly always win over the saved file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Field:
    key: str           # env var name
    label: str
    kind: str          # "str" | "path" | "bool" | "choice" | "number"
    default: str = ""
    choices: tuple = ()
    group: str = ""
    help: str = ""


FIELDS: List[Field] = [
    # --- connection ---------------------------------------------------------
    Field("KALSHI_ENV", "Exchange", "choice", "demo", ("demo", "prod"),
          "Connection", "demo = play money exchange, prod = the real one"),
    Field("KALSHI_API_KEY_ID", "API key ID", "str", "", (),
          "Connection", "from kalshi.com -> Account -> API Keys"),
    Field("KALSHI_PRIVATE_KEY_PATH", "Private key file (.pem)", "path", "", (),
          "Connection", "the RSA key downloaded when the API key was created"),
    # --- safety --------------------------------------------------------------
    Field("DRY_RUN", "Paper trading (no real orders)", "bool", "true", (),
          "Safety", "uncheck only after the paper results convince you"),
    Field("LIVE_TRADING_ACK", "Live-trading acknowledgement", "str", "", (),
          "Safety", "type I_UNDERSTAND_THE_RISKS to allow prod + live"),
    Field("PAPER_CASH_CENTS", "Paper bankroll (cents)", "number", "100000", (),
          "Safety", "simulated starting cash"),
    # --- strategies ----------------------------------------------------------
    Field("STRAT_ARB_ENABLED", "Event arbitrage", "bool", "true", (),
          "Strategies", ""),
    Field("ARB_BUDGET_FRAC", "Arb budget (fraction of equity)", "number", "0.30",
          (), "Strategies", ""),
    Field("STRAT_LONGSHOT_ENABLED", "Favorite harvesting", "bool", "true", (),
          "Strategies", ""),
    Field("LONGSHOT_BUDGET_FRAC", "Favorite budget", "number", "0.40", (),
          "Strategies", ""),
    Field("LONGSHOT_EDGE_CENTS", "Assumed favorite edge (cents)", "number", "1.0",
          (), "Strategies", "tune with `kalshi-bot calibrate`"),
    Field("STRAT_MM_ENABLED", "Market making", "bool", "true", (),
          "Strategies", "paper fills are optimistic for this one"),
    Field("MM_BUDGET_FRAC", "Market-making budget", "number", "0.20", (),
          "Strategies", ""),
    # --- risk ------------------------------------------------------------------
    Field("DAILY_LOSS_HALT_FRAC", "Daily loss halt (fraction)", "number", "0.05",
          (), "Risk", "stop entering for the day after this loss"),
    Field("MAX_DRAWDOWN_HALT_FRAC", "Max drawdown halt", "number", "0.15", (),
          "Risk", "stop until Resume after this fall from the peak"),
    Field("KELLY_FRACTION", "Kelly fraction", "number", "0.25", (),
          "Risk", "lower = smaller, smoother positions"),
    Field("MAX_TOTAL_EXPOSURE_FRAC", "Max total exposure", "number", "0.80", (),
          "Risk", "never deploy more than this fraction of equity"),
    # --- advanced ----------------------------------------------------------------
    Field("POLL_SECONDS", "Loop cadence (seconds)", "number", "10", (),
          "Advanced", ""),
    Field("LOG_LEVEL", "Log level", "choice", "INFO",
          ("DEBUG", "INFO", "WARNING"), "Advanced", ""),
    Field("STATE_DB_PATH", "State database", "path", "", (),
          "Advanced", "blank = per-user default"),
]

GROUPS = ["Connection", "Safety", "Strategies", "Risk", "Advanced"]


def defaults() -> Dict[str, str]:
    return {f.key: f.default for f in FIELDS}


def load_settings(path: Path) -> Dict[str, str]:
    """Read a KEY=VALUE file; unknown keys are preserved, comments skipped."""
    values: Dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def save_settings(path: Path, values: Dict[str, str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Saved by the Kalshi Bot GUI. Edit with care.", ""]
    for key in sorted(values):
        value = values[key]
        if value is None or str(value) == "":
            continue
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def initial_values(file_values: Dict[str, str],
                   environ: Optional[dict] = None) -> Dict[str, str]:
    """Form population order: explicit process env > saved file > default."""
    env = os.environ if environ is None else environ
    out: Dict[str, str] = {}
    for field in FIELDS:
        if env.get(field.key):
            out[field.key] = env[field.key]
        elif file_values.get(field.key, "") != "":
            out[field.key] = file_values[field.key]
        else:
            out[field.key] = field.default
    return out


def export_to_environ(values: Dict[str, str],
                      environ: Optional[dict] = None) -> None:
    """The form is authoritative at start time: set every managed key,
    removing empties so config falls back to its own defaults."""
    env = os.environ if environ is None else environ
    for field in FIELDS:
        value = str(values.get(field.key, "") or "")
        if value == "":
            env.pop(field.key, None)
        else:
            env[field.key] = value


def normalize_bool(text: str) -> bool:
    return str(text).strip().lower() in ("1", "true", "yes", "on", "y")
