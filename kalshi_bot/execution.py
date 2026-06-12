"""Order gateway: one interface for live (exchange) and paper execution.

Every order placed by any strategy flows through :class:`Executor`, which
applies final risk clamps, enforces halts, snaps prices onto the market's
tick grid, dispatches to the exchange or the paper broker, and records the
order in the state store for attribution and crash recovery.

Results are normalized dicts:
``{"order_id", "filled", "remaining", "avg_price", "fee", "error"}``.
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Optional

from .client import KalshiAPIError, KalshiClient
from .config import Config
from .models import Market, OrderBook
from .paper import PaperBroker
from .risk import RiskManager
from .state import StateStore

logger = logging.getLogger(__name__)


class Executor:
    def __init__(
        self,
        cfg: Config,
        state: StateStore,
        risk: RiskManager,
        client: Optional[KalshiClient] = None,
        paper: Optional[PaperBroker] = None,
    ) -> None:
        if (client is None) == (paper is None):
            raise ValueError("provide exactly one of client (live) or paper broker")
        self.cfg = cfg
        self.state = state
        self.risk = risk
        self.client = client
        self.paper = paper

    @property
    def paper_mode(self) -> bool:
        return self.paper is not None

    # ------------------------------------------------------------------ place
    def place(
        self,
        *,
        strategy: str,
        market: Market,
        side: str,
        price: int,
        count: int,
        time_in_force: str = "good_till_canceled",
        post_only: bool = False,
        reduce_only: bool = False,
        book: Optional[OrderBook] = None,
    ) -> dict:
        price = market.snap(price, up=(side == "ask"))
        count, reject = self.risk.clamp_order(market, price, count,
                                              reduce_only=reduce_only)
        if reject:
            return _rejected(reject)
        if not reduce_only:
            blocked, reasons = self.risk.entries_blocked()
            if blocked:
                return _rejected(f"halted: {'; '.join(reasons)}")

        if self.paper is not None:
            result = self.paper.place(
                ticker=market.ticker, side=side, price=price, count=count,
                time_in_force=time_in_force, post_only=post_only,
                reduce_only=reduce_only, strategy=strategy, book=book,
                notional=market.notional,
            )
        else:
            payload = KalshiClient.order_payload(
                market.ticker, side, price, count,
                time_in_force=time_in_force, post_only=post_only,
                reduce_only=reduce_only,
                client_order_id=f"{strategy[:8]}-{uuid.uuid4()}",
            )
            try:
                result = self.client.create_order(payload)
            except KalshiAPIError as exc:
                logger.warning("order rejected %s %s %d@%s: %s",
                               market.ticker, side, count, price, exc.message)
                self.state.journal("order_rejected", {
                    "ticker": market.ticker, "side": side, "price": price,
                    "count": count, "strategy": strategy, "error": exc.message,
                })
                return _rejected(exc.message)

        if result.get("order_id") and not result.get("error"):
            status = self._status_of(result, time_in_force, post_only)
            self.state.record_order(
                order_id=result["order_id"],
                client_order_id=result.get("client_order_id") or "",
                ticker=market.ticker,
                event_ticker=market.event_ticker,
                series_ticker=(market.event_ticker or market.ticker).split("-", 1)[0],
                strategy=strategy,
                side=side,
                price_micro=price,
                count=count,
                status=status,
                paper=self.paper_mode,
            )
            logger.info("[%s] %s %s %s %d@%s -> filled=%d remaining=%d%s",
                        strategy, "paper" if self.paper_mode else "LIVE",
                        side, market.ticker, count, _fmt(price),
                        result.get("filled", 0), result.get("remaining", 0),
                        " (resting)" if status == "resting" else "")
        elif result.get("error"):
            logger.info("[%s] order not placed %s %s: %s",
                        strategy, side, market.ticker, result["error"])
        return result

    @staticmethod
    def _status_of(result: dict, time_in_force: str, post_only: bool) -> str:
        if result.get("remaining", 0) <= 0:
            return "executed"
        if time_in_force == "good_till_canceled":
            return "resting"
        return "canceled"  # IOC remainder dies at the exchange

    # ------------------------------------------------------------------ batch
    def batch_place(self, strategy: str, legs: List[dict]) -> List[dict]:
        """Place several orders, one request when live. Legs are kwargs for
        :meth:`place` minus ``strategy``."""
        if self.paper is not None or len(legs) == 1:
            return [self.place(strategy=strategy, **leg) for leg in legs]

        prepared = []
        results: List[Optional[dict]] = [None] * len(legs)
        for i, leg in enumerate(legs):
            market: Market = leg["market"]
            side: str = leg["side"]
            price = market.snap(leg["price"], up=(side == "ask"))
            count, reject = self.risk.clamp_order(
                market, price, leg["count"], reduce_only=leg.get("reduce_only", False))
            if reject:
                results[i] = _rejected(reject)
                continue
            if not leg.get("reduce_only"):
                blocked, reasons = self.risk.entries_blocked()
                if blocked:
                    results[i] = _rejected(f"halted: {'; '.join(reasons)}")
                    continue
            payload = KalshiClient.order_payload(
                market.ticker, side, price, count,
                time_in_force=leg.get("time_in_force", "immediate_or_cancel"),
                post_only=leg.get("post_only", False),
                reduce_only=leg.get("reduce_only", False),
                client_order_id=f"{strategy[:8]}-{uuid.uuid4()}",
            )
            prepared.append((i, leg, payload, price, count))

        if prepared:
            try:
                responses = self.client.batch_create_orders([p for _, _, p, _, _ in prepared])
            except KalshiAPIError as exc:
                logger.warning("batch order failed: %s", exc.message)
                for i, _, _, _, _ in prepared:
                    results[i] = _rejected(exc.message)
                responses = []
            for (i, leg, payload, price, count), resp in zip(prepared, responses):
                results[i] = resp
                if resp.get("order_id") and not resp.get("error"):
                    market = leg["market"]
                    self.state.record_order(
                        order_id=resp["order_id"],
                        client_order_id=resp.get("client_order_id") or "",
                        ticker=market.ticker,
                        event_ticker=market.event_ticker,
                        series_ticker=(market.event_ticker or market.ticker).split("-", 1)[0],
                        strategy=strategy,
                        side=leg["side"],
                        price_micro=price,
                        count=count,
                        status=self._status_of(
                            resp, payload["time_in_force"], bool(payload.get("post_only"))),
                        paper=False,
                    )
        return [r if r is not None else _rejected("not attempted") for r in results]

    # ------------------------------------------------------------------ cancel
    def cancel(self, order_id: str) -> bool:
        try:
            if self.paper is not None:
                ok = self.paper.cancel(order_id)
            else:
                self.client.cancel_order(order_id)
                ok = True
        except KalshiAPIError as exc:
            # Already-gone orders are fine; everything else is logged.
            logger.info("cancel %s: %s", order_id, exc.message)
            ok = exc.status == 404
        if ok:
            self.state.set_order_status(order_id, "canceled")
        return ok


def _rejected(reason: str) -> dict:
    return {"order_id": None, "filled": 0, "remaining": 0, "avg_price": None,
            "fee": 0, "error": reason}


def _fmt(price_micro: int) -> str:
    return f"{price_micro / 10_000:.2f}c"
