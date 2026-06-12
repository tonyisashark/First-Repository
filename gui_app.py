"""PyInstaller entry point for the desktop app (KalshiBot.exe)."""

import sys

from kalshi_bot.gui import main

if __name__ == "__main__":
    sys.exit(main())
