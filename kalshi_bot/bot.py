"""The orchestrator: market cache, reconciliation, risk gates, main loop.

One cycle (default every 10s):

1. refresh the open-events cache (rotating pages, rate-limit friendly)
2. paper mode: sync simulated maker fills + settlements against live data
   live mode: pull new fills into the local ledger (idempotent)
3. value the portfolio (cash + escrow + conservative marks) -> equity
4. run the risk assessment (day anchor, high-water mark, halts)
5. on a fresh halt: cancel all resting strategy orders
6. snapshot equity (the compounding/performance record)
7. run strategies: arbitrage + market maker every cycle, longshot on its
   own slower interval
8. log a one-line status heartbeat

Every strategy failure is contained: three consecutive errors put that
strategy on a five-minute cooldown instead of crashing the loop.
"""

from __future__ import annotations

import logging
import signal
import time
from typing import Dict, List, Optional

from .client import KalshiAPIError, KalshiClient
from .config import Config
from .execution import Executor
from .models import Event, Fill, Market, OrderBook
from .money import micro_to_display
from .paper import PaperBroker
from .portfolio import PortfolioService, PortfolioView
from .risk import RiskManager
from .state import StateStore
from .strategies import (ArbitrageStrategy, BotContext, LongshotStrategy,
                         MarketMakerStrategy, Strategy)

logger = logging.getLogger(__name__)

KV_FILLS_SYNCED = "fills_synced_ts"
STRATEGY_ERROR_LIMIT = 3
STRATEGY_COOLDOWN_S = 300.0


class MarketCache:
    """Rotating cache of open events (with nested markets) + per-cycle books."""

    def __init__(self, client: KalshiClient, cfg: Config) -> None:
        self.client = client
        self.cfg = cfg
        self.events: Dict[str, Event] = {}
        self._seen_sweep: Dict[str, int] = {}
        self._sweep = 0
        self._cursor: Optional[str] = None
        self._last_refresh = 0.0
        self._book_memo: Dict[str, OrderBook] = {}

    @property
    def markets(self) -> Dict[str, Market]:
        out: Dict[str, Market] = {}
        for event in self.events.values():
            for market in event.markets:
                out[market.ticker] = market
        return out

    def new_cycle(self) -> None:
        self._book_memo = {}

    def refresh(self, now: float) -> None:
        if self.events and now - self._last_refresh < self.cfg.universe_refresh_seconds:
            return
        self._last_refresh = now
        pages = self.cfg.universe_pages_per_refresh
        for _ in range(max(pages, 1)):
            raw_events, cursor = self.client.get_events_page(
                status="open", cursor=self._cursor, with_nested_markets=True)
            for raw in raw_events:
                event = Event.from_payload(raw)
                if event.event_ticker:
                    self.events[event.event_ticker] = event
                    self._seen_sweep[event.event_ticker] = self._sweep
            self._cursor = cursor
            if cursor is None:
                self._evict_unseen()
                self._sweep += 1
                break

    def _evict_unseen(self) -> None:
        """After a full sweep, drop events that vanished from the open list."""
        stale = [t for t, sweep in self._seen_sweep.items() if sweep != self._sweep]
        for ticker in stale:
            self.events.pop(ticker, None)
            self._seen_sweep.pop(ticker, None)

    def books(self, tickers: List[str]) -> Dict[str, OrderBook]:
        missing = [t for t in tickers if t not in self._book_memo]
        if missing:
            markets = self.markets
            payloads = self.client.get_orderbooks(missing)
            for ticker in missing:
                payload = payloads.get(ticker)
                if payload is None:
                    continue
                notional = markets[ticker].notional if ticker in markets else 1_000_000
                self._book_memo[ticker] = OrderBook.from_payload(payload, notional)
        return {t: self._book_memo[t] for t in tickers if t in self._book_memo}


class TradingBot:
    def __init__(
        self,
        cfg: Config,
        client: KalshiClient,
        state: StateStore,
        paper: Optional[PaperBroker] = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.state = state
        self.paper = paper
        self.cache = MarketCache(client, cfg)
        self.risk = RiskManager(cfg, state)
        self.executor = Executor(cfg, state, self.risk,
                                 client=None if paper else client, paper=paper)
        self.portfolio = PortfolioService(cfg, state, client, paper=paper)
        self.strategies: List[Strategy] = []
        if cfg.arb_enabled:
            self.strategies.append(ArbitrageStrategy())
        if cfg.mm_enabled:
            self.strategies.append(MarketMakerStrategy())
        if cfg.longshot_enabled:
            self.strategies.append(LongshotStrategy())
        self._longshot_due = 0.0
        self._snapshot_due = 0.0
        self._errors: Dict[str, int] = {}
        self._cooldown_until: Dict[str, float] = {}
        self._was_halted = False
        self._stop = False
        # Optional observer (e.g. the GUI) called with a status dict per cycle.
        self.status_listener: Optional[callable] = None

    def stop(self) -> None:
        """Request a graceful stop after the current cycle (thread-safe)."""
        self._stop = True

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:
        self._install_signal_handlers()
        mode = "PAPER" if self.paper else ("LIVE" if self.cfg.env == "prod" else "DEMO-LIVE")
        logger.info("starting kalshi-bot [%s] env=%s api=%s strategies=%s",
                    mode, self.cfg.env, self.cfg.api_base,
                    ",".join(s.name for s in self.strategies) or "none")
        while not self._stop:
            started = time.monotonic()
            try:
                self.run_cycle()
            except KalshiAPIError as exc:
                if exc.status in (401, 403):
                    logger.error("authentication failed (%s); check KALSHI_API_KEY_ID/"
                                 "KALSHI_PRIVATE_KEY_PATH. Exiting.", exc.message)
                    break
                logger.warning("cycle failed: %s", exc)
            except Exception:
                logger.exception("cycle failed unexpectedly")
            elapsed = time.monotonic() - started
            self._interruptible_sleep(max(0.5, self.cfg.poll_seconds - elapsed))
        self.shutdown()

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def run_cycle(self, now: Optional[int] = None) -> PortfolioView:
        now = int(now if now is not None else time.time())
        self.cache.new_cycle()
        self.cache.refresh(time.monotonic())

        if not self._exchange_open():
            logger.info("exchange not in active trading; idling")
            time.sleep(30)
            return self.portfolio.refresh(self.cache.markets)

        if self.paper is not None:
            self._sync_paper(now)
        else:
            self._sync_fills(now)

        view = self.portfolio.refresh(self.cache.markets)
        status = self.risk.assess(view)

        if status.halted and not self._was_halted:
            logger.warning("entries halted (%s); cancelling resting orders",
                           "; ".join(status.reasons))
            self._cancel_all_resting(view)
        self._was_halted = status.halted

        if now >= self._snapshot_due:
            self.state.save_snapshot(now, view.cash, view.mtm,
                                     view.resting_escrow, view.equity,
                                     mode=self.cfg.mode_key)
            self._snapshot_due = now + self.cfg.snapshot_interval_seconds

        if not status.halted:
            self._run_strategies(view, now)

        self._heartbeat(view, status)
        return view

    # ------------------------------------------------------------ sub-steps
    def _exchange_open(self) -> bool:
        try:
            payload = self.client.exchange_status()
        except KalshiAPIError as exc:
            if exc.status in (401, 403):
                raise
            logger.warning("exchange status unavailable: %s", exc.message)
            return False
        return bool(payload.get("trading_active", payload.get("exchange_active", True)))

    def _sync_fills(self, now: int) -> None:
        since = self.state.kv_get_int(KV_FILLS_SYNCED, 0)
        if since <= 0:
            since = now - self.cfg.fills_lookback_seconds
        try:
            raw_fills = self.client.get_fills(min_ts=max(0, since - 60))
        except KalshiAPIError as exc:
            logger.warning("fill sync failed: %s", exc.message)
            return
        new = 0
        for raw in raw_fills:
            fill = Fill.from_payload(raw)
            if not fill.fill_id:
                continue
            if self.state.record_fill(
                fill_id=fill.fill_id, order_id=fill.order_id, ticker=fill.ticker,
                side=fill.book_side, count=fill.count, price_micro=fill.yes_price,
                fee_micro=fill.fee, is_taker=fill.is_taker, ts=fill.ts,
            ):
                new += 1
        if new:
            logger.info("recorded %d new fills", new)
        self.state.kv_set(KV_FILLS_SYNCED, now)

    def _sync_paper(self, now: int) -> None:
        """Feed live market states/books to the paper broker.

        Always fetches fresh market payloads for held tickers: settlement
        status must not lag behind the (slow-refreshing) universe cache.
        """
        tickers = sorted(set(self.paper.positions())
                         | {o.ticker for o in self.paper.orders()})
        if not tickers:
            return
        markets: Dict[str, Market] = {}
        try:
            for raw in self.client.get_markets(status=None, tickers=tickers):
                market = Market.from_payload(raw)
                markets[market.ticker] = market
        except KalshiAPIError as exc:
            logger.warning("paper sync quote fetch failed: %s", exc.message)
            return
        active = [t for t, m in markets.items() if m.tradeable]
        books = self.cache.books(active) if active else {}
        for ticker, market in markets.items():
            self.paper.sync_with_market(market, books.get(ticker))

    def _run_strategies(self, view: PortfolioView, now: int) -> None:
        ctx = BotContext(
            cfg=self.cfg, now=now, view=view, events=list(self.cache.events.values()),
            markets=self.cache.markets, books=self.cache.books,
            executor=self.executor, risk=self.risk, state=self.state,
            strategy_used=self.portfolio.strategy_exposure(view),
        )
        for strategy in self.strategies:
            if strategy.name == "longshot":
                if now < self._longshot_due:
                    continue
                self._longshot_due = now + self.cfg.longshot_interval_seconds
            if time.monotonic() < self._cooldown_until.get(strategy.name, 0):
                continue
            try:
                strategy.step(ctx)
                self._errors[strategy.name] = 0
            except KalshiAPIError as exc:
                if exc.status in (401, 403):
                    raise
                self._note_strategy_error(strategy.name, str(exc))
            except Exception as exc:
                logger.exception("[%s] strategy error", strategy.name)
                self._note_strategy_error(strategy.name, repr(exc))

    def _note_strategy_error(self, name: str, message: str) -> None:
        count = self._errors.get(name, 0) + 1
        self._errors[name] = count
        logger.warning("[%s] error %d/%d: %s", name, count, STRATEGY_ERROR_LIMIT, message)
        if count >= STRATEGY_ERROR_LIMIT:
            self._cooldown_until[name] = time.monotonic() + STRATEGY_COOLDOWN_S
            self._errors[name] = 0
            logger.warning("[%s] cooling down for %.0fs", name, STRATEGY_COOLDOWN_S)

    def _cancel_all_resting(self, view: PortfolioView) -> None:
        for order in view.orders:
            if self.state.strategy_of_order(order.order_id):
                self.executor.cancel(order.order_id)

    def _heartbeat(self, view: PortfolioView, status) -> None:
        used = self.portfolio.strategy_exposure(view)
        budgets = {
            "arb": self.cfg.arb_budget_frac,
            "mm": self.cfg.mm_budget_frac,
            "longshot": self.cfg.longshot_budget_frac,
        }
        deployed = " ".join(
            f"{name}={micro_to_display(used.get(name, 0))}/"
            f"{micro_to_display(int(view.equity * frac))}"
            for name, frac in budgets.items())
        halt = f" HALTED({'; '.join(status.reasons)})" if status.halted else ""
        day_frac = (status.day_pnl / status.day_anchor) if status.day_anchor else 0.0
        logger.info(
            "equity %s (day %+0.2f%%, dd %.1f%%) cash %s mtm %s escrow %s | "
            "pos %d orders %d | %s%s",
            micro_to_display(view.equity), day_frac * 100,
            status.drawdown_frac * 100,
            micro_to_display(view.cash), micro_to_display(view.mtm),
            micro_to_display(view.resting_escrow),
            len(view.positions), len(view.orders), deployed, halt,
        )
        if self.status_listener is not None:
            try:
                self.status_listener({
                    "ts": view.ts,
                    "mode": "paper" if self.paper else "live",
                    "env": self.cfg.env,
                    "equity": view.equity,
                    "cash": view.cash,
                    "mtm": view.mtm,
                    "escrow": view.resting_escrow,
                    "day_pnl": status.day_pnl,
                    "day_frac": day_frac,
                    "drawdown_frac": status.drawdown_frac,
                    "positions": len(view.positions),
                    "orders": len(view.orders),
                    "halted": status.halted,
                    "halt_reasons": list(status.reasons),
                    "used": dict(used),
                    "budgets": {name: int(view.equity * frac)
                                for name, frac in budgets.items()},
                })
            except Exception:  # pragma: no cover - observer must never kill the loop
                logger.exception("status listener failed")

    # -------------------------------------------------------------- shutdown
    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            logger.info("received signal %s; shutting down after this cycle", signum)
            self._stop = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except ValueError:  # pragma: no cover - non-main thread
                pass

    def shutdown(self) -> None:
        if self.cfg.cancel_orders_on_exit:
            try:
                view = self.portfolio.refresh(self.cache.markets)
                resting = [o for o in view.orders
                           if self.state.strategy_of_order(o.order_id)]
                if resting:
                    logger.info("cancelling %d resting orders before exit", len(resting))
                for order in resting:
                    self.executor.cancel(order.order_id)
            except Exception:
                logger.exception("failed to cancel resting orders on shutdown")
        self.state.journal("shutdown", {})
        logger.info("bot stopped")


def build_bot(cfg: Config) -> TradingBot:
    """Wire a bot from config (shared by CLI commands and tests)."""
    from .auth import KalshiSigner

    cfg.validate()
    signer = None
    if cfg.has_credentials:
        signer = KalshiSigner.load(cfg.api_key_id, cfg.private_key_path,
                                   cfg.private_key_pem)
    client = KalshiClient(
        cfg.api_base, signer=signer, read_rps=cfg.read_rps,
        write_rps=cfg.write_rps, timeout=cfg.request_timeout,
    )
    state = StateStore(cfg.state_db_path)
    paper = PaperBroker(state, cfg) if cfg.dry_run else None
    return TradingBot(cfg, client, state, paper=paper)
