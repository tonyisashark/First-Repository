"""Command-line interface.

    kalshi-bot run             start the trading loop (paper by default)
    kalshi-bot status          equity, halts, recent activity from local state
    kalshi-bot balance         exchange cash balance (or paper cash)
    kalshi-bot markets         top open markets by 24h volume
    kalshi-bot scan            one-shot arbitrage scan (read-only)
    kalshi-bot calibrate       measure favorite/longshot bias from settled markets
    kalshi-bot kill            halt trading + cancel resting orders (--flatten exits)
    kalshi-bot resume          clear halts
    kalshi-bot paper-reset     wipe the simulated portfolio
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from typing import Optional

from . import __version__
from .config import Config
from .money import micro_to_display, field_micro
from . import log as botlog


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="kalshi-bot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--env", choices=["demo", "prod"],
                        help="override KALSHI_ENV for this invocation")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="start the trading loop")
    sub.add_parser("gui", help="open the desktop control panel")
    sub.add_parser("status", help="show local state summary")
    sub.add_parser("balance", help="show cash balance")
    p_markets = sub.add_parser("markets", help="top open markets by volume")
    p_markets.add_argument("--top", type=int, default=20)
    sub.add_parser("scan", help="one-shot arbitrage scan (no orders)")
    p_cal = sub.add_parser("calibrate", help="empirical calibration from settled markets")
    p_cal.add_argument("--days", type=int, default=30)
    p_kill = sub.add_parser("kill", help="halt trading and cancel resting orders")
    p_kill.add_argument("--flatten", action="store_true",
                        help="also exit all positions at the touch (taker)")
    sub.add_parser("resume", help="clear halts and allow trading again")
    sub.add_parser("paper-reset", help="reset the paper portfolio")

    args = parser.parse_args(argv)
    if args.env:
        os.environ["KALSHI_ENV"] = args.env
        os.environ.pop("KALSHI_API_BASE", None)
    cfg = Config.from_env()
    botlog.setup(cfg.log_level)

    command = args.command or "run"
    handler = {
        "run": cmd_run,
        "gui": cmd_gui,
        "status": cmd_status,
        "balance": cmd_balance,
        "markets": cmd_markets,
        "scan": cmd_scan,
        "calibrate": cmd_calibrate,
        "kill": cmd_kill,
        "resume": cmd_resume,
        "paper-reset": cmd_paper_reset,
    }[command]
    try:
        return handler(cfg, args) or 0
    except KeyboardInterrupt:
        return 130


# ---------------------------------------------------------------------------

def cmd_run(cfg: Config, _args) -> int:
    from .bot import build_bot

    if cfg.live_trading:
        print("*** LIVE TRADING MODE: real money at risk ***", file=sys.stderr)
    bot = build_bot(cfg)
    bot.run_forever()
    return 0


def cmd_gui(_cfg: Config, _args) -> int:
    try:
        from .gui import main as gui_main
    except ImportError as exc:
        print(f"GUI unavailable: {exc}\n"
              "Install Tk support (e.g. `apt install python3-tk`) or use the "
              "packaged KalshiBot.exe on Windows.", file=sys.stderr)
        return 1
    return gui_main()


def cmd_status(cfg: Config, _args) -> int:
    from .state import StateStore
    from .risk import RiskManager

    state = StateStore(cfg.state_db_path)
    snap = state.latest_snapshot(cfg.mode_key)
    print(f"mode: {'paper' if cfg.dry_run else 'live'} ({cfg.env})")
    if snap:
        ts, cash, mtm, resting, equity = snap
        age = int(time.time()) - ts
        print(f"equity:  {micro_to_display(equity)}  (as of {age}s ago)")
        print(f"  cash {micro_to_display(cash)} | positions {micro_to_display(mtm)}"
              f" | resting escrow {micro_to_display(resting)}")
        day_ago = ts - 86_400
        history = state.snapshots_since(day_ago, cfg.mode_key)
        if len(history) > 1:
            first = history[0][1]
            if first:
                change = (equity - first) / first * 100
                print(f"  24h change: {change:+.2f}%")
    else:
        print("no snapshots yet -- run `kalshi-bot run` first")
    risk = RiskManager(cfg, state)
    blocked, reasons = risk.entries_blocked()
    print(f"halts: {'; '.join(reasons) if blocked else 'none'}")
    rows = state.recent_journal(8)
    if rows:
        print("recent activity:")
        for ts, kind, detail in rows:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(ts))
            print(f"  {stamp}  {kind}  {detail[:90]}")
    state.close()
    return 0


def cmd_balance(cfg: Config, _args) -> int:
    if cfg.dry_run:
        from .state import StateStore
        from .paper import PaperBroker

        state = StateStore(cfg.state_db_path)
        broker = PaperBroker(state, cfg)
        print(f"paper cash: {micro_to_display(broker.cash)}")
        positions = broker.positions()
        for ticker, pos in sorted(positions.items()):
            print(f"  {ticker}: {pos.count:+d} contracts (cost {micro_to_display(pos.exposure)})")
        state.close()
        return 0
    client = _client(cfg)
    balance = client.get_balance()
    print(f"balance: {micro_to_display(field_micro(balance, 'balance') or 0)}")
    return 0


def cmd_markets(cfg: Config, args) -> int:
    from .models import Market

    client = _client(cfg)
    markets = [Market.from_payload(m)
               for m in client.get_markets(status="open", max_pages=2)]
    markets.sort(key=lambda m: -m.volume_24h)
    print(f"{'ticker':40} {'vol24h':>8} {'bid':>7} {'ask':>7}  title")
    for market in markets[: args.top]:
        bid = f"{(market.yes_bid or 0)/10_000:.0f}c"
        ask = f"{(market.yes_ask or 0)/10_000:.0f}c"
        print(f"{market.ticker:40} {market.volume_24h:>8} {bid:>7} {ask:>7}  "
              f"{market.title[:40]}")
    return 0


def cmd_scan(cfg: Config, _args) -> int:
    from .models import Event, OrderBook
    from .strategies.arbitrage import find_candidates, plan_opportunity

    client = _client(cfg)
    print("fetching open events...")
    events = []
    cursor = None
    for _ in range(25):
        raw, cursor = client.get_events_page(status="open", cursor=cursor)
        events.extend(Event.from_payload(e) for e in raw)
        if cursor is None:
            break
    now = int(time.time())
    candidates = find_candidates(events, now, cfg)
    print(f"{len(events)} open events, {len(candidates)} arbitrage candidates")
    found = 0
    for event in candidates[:20]:
        tickers = [m.ticker for m in event.markets]
        payloads = client.get_orderbooks(tickers)
        books = {t: OrderBook.from_payload(p, event.markets[0].notional)
                 for t, p in payloads.items()}
        opp = plan_opportunity(event, books, cfg)
        if opp is None:
            continue
        found += 1
        print(f"  {opp.direction:5} {event.event_ticker:30} sets={opp.sets:<5} "
              f"profit/set={opp.profit_per_set/10_000:.2f}c "
              f"total={micro_to_display(opp.total_profit)} "
              f"capital={micro_to_display(opp.total_capital)}")
    if not found:
        print("no executable opportunities right now (normal -- they are rare "
              "and short-lived)")
    return 0


def cmd_calibrate(cfg: Config, args) -> int:
    from .models import Market

    client = _client(cfg)
    since = int(time.time()) - args.days * 86_400
    print(f"fetching markets settled in the last {args.days} days...")
    rows = client.get_settled_markets(min_settled_ts=since, max_pages=20)
    buckets: dict = defaultdict(lambda: [0, 0])  # price bin -> [favorite wins, total]
    used = 0
    for raw in rows:
        market = Market.from_payload(raw)
        if market.result not in ("yes", "no") or market.last is None:
            continue
        if market.last <= 0 or market.last >= market.notional:
            continue
        if market.last >= market.notional // 2:
            fav_price, fav_won = market.last, market.result == "yes"
        else:
            fav_price, fav_won = market.notional - market.last, market.result == "no"
        bin_lo = (fav_price // 50_000) * 5  # 5-cent bins, in cents
        buckets[bin_lo][1] += 1
        buckets[bin_lo][0] += int(fav_won)
        used += 1
    print(f"{used} settled markets with a usable last price\n")
    print(f"{'favorite price':>15} {'n':>6} {'implied':>9} {'realized':>9} {'edge':>7}")
    suggestion = None
    for bin_lo in sorted(buckets):
        wins, total = buckets[bin_lo]
        if total < 20:
            continue
        implied = (bin_lo + 2.5) / 100
        realized = wins / total
        edge = (realized - implied) * 100
        marker = ""
        if 90 <= bin_lo <= 95:
            suggestion = edge if suggestion is None else max(suggestion, edge)
            marker = "  <- longshot band"
        print(f"{bin_lo:>11}-{bin_lo+5:<3} {total:>6} {implied:>8.1%} {realized:>8.1%} "
              f"{edge:>+6.1f}c{marker}")
    print("\ncaveat: uses each market's *last* trade price, which can be stale in"
          " illiquid markets.")
    if suggestion is not None:
        print(f"suggested LONGSHOT_EDGE_CENTS ~ {max(suggestion, 0.0):.1f} "
              f"(current: {cfg.longshot_edge/10_000:.1f})")
    return 0


def cmd_kill(cfg: Config, args) -> int:
    from .bot import build_bot

    bot = build_bot(cfg)
    bot.risk.kill("operator kill switch")
    view = bot.portfolio.refresh(bot.cache.markets)
    cancelled = 0
    for order in view.orders:
        if bot.state.strategy_of_order(order.order_id):
            if bot.executor.cancel(order.order_id):
                cancelled += 1
    print(f"halted; cancelled {cancelled} resting orders")
    if args.flatten:
        flattened = _flatten(bot, view)
        print(f"flatten: closed {flattened} contracts at the touch "
              "(check `status` for residuals)")
    return 0


def _flatten(bot, view) -> int:
    closed = 0
    for ticker, position in view.positions.items():
        market = view.markets.get(ticker)
        if market is None or not market.tradeable or position.count == 0:
            continue
        book = bot.cache.books([ticker]).get(ticker)
        if book is None:
            continue
        if position.count > 0:
            side, touch = "ask", book.best_bid("yes")
        else:
            side, touch = "bid", book.best_ask("yes")
        if touch is None:
            continue
        result = bot.executor.place(
            strategy="kill", market=market, side=side, price=touch,
            count=abs(position.count), time_in_force="immediate_or_cancel",
            reduce_only=True, book=book,
        )
        closed += result.get("filled", 0)
    return closed


def cmd_resume(cfg: Config, _args) -> int:
    from .state import StateStore
    from .risk import RiskManager

    state = StateStore(cfg.state_db_path)
    RiskManager(cfg, state).resume()
    print("halts cleared; trading allowed again")
    state.close()
    return 0


def cmd_paper_reset(cfg: Config, _args) -> int:
    from .state import StateStore

    state = StateStore(cfg.state_db_path)
    state.paper_reset()
    state.close()
    print(f"paper portfolio reset to {micro_to_display(cfg.paper_cash)}")
    return 0


def _client(cfg: Config):
    from .auth import KalshiSigner
    from .client import KalshiClient

    signer = None
    if cfg.has_credentials:
        signer = KalshiSigner.load(cfg.api_key_id, cfg.private_key_path,
                                   cfg.private_key_pem)
    return KalshiClient(cfg.api_base, signer=signer, read_rps=cfg.read_rps,
                        write_rps=cfg.write_rps, timeout=cfg.request_timeout)


if __name__ == "__main__":
    sys.exit(main())
