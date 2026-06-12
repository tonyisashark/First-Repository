"""Per-user data locations for the desktop app.

An installed .exe runs from a read-only directory, so settings, state and
logs live in the OS-conventional per-user spot instead of the CWD:

    Windows:  %APPDATA%\\KalshiBot
    macOS:    ~/Library/Application Support/KalshiBot
    Linux:    $XDG_CONFIG_HOME/kalshi-bot  (default ~/.config/kalshi-bot)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "KalshiBot"


def user_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "kalshi-bot"


def ensure_data_dir() -> Path:
    path = user_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return user_data_dir() / "settings.env"


def default_db_path() -> Path:
    return user_data_dir() / "state.sqlite3"


def log_path() -> Path:
    return user_data_dir() / "kalshi-bot.log"
