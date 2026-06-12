"""Command-line entry point for the Kalshi daily-temperature trading bot.

Usage:
    python -m kalshi_temp_bot run                 # run the trading loop
    python -m kalshi_temp_bot list-markets        # inspect markets / candidates (public)
    python -m kalshi_temp_bot balance             # show account balance (needs creds)
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from typing import List, Optional

from . import money
from .bot import TradingBot
from .config import Config
from .factory import build_auth, build_bot, build_client
from .strategy import (
    MIN_EDGE_CENTS,
    evaluate_side,
    informative,
    mid_price_cents,
    renormalized_estimates,
)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_run(cfg: Config) -> int:
    log = logging.getLogger(__name__)
    bot, auth = build_bot(cfg)

    if not cfg.dry_run and auth is None:
        log.error("Live trading requested (DRY_RUN=false) but no valid API credentials found. Aborting.")
        return 2
    if not cfg.dry_run and cfg.env == "prod":
        log.warning("LIVE TRADING ON PRODUCTION -- real money is at risk.")

    def _handle_signal(signum, _frame):
        log.info("Received signal %s -- shutting down", signum)
        bot.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    bot.run()
    return 0


def cmd_list_markets(cfg: Config, series_override: Optional[List[str]]) -> int:
    """Public, read-only view of monitored markets and which ones are buy candidates."""
    auth = build_auth(cfg)
    client = build_client(cfg, auth)
    series_list = series_override or cfg.temperature_series

    views = []
    for series in series_list:
        try:
            raw = client.get_markets(series_ticker=series, status="open")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {series}: failed to fetch ({exc})")
            continue
        for market in raw:
            views.append(TradingBot._to_view(market))

    if not views:
        print("No open markets found. Verify your series tickers (TEMPERATURE_SERIES).")
        return 1

    # Snapshot estimates from the quoted mids (the live bot sharpens these with
    # depth-weighted order-book data; this read-only view stays light).
    mids = {
        m.ticker: mid
        for m in views
        if informative(m) and (mid := mid_price_cents(m)) is not None
    }
    estimates = renormalized_estimates(views, mids)

    n_edges = 0
    views.sort(key=lambda m: (m.event_ticker, -m.volume))
    print(f"{'EDGE?':<6}{'TICKER':<28}{'EVENT':<22}{'VOL':>10}{'EST':>7}{'YES_BID':>9}{'YES_ASK':>9}{'BEST_SIDE':>16}")
    print("-" * 107)
    for m in views:
        est = estimates.get(m.ticker)
        sides = []
        if est is not None:
            sides = [
                c for s in ("yes", "no")
                if (c := evaluate_side(m, s, est, cfg.portfolio_fraction)) is not None
            ]
        best = max(sides, key=lambda c: c.edge_cents) if sides else None
        flag = ""
        if best is not None and best.edge_cents >= MIN_EDGE_CENTS and best.growth > 0:
            flag = "EDGE"
            n_edges += 1
        est_txt = "-" if est is None else f"{est:.1f}%"
        bid = "-" if m.yes_bid is None else str(m.yes_bid)
        ask = "-" if m.yes_ask is None else str(m.yes_ask)
        side_txt = "-" if best is None else f"{best.side.upper()} {best.edge_cents:+.1f}c"
        print(f"{flag:<6}{m.ticker:<28}{m.event_ticker:<22}{m.volume:>10.0f}{est_txt:>7}{bid:>9}{ask:>9}{side_txt:>16}")
    print(f"\n{n_edges} side(s) currently clear the +{MIN_EDGE_CENTS:.0f}c net-edge bar "
          f"(estimate vs all-in taker cost, fees included; the live bot re-checks "
          f"with depth-weighted order-book estimates before trading).")
    return 0


def cmd_balance(cfg: Config) -> int:
    auth = build_auth(cfg)
    if auth is None:
        print("No API credentials configured -- cannot fetch balance.")
        return 2
    client = build_client(cfg, auth)
    data = client.get_balance()
    bal = money.balance_cents(data)
    pv = money.portfolio_value_cents(data)
    print(f"Available balance : ${bal / 100:,.2f}")
    if pv is not None:
        print(f"Portfolio value   : ${pv / 100:,.2f}")
    print(f"One-trade budget  : ${int(bal * cfg.portfolio_fraction) / 100:,.2f} "
          f"(= {cfg.portfolio_fraction:.1%} of balance)")
    return 0


def cmd_gui() -> int:
    from .gui import main as gui_main

    gui_main()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="kalshi_temp_bot", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="run the trading loop")
    p_list = sub.add_parser("list-markets", help="list monitored markets and candidates (public)")
    p_list.add_argument("--series", help="comma-separated series tickers to inspect")
    sub.add_parser("balance", help="show account balance (requires credentials)")
    sub.add_parser("gui", help="launch the graphical interface")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == "gui":
        return cmd_gui()

    cfg = Config.from_env()
    if args.command == "run":
        return cmd_run(cfg)
    if args.command == "list-markets":
        override = [s.strip() for s in args.series.split(",")] if getattr(args, "series", None) else None
        return cmd_list_markets(cfg, override)
    if args.command == "balance":
        return cmd_balance(cfg)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
