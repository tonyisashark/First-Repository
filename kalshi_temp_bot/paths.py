"""Per-user paths for configuration.

When the app is installed under Program Files it can't write next to its
executable, so settings (and the ``.env`` the GUI saves) live in a per-user
config directory: ``%APPDATA%\\KalshiTempBot`` on Windows, ``~/.config/
kalshi_temp_bot`` elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME_WIN = "KalshiTempBot"
APP_DIR_NAME_NIX = "kalshi_temp_bot"


def user_config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / APP_DIR_NAME_WIN
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / APP_DIR_NAME_NIX


def env_file_path() -> Path:
    return user_config_dir() / ".env"


def ensure_config_dir() -> Path:
    directory = user_config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory
