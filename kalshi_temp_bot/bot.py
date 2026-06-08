"""The daily-temperature trading bot: a small state machine.

States
------
IDLE     no position / no orders -- scanning for a buy candidate
BUYING   a YES buy (limit @ 90c) is working -- waiting for fill(s)
HOLDING  contracts held with a resting YES sell (limit @ 99c) working
EXITING  a forced close-out (aggressive sell before market close) is working

Invariants
----------
* Only one trade is ever in flight (enforced by only entering from IDLE).
* No position is carried through market close: as a market nears its close time
  the held position is force-sold regardless of price.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from . import money
from .config import Config
from .kalshi_client import KalshiClient
from .kalshi_ws import KalshiWebSocket, MarketDataCache
from .strategy import (
    MarketView,
    parse_time,
    pick_best_candidate,
    position_size,
    seconds_to_close,
    select_buy_candidates,
)

logger = logging.getLogger(__name__)


class State(Enum):
    IDLE = "idle"
    BUYING = "buying"
    HOLDING = "holding"
    EXITING = "exiting"


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

        self.state = State.IDLE
        self.position: Optional[dict] = None  # {ticker, count, buy_price}
        self.buy_order: Optional[dict] = None
        self.sell_order: Optional[dict] = None
        self.exit_order: Optional[dict] = None
        self._buy_placed_at: float = 0.0

        self._market_cache: List[MarketView] = []
        self._last_scan: float = 0.0
        self._current_tickers: List[str] = []
        self._running = False

    # -- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        self._running = True
        mode = "DRY-RUN (paper)" if self.cfg.dry_run else "LIVE"
        logger.info(
            "Starting bot | env=%s | mode=%s | series=%s",
            self.cfg.env, mode, ",".join(self.cfg.temperature_series),
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
        if self.state is State.IDLE:
            self._try_enter(markets)
        elif self.state is State.BUYING:
            self._check_buy(markets)
        elif self.state is State.HOLDING:
            self._manage_holding(markets)
        elif self.state is State.EXITING:
            self._check_exit()

    # -- market data -------------------------------------------------------
    def _get_markets(self) -> List[MarketView]:
        """Return the current market universe, scanned via REST and overlaid with
        the freshest realtime prices from the WebSocket cache."""
        now = time.time()
        if not self._market_cache or (now - self._last_scan) >= self.cfg.scan_interval_seconds:
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
        for series in self.cfg.temperature_series:
            try:
                raw_markets = self.client.get_markets(series_ticker=series, status="open")
            except Exception as exc:
                logger.warning("Failed to fetch markets for series %s: %s", series, exc)
                continue
            for raw in raw_markets:
                views.append(self._to_view(raw))
        self._market_cache = views
        self._current_tickers = [v.ticker for v in views]
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

    # -- IDLE: look for an entry ------------------------------------------
    def _try_enter(self, markets: List[MarketView]) -> None:
        candidates = select_buy_candidates(
            markets,
            target_yes_price_cents=self.cfg.buy_yes_price_cents,
            volume_threshold_ratio=self.cfg.volume_threshold_ratio,
            scope=self.cfg.max_volume_scope,
            min_seconds_to_close=self.cfg.min_seconds_to_close,
        )
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

        logger.info(
            "ENTER %s | volume=%.0f | buying %d YES @ %dc (1/3 of %s c)",
            best.ticker, best.volume, count, self.cfg.buy_yes_price_cents, balance,
        )
        self.position = {"ticker": best.ticker, "count": count, "buy_price": self.cfg.buy_yes_price_cents}

        if self.cfg.dry_run:
            # Paper trade: assume the resting bid at 90c fills.
            logger.info("[PAPER] filled buy %d %s @ %dc", count, best.ticker, self.cfg.buy_yes_price_cents)
            self._enter_holding()
            return

        self.buy_order = self.client.create_order(
            ticker=best.ticker,
            is_buy=True,
            count=count,
            price_cents=self.cfg.buy_yes_price_cents,
            time_in_force="good_till_canceled",
        )
        self._buy_placed_at = time.time()
        self.state = State.BUYING

    # -- BUYING: wait for fill --------------------------------------------
    def _check_buy(self, markets: List[MarketView]) -> None:
        ticker = self.position["ticker"]
        held = int(self.client.get_position_contracts(ticker))
        timed_out = (time.time() - self._buy_placed_at) >= self.cfg.buy_timeout_seconds

        if held >= 1 and timed_out:
            # Lock in whatever filled and stop trying to acquire more.
            self._cancel(self.buy_order)
            self.buy_order = None
            self.position["count"] = held
            logger.info("Buy filled %d %s; entering HOLDING", held, ticker)
            self._enter_holding()
        elif held >= self.position["count"]:
            self.buy_order = None
            logger.info("Buy fully filled %d %s; entering HOLDING", held, ticker)
            self._enter_holding()
        elif timed_out:
            # No fills within the window -- abandon the entry and re-scan.
            logger.info("Buy for %s did not fill within %ss; cancelling", ticker, self.cfg.buy_timeout_seconds)
            self._cancel(self.buy_order)
            self._reset_to_idle()

    def _enter_holding(self) -> None:
        ticker = self.position["ticker"]
        count = self.position["count"]
        if self.cfg.dry_run:
            logger.info("[PAPER] resting SELL %d %s @ %dc", count, ticker, self.cfg.sell_yes_price_cents)
            self.sell_order = {"order_id": "paper", "status": "resting"}
        else:
            self.sell_order = self.client.create_order(
                ticker=ticker,
                is_buy=False,
                count=count,
                price_cents=self.cfg.sell_yes_price_cents,
                time_in_force="good_till_canceled",
            )
            logger.info("Resting sell placed for %d %s @ %dc", count, ticker, self.cfg.sell_yes_price_cents)
        self.state = State.HOLDING

    # -- HOLDING: wait for 99c, or force-sell before close ----------------
    def _manage_holding(self, markets: List[MarketView]) -> None:
        ticker = self.position["ticker"]
        market = self._market_for(markets, ticker)

        # 1) Hard deadline: never carry a position through market close.
        stc = seconds_to_close(market) if market else None
        if stc is not None and stc <= self.cfg.force_sell_buffer_seconds:
            logger.info("%s closes in %.0fs -- force-selling regardless of price", ticker, stc)
            self._force_sell()
            return
        if stc is None and market is None:
            # Market dropped out of the open set (likely closed). Force-exit.
            logger.warning("%s no longer open -- force-selling to flatten", ticker)
            self._force_sell()
            return

        # 2) Normal exit: the resting 99c sell does the work in live mode.
        if self.cfg.dry_run:
            if market and market.yes_bid is not None and market.yes_bid >= self.cfg.sell_yes_price_cents:
                logger.info("[PAPER] sell filled %s @ %dc -- trade complete", ticker, market.yes_bid)
                self._reset_to_idle()
            return

        if int(self.client.get_position_contracts(ticker)) <= 0:
            logger.info("Position %s closed at target %dc -- trade complete", ticker, self.cfg.sell_yes_price_cents)
            self._reset_to_idle()

    def _force_sell(self) -> None:
        ticker = self.position["ticker"]
        count = self.position["count"]
        # Cancel the resting 99c sell first so it doesn't compete with the exit.
        self._cancel(self.sell_order)
        self.sell_order = None

        if self.cfg.dry_run:
            logger.info("[PAPER] force-sold %d %s at market -- trade complete", count, ticker)
            self._reset_to_idle()
            return

        # Aggressive sell that sweeps all resting bids, guaranteeing we exit
        # whatever liquidity exists before the close (market order semantics).
        self.exit_order = self.client.create_order(
            ticker=ticker,
            is_buy=False,
            count=count,
            market_order=True,
        )
        logger.info("Force-sell submitted for %d %s", count, ticker)
        self.state = State.EXITING

    # -- EXITING: confirm we're flat --------------------------------------
    def _check_exit(self) -> None:
        ticker = self.position["ticker"]
        held = int(self.client.get_position_contracts(ticker))
        if held <= 0:
            logger.info("Force-sell complete; %s flat", ticker)
            self._reset_to_idle()
        else:
            # Some size couldn't be sold (thin book). Re-sweep aggressively.
            logger.warning("%s still holds %d after force-sell; re-sweeping", ticker, held)
            self.exit_order = self.client.create_order(
                ticker=ticker, is_buy=False, count=held, market_order=True,
            )

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

    def _reset_to_idle(self) -> None:
        self.position = None
        self.buy_order = None
        self.sell_order = None
        self.exit_order = None
        self._buy_placed_at = 0.0
        self.state = State.IDLE

    def _reconcile_existing_positions(self) -> None:
        """Adopt a pre-existing YES position on startup so the one-trade rule and
        the no-position-through-close rule hold across restarts."""
        for pos in self.client.get_positions():
            count = int(money.position_contracts(pos))
            if count > 0:
                ticker = pos.get("ticker", "")
                logger.info("Adopting existing position %s x%d; resuming HOLDING", ticker, count)
                self.position = {"ticker": ticker, "count": count, "buy_price": None}
                self._enter_holding()
                return
