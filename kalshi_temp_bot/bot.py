"""The daily-temperature trading bot.

Manages up to ``max_positions`` concurrent positions (YES or NO). Each position
runs its own small lifecycle:

BUYING   a limit buy at the side's ask is working -- waiting for fill(s)
HOLDING  contracts held while the book and the estimate are monitored
EXITING  a forced close-out (aggressive sell) is working

Entry: the bot continuously estimates every market's true probability from
market data alone (multi-level depth-weighted microprice, EWMA-smoothed,
renormalized across each event's buckets) and takes the single market side --
YES at the ask or NO at ``100 - bid`` -- with the greatest expected log-growth,
provided its net edge (estimate minus all-in cost, fees included) clears
``MIN_EDGE_CENTS``. Sizing is the configured portfolio fraction, capped at the
trade's own Kelly fraction.

Exit rules, per position:
* liquidity     -- the book's resting depth on our exit side falls to/below
                   ``LIQUIDITY_EXIT_BUFFER x position size``: sell right before
                   the liquidity needed to exit runs out;
* edge-reversal -- the market's bid now *overprices* our side versus the
                   estimate by ``EXIT_EDGE_CENTS`` (net of the exit fee):
                   cashing out beats holding, whether at a profit or a loss.
A position that hits neither rides through market close and settles.

A sell is only ever attempted while the market has at least one bid on our
side -- a worthless position with an empty book is held quietly (selling into
nothing just fails) and does NOT count against ``max_positions``, so a stuck
position can't block new trades.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from . import money
from .config import Config
from .kalshi_client import KalshiClient
from .kalshi_ws import KalshiWebSocket, MarketDataCache
from .strategy import (
    ESTIMATE_MAX_AGE_SECONDS,
    EWMA_HALF_LIFE_SECONDS,
    EXIT_EDGE_CENTS,
    LEVEL_DECAY_CENTS,
    MAX_INFORMATIVE_SPREAD_CENTS,
    MIN_EDGE_CENTS,
    RAIL_MAX_CENTS,
    RAIL_MIN_CENTS,
    MarketView,
    TradeCandidate,
    best_candidate,
    evaluate_maker_side,
    evaluate_side,
    idle_watch_summary,
    informative,
    microprice_cents,
    mid_price_cents,
    parse_time,
    position_size,
    renormalized_estimates,
    select_maker_candidates,
    select_trade_candidates,
    side_quotes,
    taker_fee_cents,
)

logger = logging.getLogger(__name__)

# --- self-tuned timing / risk constants --------------------------------------
POLL_INTERVAL_SECONDS = 1.0        # decision-loop cadence
# The market *universe* changes slowly (new buckets list once a day) and live
# quotes ride the WebSocket, so the REST re-scan can be sparse -- the read
# budget that frees up goes to order-book fetches (the real estimate input).
SCAN_INTERVAL_SECONDS = 15.0       # REST market-universe re-scan cadence
HEARTBEAT_INTERVAL_SECONDS = 30.0  # "I'm alive" status-line cadence
BOOK_POLL_SECONDS = 5.0            # per-ticker order-book fetch cadence
BUY_TIMEOUT_SECONDS = 30.0         # cancel an unfilled TAKER entry after this
# A resting maker bid is patient by design, but stale prices are dangerous:
# cancel and let the next tick repost at the freshly-estimated level.
MAKER_BUY_TIMEOUT_SECONDS = 60.0
# A resting take-profit offer gets this long to earn the spread before the
# bot falls back to selling at the bid (the bird in hand).
EXIT_OFFER_TIMEOUT_SECONDS = 60.0
LIQUIDITY_EXIT_BUFFER = 2.0        # sell when exit-side depth <= this x position
# Take-profit: the strategy harvests convergence rather than holding to
# settlement, so the same bankroll cycles through several edges a day. Sell
# once the bid (net of the exit fee) locks a real gain over the all-in entry
# cost AND holding to settlement adds almost nothing over selling right now.
TAKE_PROFIT_MIN_GAIN_CENTS = 1.0   # locked gain must beat this
TAKE_PROFIT_REMAINING_CENTS = 1.0  # holding must add less than this
# After a forced exit, don't re-enter the same market for a while: the exit
# means either its liquidity is draining or the model and the market disagree
# sharply -- flipping straight back in would just churn fees on noise.
REENTRY_COOLDOWN_SECONDS = 300.0

# Seconds to wait between successive per-series market requests during a scan,
# so a multi-city scan doesn't burst past Kalshi's rate limit.
SERIES_REQUEST_SPACING = 0.15

# Book fetches are the expensive part of estimation. Markets whose cheap
# mid-based edge is no more than this far BELOW the edge bar get tracked
# (continuous book polls feed the EWMA, so by the time a real edge crosses the
# bar the estimate has history behind it). A fairly-priced tight book sits
# around -1 to -3c on this measure, so the slack must comfortably cover that.
# Fetches are budgeted per tick so tracking can't burst past the rate limit.
PREFILTER_SLACK_CENTS = 8.0
BOOK_FETCH_BUDGET_PER_TICK = 8
# Leftover budget after the near-edge list goes to background coverage: a
# least-recently-polled round-robin over the rest of the universe, so every
# informative market eventually carries a book-backed estimate. The cadence
# keeps a series alive (< ESTIMATE_MAX_AGE_SECONDS between samples) and lets
# it mature (3 samples) within ~a minute of first being seen. Books that came
# back one-sided/uninformative are re-checked, just much less often.
BACKGROUND_POLL_SECONDS = 25.0
UNINFORMATIVE_POLL_SECONDS = 90.0
# An estimate may only TRADE once it has real history: several book samples
# over a minimum age. A first glance at a book seeds the EWMA outright, so
# without this gate one quirky snapshot could buy a position by itself.
MIN_ESTIMATE_HISTORY_SECONDS = 15.0
MIN_ESTIMATE_SAMPLES = 3


class TradeState(Enum):
    BUYING = "buying"
    HOLDING = "holding"
    OFFERING = "offering"                  # take-profit offer resting in the book
    EXITING = "exiting"


@dataclass
class Trade:
    """A single position and its in-flight orders."""

    ticker: str
    side: str                              # "yes" or "no"
    count: float                           # contracts (0.01 granularity on v2)
    buy_price: Optional[int]               # in the side's own terms
    event: str = ""                        # event ticker ("" when unknown)
    maker: bool = False                    # entry rested inside the spread (no fee)
    state: TradeState = TradeState.BUYING
    buy_order: Optional[dict] = None
    exit_order: Optional[dict] = None
    buy_placed_at: float = 0.0
    cost_cents: float = 0.0                # all-in entry cost per contract (set on fill)
    exit_offer_price: Optional[int] = None # resting take-profit offer level
    exit_offer_at: float = 0.0
    last_mark_cents: Optional[float] = None  # latest sellable value (bid - fee)
    depth_checked_at: float = 0.0          # last order-book poll
    bid_depth: Optional[float] = None      # last observed exit-side depth
    missing_scans: int = 0                 # consecutive scans without the market


class TradingBot:
    def __init__(
        self,
        client: KalshiClient,
        config: Config,
        cache: Optional[MarketDataCache] = None,
        ws: Optional[KalshiWebSocket] = None,
    ) -> None:
        self.client = client
        self.cfg = config
        self.cache = cache
        self.ws = ws

        self.trades: List[Trade] = []

        # Tuned constants, copied to instance attributes so tests can shorten
        # them; they are not user configuration.
        self.poll_interval = POLL_INTERVAL_SECONDS
        self.scan_interval = SCAN_INTERVAL_SECONDS
        self.heartbeat_interval = HEARTBEAT_INTERVAL_SECONDS
        self.book_poll_seconds = BOOK_POLL_SECONDS
        self.background_poll_seconds = BACKGROUND_POLL_SECONDS
        self.buy_timeout = BUY_TIMEOUT_SECONDS
        self.maker_buy_timeout = MAKER_BUY_TIMEOUT_SECONDS
        self.exit_offer_timeout = EXIT_OFFER_TIMEOUT_SECONDS
        self.ewma_half_life = EWMA_HALF_LIFE_SECONDS
        self.min_estimate_history = MIN_ESTIMATE_HISTORY_SECONDS
        self.min_estimate_samples = MIN_ESTIMATE_SAMPLES

        self._market_cache: List[MarketView] = []
        self._last_scan: float = 0.0
        self._scanned_this_tick: bool = False
        self._last_heartbeat: float = 0.0
        self._current_tickers: List[str] = []
        self._running = False

        # EWMA-smoothed microprice per ticker: ticker -> (value_cents, updated_at).
        self._micro_ewma: Dict[str, Tuple[float, float]] = {}
        # Estimate maturity per ticker: ticker -> (first_sample_at, n_samples).
        self._micro_history: Dict[str, Tuple[float, int]] = {}
        # Last fetched book summary per ticker: ticker -> (summary, fetched_at).
        self._book_cache: Dict[str, Tuple[dict, float]] = {}
        self._book_polled_at: Dict[str, float] = {}
        # Forced-exit timestamps per ticker, for the re-entry cooldown.
        self._exited_at: Dict[str, float] = {}
        # Book-fetch outcome counters since the last heartbeat, so a zero-
        # estimate state explains itself ("all books one-sided" vs "fetches
        # failing" need opposite responses).
        self._book_stats = {"ok": 0, "unusable": 0, "failed": 0}

        # Paper ledger (dry-run): real cash accounting so the heartbeat shows
        # actual session profitability, not a static configured balance.
        self._paper_balance: float = float(config.paper_balance_cents)
        self._paper_pnl: float = 0.0
        self._paper_closed: int = 0

    # -- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        self._running = True
        mode = "DRY-RUN (paper)" if self.cfg.dry_run else "LIVE"
        logger.info(
            "Starting bot | env=%s | mode=%s | max_positions=%d | series=%s",
            self.cfg.env, mode, self.cfg.max_positions, ",".join(self.cfg.temperature_series),
        )
        if self.ws is not None:
            self.ws.start()
        if not self.cfg.dry_run:
            try:
                self._reconcile_existing_positions()
            except Exception as exc:
                logger.warning("Startup position reconcile failed: %s", exc)

        while self._running:
            try:
                self.tick()
            except Exception as exc:
                logger.exception("Tick error: %s", exc)
            time.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False
        if self.ws is not None:
            self.ws.stop()

    # -- main step ---------------------------------------------------------
    def tick(self) -> None:
        markets = self._get_markets()
        # Advance each open position; drop the ones that have gone flat.
        self.trades = [t for t in self.trades if not self._manage_trade(t, markets)]
        # Open new positions while we have free slots.
        self._maybe_enter(markets)
        # Heartbeat last, so it reports this tick's fetches and entries rather
        # than a snapshot the entry logic is about to make stale.
        self._maybe_heartbeat(markets)

    # -- heartbeat ---------------------------------------------------------
    def _maybe_heartbeat(self, markets: List[MarketView]) -> None:
        """Periodically log a status line so an idle (silent) bot is visibly alive.

        Fires immediately on the first tick, then every ``heartbeat_interval``.
        """
        now = time.time()
        if self._last_heartbeat != 0.0 and (now - self._last_heartbeat) < self.heartbeat_interval:
            return
        self._last_heartbeat = now
        try:
            logger.info("Heartbeat: %s", self._heartbeat_message(markets))
        except Exception:  # noqa: BLE001 - a status line must never break the loop
            logger.debug("heartbeat formatting failed", exc_info=True)

    def _heartbeat_message(self, markets: List[MarketView]) -> str:
        estimates = self._estimates(markets)
        watch = idle_watch_summary(
            markets,
            estimates=estimates,
            portfolio_fraction=self.cfg.portfolio_fraction,
        )
        if markets and not estimates:
            # Zero estimates should explain itself: fetches failing and books
            # being one-sided overnight call for opposite responses.
            stats = self._book_stats
            watch += (
                f" [books since last heartbeat: {stats['ok']} usable, "
                f"{stats['unusable']} one-sided/wide, {stats['failed']} failed]"
            )
        self._book_stats = {"ok": 0, "unusable": 0, "failed": 0}
        head = f"{self._occupied_slots(markets)}/{self.cfg.max_positions} slots, {len(self.trades)} position(s)"
        if self.cfg.dry_run:
            head += (
                f" | paper ${self._paper_balance / 100:.2f}"
                f" (P&L {self._paper_pnl / 100:+.2f}, {self._paper_closed} closed)"
            )
        if not self.trades:
            return f"{watch} | {head}"
        briefs = []
        for trade in self.trades[:3]:
            market = self._market_for(markets, trade.ticker)
            bid = self._side_bid(market, trade.side) if market else None
            bid_txt = f"{bid}c" if bid is not None else "?"
            depth_txt = f" depth {trade.bid_depth:.0f}" if trade.bid_depth is not None else ""
            briefs.append(
                f"{trade.ticker} {trade.side.upper()} x{trade.count:g} {trade.state.value} bid {bid_txt}{depth_txt}"
            )
        more = f" +{len(self.trades) - 3} more" if len(self.trades) > 3 else ""
        return f"{watch} | {head} [{'; '.join(briefs)}{more}]"

    # -- market data -------------------------------------------------------
    def _get_markets(self) -> List[MarketView]:
        """Return the current market universe, scanned via REST and overlaid with
        the freshest realtime prices from the WebSocket cache."""
        now = time.time()
        # Throttle by the scan interval -- *including* when the last scan came
        # back empty. (Re-scanning every tick on an empty result would hammer
        # the API and trigger continuous rate limiting that never recovers.)
        self._scanned_this_tick = False
        if self._last_scan == 0.0 or (now - self._last_scan) >= self.scan_interval:
            self._scan_markets()
            self._last_scan = now
            self._scanned_this_tick = True

        if self.cache is not None:
            for view in self._market_cache:
                fresh = self.cache.get(view.ticker)
                if not fresh:
                    continue
                if fresh.get("yes_bid") is not None:
                    view.yes_bid = fresh["yes_bid"]
                if fresh.get("yes_ask") is not None:
                    view.yes_ask = fresh["yes_ask"]
                if fresh.get("last_price") is not None:
                    view.last_price = fresh["last_price"]
                if fresh.get("volume"):
                    view.volume = fresh["volume"]
        return self._market_cache

    def _scan_markets(self) -> None:
        views: List[MarketView] = []
        for index, series in enumerate(self.cfg.temperature_series):
            if index > 0 and SERIES_REQUEST_SPACING > 0:
                # Spread requests out so a multi-city scan doesn't burst past
                # Kalshi's per-second rate limit.
                time.sleep(SERIES_REQUEST_SPACING)
            try:
                raw_markets = self.client.get_markets(series_ticker=series, status="open")
            except Exception as exc:
                logger.warning("Failed to fetch markets for series %s: %s", series, exc)
                continue
            for raw in raw_markets:
                views.append(self._to_view(raw))
        self._market_cache = views
        self._current_tickers = [v.ticker for v in views]
        if not views:
            logger.warning(
                "Scan found 0 open markets for series %s on env '%s'. "
                "Check that the series tickers exist on this environment "
                "(temperature markets live on 'prod').",
                ",".join(self.cfg.temperature_series), self.cfg.env,
            )
        else:
            logger.debug("Scanned %d open temperature markets", len(views))

    @staticmethod
    def _to_view(raw: dict) -> MarketView:
        return MarketView(
            ticker=raw.get("ticker", ""),
            event_ticker=raw.get("event_ticker", ""),
            yes_bid=money.market_price_cents(raw, "yes_bid"),
            yes_ask=money.market_price_cents(raw, "yes_ask"),
            last_price=money.market_price_cents(raw, "last_price"),
            volume=money.market_volume(raw),
            close_time=parse_time(raw.get("close_time")),
            status=raw.get("status", "open"),
        )

    def current_tickers(self) -> List[str]:
        """Used by the WebSocket layer to know what to subscribe to."""
        return list(self._current_tickers)

    # -- probability estimation (book microprice + EWMA + renormalization) --
    def _estimates(self, markets: List[MarketView]) -> Dict[str, float]:
        """Final renormalized probability estimates for every ticker whose
        book-backed microprice is fresh enough to trust."""
        return renormalized_estimates(markets, self._fresh_micro())

    def _fresh_micro(self) -> Dict[str, float]:
        now = time.time()
        return {
            ticker: value
            for ticker, (value, ts) in self._micro_ewma.items()
            if (now - ts) < ESTIMATE_MAX_AGE_SECONDS
        }

    def _tradable_estimates(self, markets: List[MarketView]) -> Dict[str, float]:
        """Estimates mature enough to put money behind: enough samples over
        enough time that one quirky book snapshot can't trade by itself.
        (The heartbeat shows immature estimates too; trading does not.)"""
        now = time.time()
        mature: Dict[str, float] = {}
        for ticker, value in self._estimates(markets).items():
            first_at, samples = self._micro_history.get(ticker, (now, 0))
            if samples >= self.min_estimate_samples and (now - first_at) >= self.min_estimate_history:
                mature[ticker] = value
        return mature

    def _note_book(self, ticker: str, summary: dict, now: float) -> None:
        """Fold one fetched order book into the per-ticker EWMA estimate."""
        self._book_cache[ticker] = (summary, now)
        bid, ask = summary["bid"], summary["ask"]
        if bid is None or ask is None or not (0 <= ask - bid <= MAX_INFORMATIVE_SPREAD_CENTS):
            # One-sided, crossed or uninformative book: let the estimate go stale.
            self._book_stats["unusable"] += 1
            return
        micro = microprice_cents(bid, ask, summary["bid_eff"], summary["ask_eff"])
        if micro is None:
            self._book_stats["unusable"] += 1
            return
        self._book_stats["ok"] += 1
        previous = self._micro_ewma.get(ticker)
        # A stale series restarts: its old history must not vouch for new data.
        fresh_start = previous is None or (now - previous[1]) >= ESTIMATE_MAX_AGE_SECONDS
        if fresh_start or self.ewma_half_life <= 0:
            value = micro
        else:
            # Half-life decay: alpha is the weight of the new sample.
            alpha = 1.0 - 0.5 ** ((now - previous[1]) / self.ewma_half_life)
            value = previous[0] + alpha * (micro - previous[0])
        self._micro_ewma[ticker] = (value, now)
        if fresh_start:
            self._micro_history[ticker] = (now, 1)
        else:
            first_at, samples = self._micro_history.get(ticker, (now, 0))
            self._micro_history[ticker] = (first_at, samples + 1)

    def _fetch_book(self, ticker: str, now: float) -> Optional[dict]:
        """Fetch + record one market's order book; ``None`` on failure."""
        get_orderbook = getattr(self.client, "get_orderbook", None)
        if get_orderbook is None:
            return None
        self._book_polled_at[ticker] = now
        try:
            summary = money.orderbook_summary(get_orderbook(ticker), LEVEL_DECAY_CENTS)
        except Exception as exc:
            logger.warning("Orderbook fetch for %s failed: %s", ticker, exc)
            self._book_stats["failed"] += 1
            return None
        self._note_book(ticker, summary, now)
        return summary

    def _update_book_estimates(self, markets: List[MarketView]) -> None:
        """Fetch order books, hottest markets first, then background coverage.

        A cheap mid-based pass scores every market; those within
        ``PREFILTER_SLACK_CENTS`` of the edge bar get first claim on the
        per-tick fetch budget (at the fast ``book_poll_seconds`` cadence).
        Whatever budget is left round-robins the rest of the universe at a
        slower cadence, so the whole board stays estimated without ever
        bursting past the rate limit.
        """
        mids = {
            m.ticker: mid
            for m in markets
            if informative(m) and (mid := mid_price_cents(m)) is not None
        }
        rough = renormalized_estimates(markets, mids)
        scored: List[Tuple[float, MarketView]] = []
        for market in markets:
            estimate = rough.get(market.ticker)
            if estimate is None:
                continue
            mid = mids.get(market.ticker)
            if mid is None or not (RAIL_MIN_CENTS <= mid <= RAIL_MAX_CENTS):
                # Rail-priced (settled-in-all-but-name) buckets: their NO side
                # always *looks* near-edge on the mid but can never clear the
                # bar net of fees -- don't let them crowd the fetch queue.
                continue
            edges = [
                cand.edge_cents
                for side in ("yes", "no")
                for evaluate in (evaluate_side, evaluate_maker_side)
                if (cand := evaluate(market, side, estimate, self.cfg.portfolio_fraction))
            ]
            if edges and max(edges) >= MIN_EDGE_CENTS - PREFILTER_SLACK_CENTS:
                scored.append((max(edges), market))

        now = time.time()
        # Budget counts *attempts* (rate-limit pressure), not successes.
        fetched = 0
        hot = set()
        for _, market in sorted(scored, key=lambda pair: -pair[0]):
            if fetched >= BOOK_FETCH_BUDGET_PER_TICK:
                return
            hot.add(market.ticker)
            if (now - self._book_polled_at.get(market.ticker, 0.0)) < self.book_poll_seconds:
                continue
            self._fetch_book(market.ticker, now)
            fetched += 1

        if self._scanned_this_tick:
            # The scan's per-series requests already used this second's read
            # allowance; background coverage can wait a tick.
            return
        # Leftover budget widens coverage: round-robin the rest of the
        # universe, least-recently-polled first, so estimates exist (and are
        # mature) *before* a market drifts toward an edge.
        backlog: List[Tuple[float, MarketView]] = []
        for market in markets:
            if market.ticker in hot:
                continue
            mid = mids.get(market.ticker)
            if mid is not None and not (RAIL_MIN_CENTS <= mid <= RAIL_MAX_CENTS):
                continue  # settled in all but name; its book adds nothing
            polled = self._book_polled_at.get(market.ticker, 0.0)
            cadence = self.background_poll_seconds
            cached = self._book_cache.get(market.ticker)
            if cached is not None and (cached[0]["bid"] is None or cached[0]["ask"] is None):
                cadence = UNINFORMATIVE_POLL_SECONDS
            if (now - polled) >= cadence:
                backlog.append((polled, market))
        for polled, market in sorted(backlog, key=lambda pair: pair[0]):
            if fetched >= BOOK_FETCH_BUDGET_PER_TICK:
                break
            self._fetch_book(market.ticker, now)
            fetched += 1

    # -- entry ---------------------------------------------------------------
    def _maybe_enter(self, markets: List[MarketView]) -> None:
        free = self.cfg.max_positions - self._occupied_slots(markets)
        pending_makers = [
            t for t in self.trades if t.state is TradeState.BUYING and t.maker
        ]
        if free <= 0 and not pending_makers:
            return
        self._update_book_estimates(markets)
        now = time.time()
        blocked = {t.ticker for t in self.trades}  # never double-up on a market
        blocked.update(
            ticker
            for ticker, ts in self._exited_at.items()
            if (now - ts) < REENTRY_COOLDOWN_SECONDS  # recently force-exited
        )
        # One position per event: same-event "edges" share one estimate (and
        # one failure mode -- an event-level renormalization error shows up as
        # several phantom edges at once), and same-event YES buys are mutually
        # exclusive outcomes anyway.
        held_events = {t.event for t in self.trades if t.event}
        estimates = self._tradable_estimates(markets)

        def allowed(cand: TradeCandidate) -> bool:
            return (
                cand.market.ticker not in blocked
                and cand.market.event_ticker not in held_events
            )

        taker = best_candidate([
            c
            for c in select_trade_candidates(
                markets, estimates=estimates,
                portfolio_fraction=self.cfg.portfolio_fraction,
            )
            if allowed(c)
        ])

        if free <= 0:
            # Every slot is busy, but a resting maker bid yields to a live
            # taker edge: a certain fill at >= the bar beats a maybe-fill.
            if taker is None:
                return
            victim = min(pending_makers, key=lambda t: t.buy_placed_at)
            logger.info(
                "Preempting resting bid %s %s @ %sc for taker edge %s %s",
                victim.ticker, victim.side.upper(), victim.buy_price,
                taker.market.ticker, taker.side.upper(),
            )
            self._cancel(victim.buy_order)
            self.trades.remove(victim)
            self._enter(taker, maker=False)
            return

        if taker is not None:
            self._enter(taker, maker=False)
            return
        # No taker edge: rest a fee-free bid one tick inside the spread of the
        # best maker candidate instead. The spread becomes income, not a cost.
        maker = best_candidate([
            c
            for c in select_maker_candidates(
                markets, estimates=estimates,
                portfolio_fraction=self.cfg.portfolio_fraction,
            )
            if allowed(c)
        ])
        if maker is not None:
            self._enter(maker, maker=True)

    def _enter(self, best: TradeCandidate, maker: bool) -> None:
        # Fractional (0.01-contract) sizing deploys the budget almost exactly;
        # the legacy order schema only takes whole contracts.
        fractional = self.cfg.order_api != "legacy"
        balance = self._budget_balance_cents()
        count = position_size(balance, best.fraction, best.price_cents, fractional=fractional)
        count = money.floor_contracts(self._cap_by_book_depth(best, count), fractional)
        if count < (0.01 if fractional else 1):
            logger.info(
                "Candidate %s %s found but size is 0 (balance %sc, book too thin)",
                best.market.ticker, best.side.upper(), balance,
            )
            return
        self._open_trade(best, count, balance, maker=maker)

    def _cap_by_book_depth(self, cand: TradeCandidate, count: float) -> float:
        """Never size beyond what the book can fill -- entry fills against one
        side's resting orders and the eventual exit needs the other side."""
        cached = self._book_cache.get(cand.market.ticker)
        if cached is None:
            return count
        summary, _ = cached
        if cand.side == "yes":
            entry_depth, exit_depth = summary["no_total"], summary["yes_total"]
        else:
            entry_depth, exit_depth = summary["yes_total"], summary["no_total"]
        return min(count, entry_depth, exit_depth)

    def _open_trade(self, cand: TradeCandidate, count: float, balance: int, maker: bool = False) -> None:
        logger.info(
            "ENTER %s %s %s | est %.1f%% vs cost %.1fc (edge %+.1fc, growth %+.4f) | "
            "buying %g @ %dc (%.0f%% of %sc balance)",
            "MAKER" if maker else "TAKER",
            cand.market.ticker, cand.side.upper(), cand.chance_cents, cand.cost_cents,
            cand.edge_cents, cand.growth, count, cand.price_cents,
            cand.fraction * 100, balance,
        )
        trade = Trade(
            ticker=cand.market.ticker, side=cand.side, count=count,
            buy_price=cand.price_cents, event=cand.market.event_ticker,
            maker=maker,
        )

        if self.cfg.dry_run:
            if maker:
                # The bid rests; the paper fill happens when the market trades
                # down through it (see _trade_check_buy).
                trade.buy_placed_at = time.time()
                trade.state = TradeState.BUYING
            else:
                self._paper_fill_entry(trade)
        else:
            trade.buy_order = self.client.create_order(
                ticker=cand.market.ticker,
                is_buy=True,
                count=count,
                price_cents=cand.price_cents,
                time_in_force="good_till_canceled",
                side=cand.side,
            )
            trade.buy_placed_at = time.time()
            trade.state = TradeState.BUYING
        self.trades.append(trade)

    def _entry_cost_cents(self, trade: Trade) -> Optional[float]:
        """All-in entry cost per contract: makers pay no fee, takers do.
        ``None`` for adopted positions with an unknown entry price."""
        if trade.cost_cents:
            return trade.cost_cents
        if trade.buy_price is None:
            return None
        fee = 0.0 if trade.maker else taker_fee_cents(trade.buy_price)
        return float(trade.buy_price) + fee

    def _paper_fill_entry(self, trade: Trade) -> None:
        trade.cost_cents = self._entry_cost_cents(trade) or 0.0
        self._paper_balance -= trade.cost_cents * trade.count
        logger.info(
            "[PAPER] filled buy %g %s %s @ %dc (%s, all-in %.2fc)",
            trade.count, trade.ticker, trade.side.upper(), trade.buy_price,
            "maker" if trade.maker else "taker", trade.cost_cents,
        )
        self._begin_holding(trade)

    def _paper_close(self, trade: Trade, value_cents: float, reason: str) -> bool:
        proceeds = value_cents * trade.count
        self._paper_balance += proceeds
        pnl = (value_cents - trade.cost_cents) * trade.count if trade.cost_cents else 0.0
        self._paper_pnl += pnl
        self._paper_closed += 1
        logger.info(
            "[PAPER] %s %s closed (%s): %.2fc/contract x %g -> %+.0fc | session P&L %+.0fc",
            trade.ticker, trade.side.upper(), reason, value_cents, trade.count,
            pnl, self._paper_pnl,
        )
        return True

    def _begin_holding(self, trade: Trade) -> None:
        logger.info(
            "Holding %g %s %s; exits: take-profit on convergence, depth <= %.1fx position, "
            "edge reversal >= %.0fc, else settle",
            trade.count, trade.ticker, trade.side.upper(),
            LIQUIDITY_EXIT_BUFFER, EXIT_EDGE_CENTS,
        )
        trade.state = TradeState.HOLDING

    # -- per-position management ------------------------------------------
    def _manage_trade(self, trade: Trade, markets: List[MarketView]) -> bool:
        """Advance one position. Returns True when it is finished (flat) and
        should be dropped from the active list."""
        if trade.state is TradeState.BUYING:
            return self._trade_check_buy(trade, markets)
        if trade.state is TradeState.HOLDING:
            return self._trade_manage_holding(trade, markets)
        if trade.state is TradeState.OFFERING:
            return self._trade_manage_offering(trade, markets)
        if trade.state is TradeState.EXITING:
            return self._trade_check_exit(trade, markets)
        return False

    def _held_for(self, trade: Trade) -> float:
        """Contracts currently held for this trade's side (live mode).

        Kalshi reports a signed position: positive = YES, negative = NO.
        """
        signed = float(self.client.get_position_contracts(trade.ticker))
        held = signed if trade.side == "yes" else -signed
        return max(0.0, held)

    def _trade_check_buy(self, trade: Trade, markets: List[MarketView]) -> bool:
        timeout = self.maker_buy_timeout if trade.maker else self.buy_timeout
        timed_out = (time.time() - trade.buy_placed_at) >= timeout

        if self.cfg.dry_run:
            # Paper maker fill: the market trading down through the resting
            # bid is the only fill observable from quotes alone. This is the
            # pessimistic (purely adversely-selected) case -- real fills also
            # come from uninformed sellers hitting the bid -- so paper results
            # UNDERSTATE the maker path rather than flattering it.
            ask = self._side_ask(self._market_for(markets, trade.ticker), trade.side)
            if ask is not None and trade.buy_price is not None and ask <= trade.buy_price:
                self._paper_fill_entry(trade)
                return False
            if timed_out:
                logger.info(
                    "[PAPER] resting bid %s %s @ %sc did not fill within %ss; cancelled",
                    trade.ticker, trade.side.upper(), trade.buy_price, timeout,
                )
                return True
            return False

        held = self._held_for(trade)
        if held >= trade.count - 0.005:  # full fill (within fixed-point rounding)
            trade.buy_order = None
            trade.cost_cents = self._entry_cost_cents(trade) or 0.0
            logger.info("Buy fully filled %g %s %s; entering HOLDING", held, trade.ticker, trade.side.upper())
            self._begin_holding(trade)
            return False
        if held > 0 and timed_out:
            # Lock in whatever filled and stop trying to acquire more.
            self._cancel(trade.buy_order)
            trade.buy_order = None
            trade.count = held
            trade.cost_cents = self._entry_cost_cents(trade) or 0.0
            logger.info("Buy filled %g %s %s; entering HOLDING", held, trade.ticker, trade.side.upper())
            self._begin_holding(trade)
            return False
        if timed_out:
            logger.info("Buy for %s did not fill within %ss; cancelling", trade.ticker, timeout)
            self._cancel(trade.buy_order)
            return True
        return False

    def _trade_manage_holding(self, trade: Trade, markets: List[MarketView]) -> bool:
        # The preferred exit is the take-profit harvest (sell once the market
        # has converged and paid the edge, freeing the slot for the next one);
        # liquidity and edge-reversal exits protect the downside, and only a
        # position the market never pays for is carried through to settlement.
        market = self._market_for(markets, trade.ticker)
        if market is None:
            # Closed or temporarily missing from the scan: nothing actionable,
            # but drop the trade once settlement has flattened the position.
            # (Several consecutive misses, so one failed series fetch doesn't
            # count as a close.)
            trade.missing_scans += 1
            if self.cfg.dry_run and trade.missing_scans >= 3:
                # Settled away from observation: credit the ledger at the last
                # sellable mark (conservative -- a ridden winner settles at
                # 100c but is booked at its final observed bid).
                value = trade.last_mark_cents
                if value is None:
                    value = trade.cost_cents
                return self._paper_close(trade, value, "settled, marked at last bid")
            logger.debug("%s not in the current scan; holding through to settlement", trade.ticker)
            return self._settled_flat(trade)
        trade.missing_scans = 0

        # Poll this position's order book on a cadence; the same fetch feeds
        # the probability estimate and the liquidity measurement.
        now = time.time()
        depth: Optional[float] = None
        if (now - trade.depth_checked_at) >= self.book_poll_seconds:
            trade.depth_checked_at = now
            summary = self._fetch_book(trade.ticker, now)
            if summary is not None:
                depth = summary["yes_total"] if trade.side == "yes" else summary["no_total"]
                trade.bid_depth = depth

        # Track the latest sellable value (bid net of exit fee): it is the
        # paper mark for any exit and the trigger input for the rest.
        estimate = self._estimates(markets).get(trade.ticker)
        side_bid = self._side_bid(market, trade.side)
        sell_value: Optional[float] = None
        if side_bid is not None and 1 <= side_bid <= 99:
            sell_value = side_bid - taker_fee_cents(side_bid)
            trade.last_mark_cents = sell_value

        # 1) Liquidity exit: sell while the book still has enough depth on our
        #    exit side to actually fill the position. An empty book (depth 0)
        #    is unsellable -- hold quietly instead of spamming doomed orders.
        if depth is not None and 0 < depth <= trade.count * LIQUIDITY_EXIT_BUFFER:
            logger.info(
                "%s %s depth %.0f <= %.1fx position (%g) -- selling before liquidity runs out",
                trade.ticker, trade.side.upper(), depth, LIQUIDITY_EXIT_BUFFER, trade.count,
            )
            return self._force_exit(trade, "liquidity")

        # 2) Edge reversal: the bid now overprices our side vs the estimate --
        #    selling captures more value than holding, win or lose.
        if estimate is not None and sell_value is not None:
            chance = estimate if trade.side == "yes" else 100.0 - estimate
            if sell_value - chance >= EXIT_EDGE_CENTS:
                logger.info(
                    "%s %s bid %dc nets %.1fc vs est %.1f%% -- selling (edge reversal)",
                    trade.ticker, trade.side.upper(), side_bid, sell_value, chance,
                )
                return self._force_exit(trade, "edge-reversal")

            # 3) Take-profit: the market converged and paid the edge. Selling
            #    now banks a real gain over the all-in entry cost, and holding
            #    to settlement would add almost nothing in expectation --
            #    recycle the bankroll into the next dislocation instead. No
            #    re-entry cooldown: the entry bar guards against churn.
            cost_basis = self._entry_cost_cents(trade)
            if cost_basis is not None:
                locked = sell_value - cost_basis
                remaining = chance - sell_value
                if locked >= TAKE_PROFIT_MIN_GAIN_CENTS and remaining < TAKE_PROFIT_REMAINING_CENTS:
                    # Prefer harvesting as a maker: an offer resting inside
                    # the spread sells higher AND fee-free. Taker fallback
                    # when the spread leaves no room.
                    offer = self._exit_offer_level(market, trade.side, chance)
                    if offer is not None:
                        return self._post_exit_offer(trade, offer, locked, remaining)
                    logger.info(
                        "%s %s bid %dc locks %+.1fc over %.1fc cost; holding adds only %.1fc "
                        "-- taking profit",
                        trade.ticker, trade.side.upper(), side_bid, locked, cost_basis, remaining,
                    )
                    return self._force_exit(trade, "take-profit", cooldown=False)
        return False

    @staticmethod
    def _exit_offer_level(market: MarketView, side: str, chance: float) -> Optional[int]:
        """Where to rest a take-profit offer: as high as the book allows while
        staying at/above fair value, one tick inside the spread. ``None`` when
        the spread leaves no room (taker is then the only exit)."""
        quotes = side_quotes(market, side)
        bid, ask = quotes["bid"], quotes["ask"]
        if bid is None or ask is None or ask - bid < 2:
            return None
        level = min(ask - 1, max(bid + 1, math.ceil(chance)))
        if not (1 <= level <= 99):
            return None
        return int(level)

    def _post_exit_offer(self, trade: Trade, level: int, locked: float, remaining: float) -> bool:
        trade.exit_offer_price = level
        trade.exit_offer_at = time.time()
        if not self.cfg.dry_run:
            trade.exit_order = self.client.create_order(
                ticker=trade.ticker, is_buy=False, count=trade.count,
                price_cents=level, time_in_force="good_till_canceled",
                side=trade.side,
            )
        logger.info(
            "%s %s converged (locks %+.1fc at bid, %.1fc left to earn) -- offering %g @ %dc "
            "(maker, fee-free; taker fallback in %.0fs)",
            trade.ticker, trade.side.upper(), locked, remaining,
            trade.count, level, self.exit_offer_timeout,
        )
        trade.state = TradeState.OFFERING
        return False

    def _trade_manage_offering(self, trade: Trade, markets: List[MarketView]) -> bool:
        """A take-profit offer is resting in the book: detect its fill, keep
        the defensive exits armed, and fall back to the bid on timeout."""
        market = self._market_for(markets, trade.ticker)
        if market is None:
            # Market vanished mid-offer: pull the order and let the HOLDING
            # logic handle settlement/disappearance accounting.
            self._cancel(trade.exit_order)
            trade.exit_order = None
            trade.state = TradeState.HOLDING
            return False
        trade.missing_scans = 0
        now = time.time()

        if self.cfg.dry_run:
            bid = self._side_bid(market, trade.side)
            if (
                bid is not None and trade.exit_offer_price is not None
                and bid >= trade.exit_offer_price
            ):
                # The bid rose to (or through) the offer: it filled.
                return self._paper_close(trade, float(trade.exit_offer_price), "take-profit maker")
        else:
            held = self._held_for(trade)
            if held <= 0:
                logger.info(
                    "%s %s offer filled at %sc; flat",
                    trade.ticker, trade.side.upper(), trade.exit_offer_price,
                )
                return True
            trade.count = held  # manage only what remains after partial fills

        # Defensive exits stay armed while the offer rests.
        estimate = self._estimates(markets).get(trade.ticker)
        side_bid = self._side_bid(market, trade.side)
        sell_value: Optional[float] = None
        chance: Optional[float] = None
        if side_bid is not None and 1 <= side_bid <= 99:
            sell_value = side_bid - taker_fee_cents(side_bid)
            trade.last_mark_cents = sell_value
        if estimate is not None:
            chance = estimate if trade.side == "yes" else 100.0 - estimate
        if sell_value is not None and chance is not None and sell_value - chance >= EXIT_EDGE_CENTS:
            self._cancel(trade.exit_order)
            trade.exit_order = None
            return self._force_exit(trade, "edge-reversal")

        if (now - trade.exit_offer_at) >= self.exit_offer_timeout:
            # The patient attempt had its window. If the bid still locks the
            # gain, take it; otherwise re-evaluate from HOLDING next tick.
            self._cancel(trade.exit_order)
            trade.exit_order = None
            cost_basis = self._entry_cost_cents(trade)
            if (
                sell_value is not None and chance is not None and cost_basis is not None
                and sell_value - cost_basis >= TAKE_PROFIT_MIN_GAIN_CENTS
                and chance - sell_value < TAKE_PROFIT_REMAINING_CENTS
            ):
                logger.info(
                    "%s %s offer at %sc unfilled for %.0fs; selling at the bid instead",
                    trade.ticker, trade.side.upper(), trade.exit_offer_price,
                    self.exit_offer_timeout,
                )
                return self._force_exit(trade, "take-profit", cooldown=False)
            trade.state = TradeState.HOLDING
        return False

    def _settled_flat(self, trade: Trade) -> bool:
        """True once a position in a closed/vanished market has settled away
        (live mode only). Throttled -- this is the only API call for such trades."""
        if self.cfg.dry_run:
            return False
        now = time.time()
        if (now - trade.depth_checked_at) < max(self.book_poll_seconds, 30.0):
            return False
        trade.depth_checked_at = now
        try:
            held = self._held_for(trade)
        except Exception as exc:
            logger.debug("Settlement check for %s failed: %s", trade.ticker, exc)
            return False
        if held <= 0:
            logger.info("%s settled flat; closing out its tracking", trade.ticker)
            return True
        return False

    def _force_exit(self, trade: Trade, reason: str, cooldown: bool = True) -> bool:
        if cooldown:
            # Defensive exits sit out for a while; a take-profit harvest may
            # re-enter as soon as a fresh edge clears the bar.
            self._exited_at[trade.ticker] = time.time()
        if self.cfg.dry_run:
            # Credit the ledger at the latest sellable mark (bid - fee).
            value = trade.last_mark_cents
            if value is None:
                value = trade.cost_cents  # never marked: close at breakeven
            return self._paper_close(trade, value, reason)

        # Aggressive sell that sweeps all resting bids (market-order semantics).
        trade.exit_order = self.client.create_order(
            ticker=trade.ticker, is_buy=False, count=trade.count,
            market_order=True, side=trade.side,
        )
        logger.info("Force-sell submitted for %g %s %s (%s)", trade.count, trade.ticker, trade.side.upper(), reason)
        trade.state = TradeState.EXITING
        return False

    def _trade_check_exit(self, trade: Trade, markets: List[MarketView]) -> bool:
        held = self._held_for(trade)
        if held <= 0:
            logger.info("Force-sell complete; %s flat", trade.ticker)
            return True
        # Some size couldn't be sold (thin book). Re-sweep aggressively -- but
        # only while there is a bid to sell into; selling into an empty book
        # just fails, so wait quietly for liquidity to return instead.
        if not self._has_exit_liquidity(trade, markets):
            logger.debug("%s still holds %g but the book is empty; waiting for bids", trade.ticker, held)
            return False
        logger.warning("%s still holds %g after force-sell; re-sweeping", trade.ticker, held)
        trade.exit_order = self.client.create_order(
            ticker=trade.ticker, is_buy=False, count=held,
            market_order=True, side=trade.side,
        )
        return False

    # -- slots / liquidity -------------------------------------------------
    def _occupied_slots(self, markets: List[MarketView]) -> int:
        """How many positions count against ``max_positions``.

        A held position whose market has no exit liquidity (no bid on its
        side to sell into) is excluded, so a stuck position can't block
        new trades.
        """
        held_states = (TradeState.HOLDING, TradeState.OFFERING, TradeState.EXITING)
        slots = 0
        for trade in self.trades:
            if trade.state in held_states and not self._has_exit_liquidity(trade, markets):
                continue
            slots += 1
        return slots

    @staticmethod
    def _side_bid(market: Optional[MarketView], side: str) -> Optional[int]:
        if market is None:
            return None
        return side_quotes(market, side)["bid"]

    @staticmethod
    def _side_ask(market: Optional[MarketView], side: str) -> Optional[int]:
        if market is None:
            return None
        return side_quotes(market, side)["ask"]

    def _has_exit_liquidity(self, trade: Trade, markets: List[MarketView]) -> bool:
        bid = self._side_bid(self._market_for(markets, trade.ticker), trade.side)
        return bid is not None and bid > 0

    # -- helpers -----------------------------------------------------------
    def _budget_balance_cents(self) -> int:
        if self.cfg.dry_run or self.client.auth is None:
            # The evolving paper ledger, so dry-run sizing compounds (and
            # shrinks) with results exactly as live sizing would.
            return max(0, int(self._paper_balance))
        try:
            return money.balance_cents(self.client.get_balance())
        except Exception as exc:
            logger.warning("Balance fetch failed (%s); using paper balance for sizing", exc)
            return self.cfg.paper_balance_cents

    @staticmethod
    def _market_for(markets: List[MarketView], ticker: str) -> Optional[MarketView]:
        for market in markets:
            if market.ticker == ticker:
                return market
        return None

    def _cancel(self, order: Optional[dict]) -> None:
        if not order:
            return
        order_id = order.get("order_id")
        if not order_id or self.cfg.dry_run or order_id == "paper":
            return
        try:
            self.client.cancel_order(order_id)
        except Exception as exc:
            logger.warning("Cancel of order %s failed: %s", order_id, exc)

    def _reconcile_existing_positions(self) -> None:
        """Adopt pre-existing positions (either side) on startup so they keep
        being managed across restarts."""
        for pos in self.client.get_positions():
            signed = money.position_contracts(pos)
            if signed == 0:
                continue
            side = "yes" if signed > 0 else "no"
            count = money.floor_contracts(abs(signed))
            ticker = pos.get("ticker", "")
            logger.info("Adopting existing position %s %s x%g; resuming HOLDING", ticker, side.upper(), count)
            trade = Trade(ticker=ticker, side=side, count=count, buy_price=None)
            self._begin_holding(trade)
            self.trades.append(trade)
