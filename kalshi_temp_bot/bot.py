"""The daily-temperature trading bot.

Manages up to ``max_positions`` concurrent YES positions. Each position runs its
own small lifecycle:

BUYING   a YES buy (limit @ 90c) is working -- waiting for fill(s)
HOLDING  contracts held with a resting YES sell (limit @ 99c) working
EXITING  a forced close-out (aggressive sell) is working

Exit rules, per position:
* take-profit -- the resting sell at 99c fills; or
* stop-loss   -- the YES bid falls to/below ``min_sell_price_cents`` (if > 0); or
* close       -- never carry a position through market close: force-sell
                 ``force_sell_buffer_seconds`` before it.

A position whose market currently has no exit liquidity (no YES bid) does NOT
count against ``max_positions``, so a stuck position can't block new trades.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from . import money
from .config import Config
from .kalshi_client import KalshiClient
from .kalshi_ws import KalshiWebSocket, MarketDataCache
from .strategy import (
    MarketView,
    idle_watch_summary,
    parse_time,
    pick_best_candidate,
    position_size,
    seconds_to_close,
    select_buy_candidates,
)

logger = logging.getLogger(__name__)

# Seconds to wait between successive per-series market requests during a scan,
# so a multi-city scan doesn't burst past Kalshi's rate limit.
SERIES_REQUEST_SPACING = 0.15


class TradeState(Enum):
    BUYING = "buying"
    HOLDING = "holding"
    EXITING = "exiting"


@dataclass
class Trade:
    """A single position and its in-flight orders."""

    ticker: str
    count: int
    buy_price: Optional[int]
    state: TradeState = TradeState.BUYING
    buy_order: Optional[dict] = None
    sell_order: Optional[dict] = None
    exit_order: Optional[dict] = None
    buy_placed_at: float = 0.0


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

        self._market_cache: List[MarketView] = []
        self._last_scan: float = 0.0
        self._last_heartbeat: float = 0.0
        self._current_tickers: List[str] = []
        self._running = False

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
            time.sleep(self.cfg.poll_interval_seconds)

    def stop(self) -> None:
        self._running = False
        if self.ws is not None:
            self.ws.stop()

    # -- main step ---------------------------------------------------------
    def tick(self) -> None:
        markets = self._get_markets()
        self._maybe_heartbeat(markets)
        # Advance each open position; drop the ones that have gone flat.
        self.trades = [t for t in self.trades if not self._manage_trade(t, markets)]
        # Open new positions while we have free slots.
        self._maybe_enter(markets)

    # -- heartbeat ---------------------------------------------------------
    def _maybe_heartbeat(self, markets: List[MarketView]) -> None:
        """Periodically log a status line so an idle (silent) bot is visibly alive.

        Fires immediately on the first tick, then every ``heartbeat_interval_seconds``.
        """
        now = time.time()
        if self._last_heartbeat != 0.0 and (now - self._last_heartbeat) < self.cfg.heartbeat_interval_seconds:
            return
        self._last_heartbeat = now
        try:
            logger.info("Heartbeat: %s", self._heartbeat_message(markets))
        except Exception:  # noqa: BLE001 - a status line must never break the loop
            logger.debug("heartbeat formatting failed", exc_info=True)

    def _heartbeat_message(self, markets: List[MarketView]) -> str:
        watch = idle_watch_summary(
            markets,
            target_yes_price_cents=self.cfg.buy_yes_price_cents,
            volume_threshold_ratio=self.cfg.volume_threshold_ratio,
            scope=self.cfg.max_volume_scope,
            min_seconds_to_close=self.cfg.min_seconds_to_close,
        )
        head = f"{self._occupied_slots(markets)}/{self.cfg.max_positions} slots, {len(self.trades)} position(s)"
        if not self.trades:
            return f"{watch} | {head}"
        briefs = []
        for trade in self.trades[:3]:
            market = self._market_for(markets, trade.ticker)
            bid = market.yes_bid if market else None
            bid_txt = f"{bid}c" if bid is not None else "?"
            briefs.append(f"{trade.ticker} x{trade.count} {trade.state.value} bid {bid_txt}")
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
        if self._last_scan == 0.0 or (now - self._last_scan) >= self.cfg.scan_interval_seconds:
            self._scan_markets()
            self._last_scan = now

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

    # -- entry -------------------------------------------------------------
    def _maybe_enter(self, markets: List[MarketView]) -> None:
        if self._occupied_slots(markets) >= self.cfg.max_positions:
            return
        held = {t.ticker for t in self.trades}
        candidates = [
            c
            for c in select_buy_candidates(
                markets,
                target_yes_price_cents=self.cfg.buy_yes_price_cents,
                volume_threshold_ratio=self.cfg.volume_threshold_ratio,
                scope=self.cfg.max_volume_scope,
                min_seconds_to_close=self.cfg.min_seconds_to_close,
            )
            if c.ticker not in held  # never double-up on the same market
        ]
        best = pick_best_candidate(candidates)
        if best is None:
            return

        balance = self._budget_balance_cents()
        count = position_size(balance, self.cfg.portfolio_fraction, self.cfg.buy_yes_price_cents)
        if count < 1:
            logger.info(
                "Candidate %s found but balance %s c too small to buy at %s c",
                best.ticker, balance, self.cfg.buy_yes_price_cents,
            )
            return
        self._open_trade(best, count, balance)

    def _open_trade(self, market: MarketView, count: int, balance: int) -> None:
        logger.info(
            "ENTER %s | vol=%.0f | buying %d YES @ %dc (sizing from %s c balance)",
            market.ticker, market.volume, count, self.cfg.buy_yes_price_cents, balance,
        )
        trade = Trade(ticker=market.ticker, count=count, buy_price=self.cfg.buy_yes_price_cents)

        if self.cfg.dry_run:
            logger.info("[PAPER] filled buy %d %s @ %dc", count, market.ticker, self.cfg.buy_yes_price_cents)
            self._begin_holding(trade)
        else:
            trade.buy_order = self.client.create_order(
                ticker=market.ticker,
                is_buy=True,
                count=count,
                price_cents=self.cfg.buy_yes_price_cents,
                time_in_force="good_till_canceled",
            )
            trade.buy_placed_at = time.time()
            trade.state = TradeState.BUYING
        self.trades.append(trade)

    def _begin_holding(self, trade: Trade) -> None:
        if self.cfg.dry_run:
            logger.info("[PAPER] resting SELL %d %s @ %dc", trade.count, trade.ticker, self.cfg.sell_yes_price_cents)
            trade.sell_order = {"order_id": "paper", "status": "resting"}
        else:
            trade.sell_order = self.client.create_order(
                ticker=trade.ticker,
                is_buy=False,
                count=trade.count,
                price_cents=self.cfg.sell_yes_price_cents,
                time_in_force="good_till_canceled",
            )
            logger.info("Resting sell placed for %d %s @ %dc", trade.count, trade.ticker, self.cfg.sell_yes_price_cents)
        trade.state = TradeState.HOLDING

    # -- per-position management ------------------------------------------
    def _manage_trade(self, trade: Trade, markets: List[MarketView]) -> bool:
        """Advance one position. Returns True when it is finished (flat) and
        should be dropped from the active list."""
        if trade.state is TradeState.BUYING:
            return self._trade_check_buy(trade)
        if trade.state is TradeState.HOLDING:
            return self._trade_manage_holding(trade, markets)
        if trade.state is TradeState.EXITING:
            return self._trade_check_exit(trade)
        return False

    def _trade_check_buy(self, trade: Trade) -> bool:
        held = int(self.client.get_position_contracts(trade.ticker))
        timed_out = (time.time() - trade.buy_placed_at) >= self.cfg.buy_timeout_seconds

        if held >= trade.count:
            trade.buy_order = None
            logger.info("Buy fully filled %d %s; entering HOLDING", held, trade.ticker)
            self._begin_holding(trade)
            return False
        if held >= 1 and timed_out:
            # Lock in whatever filled and stop trying to acquire more.
            self._cancel(trade.buy_order)
            trade.buy_order = None
            trade.count = held
            logger.info("Buy filled %d %s; entering HOLDING", held, trade.ticker)
            self._begin_holding(trade)
            return False
        if timed_out:
            logger.info("Buy for %s did not fill within %ss; cancelling", trade.ticker, self.cfg.buy_timeout_seconds)
            self._cancel(trade.buy_order)
            return True
        return False

    def _trade_manage_holding(self, trade: Trade, markets: List[MarketView]) -> bool:
        market = self._market_for(markets, trade.ticker)

        # 1) Hard deadline: never carry a position through market close.
        stc = seconds_to_close(market) if market else None
        if stc is not None and stc <= self.cfg.force_sell_buffer_seconds:
            logger.info("%s closes in %.0fs -- force-selling regardless of price", trade.ticker, stc)
            return self._force_exit(trade, "close")
        if market is None:
            logger.warning("%s no longer open -- force-selling to flatten", trade.ticker)
            return self._force_exit(trade, "closed")

        # 2) Stop-loss: sell once the bid falls to/below the floor (if enabled).
        if (
            self.cfg.min_sell_price_cents > 0
            and market.yes_bid is not None
            and market.yes_bid <= self.cfg.min_sell_price_cents
        ):
            logger.info(
                "%s bid %dc <= stop %dc -- selling (stop-loss)",
                trade.ticker, market.yes_bid, self.cfg.min_sell_price_cents,
            )
            return self._force_exit(trade, "stop-loss")

        # 3) Take-profit: the resting 99c sell does the work in live mode.
        if self.cfg.dry_run:
            if market.yes_bid is not None and market.yes_bid >= self.cfg.sell_yes_price_cents:
                logger.info("[PAPER] sell filled %s @ %dc -- position closed", trade.ticker, market.yes_bid)
                return True
            return False
        if int(self.client.get_position_contracts(trade.ticker)) <= 0:
            logger.info("Position %s closed at target %dc", trade.ticker, self.cfg.sell_yes_price_cents)
            return True
        return False

    def _force_exit(self, trade: Trade, reason: str) -> bool:
        # Cancel the resting take-profit sell first so it doesn't compete.
        self._cancel(trade.sell_order)
        trade.sell_order = None

        if self.cfg.dry_run:
            logger.info("[PAPER] %s sold at market (%s) -- position closed", trade.ticker, reason)
            return True

        # Aggressive sell that sweeps all resting bids (market-order semantics).
        trade.exit_order = self.client.create_order(
            ticker=trade.ticker, is_buy=False, count=trade.count, market_order=True,
        )
        logger.info("Force-sell submitted for %d %s (%s)", trade.count, trade.ticker, reason)
        trade.state = TradeState.EXITING
        return False

    def _trade_check_exit(self, trade: Trade) -> bool:
        held = int(self.client.get_position_contracts(trade.ticker))
        if held <= 0:
            logger.info("Force-sell complete; %s flat", trade.ticker)
            return True
        # Some size couldn't be sold (thin book). Re-sweep aggressively.
        logger.warning("%s still holds %d after force-sell; re-sweeping", trade.ticker, held)
        trade.exit_order = self.client.create_order(
            ticker=trade.ticker, is_buy=False, count=held, market_order=True,
        )
        return False

    # -- slots / liquidity -------------------------------------------------
    def _occupied_slots(self, markets: List[MarketView]) -> int:
        """How many positions count against ``max_positions``.

        A HOLDING/EXITING position whose market has no exit liquidity (no YES bid
        to sell into) is excluded, so a stuck position can't block new trades.
        """
        slots = 0
        for trade in self.trades:
            if trade.state in (TradeState.HOLDING, TradeState.EXITING) and not self._has_exit_liquidity(trade, markets):
                continue
            slots += 1
        return slots

    def _has_exit_liquidity(self, trade: Trade, markets: List[MarketView]) -> bool:
        market = self._market_for(markets, trade.ticker)
        return market is not None and market.yes_bid is not None and market.yes_bid > 0

    # -- helpers -----------------------------------------------------------
    def _budget_balance_cents(self) -> int:
        if self.client.auth is None:
            return self.cfg.paper_balance_cents
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
        """Adopt pre-existing YES positions on startup so the no-position-through-
        close rule holds across restarts."""
        for pos in self.client.get_positions():
            count = int(money.position_contracts(pos))
            if count > 0:
                ticker = pos.get("ticker", "")
                logger.info("Adopting existing position %s x%d; resuming HOLDING", ticker, count)
                trade = Trade(ticker=ticker, count=count, buy_price=None)
                self._begin_holding(trade)
                self.trades.append(trade)
