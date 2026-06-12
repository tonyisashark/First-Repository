"""Paper-trading broker: simulates Kalshi fills against *live* orderbooks.

Dry-run mode routes every order here instead of the exchange. Taker orders
sweep the real visible book level by level (realistic, ignoring only our own
market impact); resting maker orders fill at their limit price when the live
book crosses them (optimistic: real queues mean you fill later/less, so treat
paper maker P&L as an upper bound). Settlement pays out when the market
reports a result.

Cash/position accounting mirrors Kalshi's collateral model exactly:

    buy YES  c @ p : cash -= p*c                  ; pos += c
    sell YES c @ p : covered part  -> cash += p*c ; naked part -> cash -= (N-p)*c
    (a naked YES sale *is* buying NO at N-p)      ; pos -= c
    settle YES     : cash += N * max(pos, 0)
    settle NO      : cash += N * max(-pos, 0)

where N is the contract notional ($1).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Dict, List, Optional

from .config import Config
from .fees import fill_fee_micro
from .models import Market, Order, OrderBook, Position
from .money import MICRO_PER_DOLLAR
from .state import StateStore

logger = logging.getLogger(__name__)

KV_CASH = "paper_cash_micro"


class PaperBroker:
    def __init__(self, state: StateStore, cfg: Config) -> None:
        self.state = state
        self.cfg = cfg
        if state.kv_get(KV_CASH) is None:
            state.kv_set(KV_CASH, cfg.paper_cash)

    # ----------------------------------------------------------------- views
    @property
    def cash(self) -> int:
        return self.state.kv_get_int(KV_CASH, self.cfg.paper_cash)

    def _set_cash(self, value: int) -> None:
        self.state.kv_set(KV_CASH, int(value))

    def positions(self) -> Dict[str, Position]:
        out: Dict[str, Position] = {}
        for ticker, (count, cost) in self.state.paper_positions().items():
            out[ticker] = Position(ticker=ticker, count=count, exposure=cost)
        return out

    def orders(self) -> List[Order]:
        rows = self.state.paper_orders()
        return [
            Order(order_id=r[0], ticker=r[1], side=r[2], yes_price=int(r[3]),
                  remaining=int(r[4]), initial=int(r[4]), status="resting",
                  created_ts=int(r[8] or 0))
            for r in rows
        ]

    # ----------------------------------------------------------------- trade
    def place(
        self,
        *,
        ticker: str,
        side: str,
        price: int,
        count: int,
        time_in_force: str = "good_till_canceled",
        post_only: bool = False,
        reduce_only: bool = False,
        strategy: str = "",
        book: Optional[OrderBook] = None,
        notional: int = MICRO_PER_DOLLAR,
    ) -> dict:
        order_id = f"paper-{uuid.uuid4()}"
        result = {"order_id": order_id, "client_order_id": order_id, "filled": 0,
                  "remaining": count, "avg_price": None, "fee": 0, "error": None}

        if reduce_only:
            count = min(count, self._reducible(ticker, side))
            result["remaining"] = count
            if count <= 0:
                result.update(error="reduce_only with nothing to reduce", remaining=0)
                return result

        if post_only:
            crossing = self._would_cross(side, price, book)
            if crossing:
                result["error"] = "post_only order would cross the book"
                return result
            self.state.paper_save_order(order_id, ticker, side, price, count,
                                        post_only, reduce_only, strategy)
            return result

        plan: List[tuple] = []
        planned = 0
        if book is not None:
            levels = book.asks_for("yes") if side == "bid" else book.bids_for("yes")
            for level_price, avail in levels:
                if side == "bid" and level_price > price:
                    break
                if side == "ask" and level_price < price:
                    break
                take = min(avail, count - planned)
                if take <= 0:
                    break
                plan.append((level_price, take))
                planned += take
                if planned >= count:
                    break

        if time_in_force == "fill_or_kill" and planned < count:
            result["error"] = "fill_or_kill order cannot be fully filled"
            return result

        filled, spent, fees = 0, 0, 0
        for level_price, take in plan:
            fee = fill_fee_micro(level_price, take, self.cfg.taker_fee_bps, notional)
            self._settle_fill(ticker, side, level_price, take, notional)
            self._set_cash(self.cash - fee)
            self._record_fill(order_id, ticker, side, level_price, take, fee,
                              is_taker=True, strategy=strategy)
            filled += take
            spent += take * level_price
            fees += fee

        remaining = count - filled
        if remaining > 0 and time_in_force == "good_till_canceled":
            self.state.paper_save_order(order_id, ticker, side, price, remaining,
                                        False, reduce_only, strategy)
        result.update(
            filled=filled,
            remaining=remaining,
            avg_price=(spent // filled) if filled else None,
            fee=fees,
        )
        return result

    def cancel(self, order_id: str) -> bool:
        return self.state.paper_delete_order(order_id)

    # ------------------------------------------------------------ lifecycle
    def sync_with_market(self, market: Market, book: Optional[OrderBook]) -> None:
        """Maker fills + settlement for one ticker, given fresh data."""
        if market.status in ("finalized", "determined", "settled") and \
                market.result in ("yes", "no"):
            self._settle_market(market)
            return
        if market.status not in ("active", "open"):
            for order in self.orders():
                if order.ticker == market.ticker:
                    self.cancel(order.order_id)
            return
        if book is None:
            return
        for row in self.state.paper_orders():
            order_id, ticker, side, price, remaining, _post, reduce_only, strategy, _ts = row
            if ticker != market.ticker:
                continue
            price, remaining = int(price), int(remaining)
            if bool(reduce_only):
                cap = self._reducible(ticker, side)
                if cap <= 0:
                    self.cancel(order_id)
                    continue
                remaining = min(remaining, cap)
            fillable = 0
            if side == "bid":
                best = book.best_ask("yes")
                if best is not None and best <= price:
                    fillable = min(remaining, book.depth_at_or_better("yes", price))
            else:
                bids = book.bids_for("yes")
                crossing = sum(c for p, c in bids if p >= price)
                if crossing:
                    fillable = min(remaining, crossing)
            if fillable <= 0:
                continue
            fee = fill_fee_micro(price, fillable, self.cfg.maker_fee_bps, market.notional)
            self._settle_fill(ticker, side, price, fillable, market.notional)
            self._set_cash(self.cash - fee)
            self._record_fill(order_id, ticker, side, price, fillable, fee,
                              is_taker=False, strategy=strategy)
            self.state.paper_update_order(order_id, remaining - fillable)

    def _settle_market(self, market: Market) -> None:
        positions = self.state.paper_positions()
        entry = positions.get(market.ticker)
        for order in self.orders():
            if order.ticker == market.ticker:
                self.cancel(order.order_id)
        if not entry:
            return
        count, cost = entry
        payout = market.notional * (max(count, 0) if market.result == "yes"
                                    else max(-count, 0))
        self._set_cash(self.cash + payout)
        self.state.paper_set_position(market.ticker, 0, 0)
        self.state.journal("paper_settlement", {
            "ticker": market.ticker, "result": market.result, "count": count,
            "cost_micro": cost, "payout_micro": payout, "pnl_micro": payout - cost,
        })
        logger.info("paper settlement %s result=%s count=%+d pnl=%+.2f USD",
                    market.ticker, market.result, count, (payout - cost) / 1e6)

    # ------------------------------------------------------------- internals
    def _reducible(self, ticker: str, side: str) -> int:
        count, _ = self.state.paper_positions().get(ticker, (0, 0))
        return max(-count, 0) if side == "bid" else max(count, 0)

    @staticmethod
    def _would_cross(side: str, price: int, book: Optional[OrderBook]) -> bool:
        if book is None:
            return False
        if side == "bid":
            best = book.best_ask("yes")
            return best is not None and price >= best
        best = book.best_bid("yes")
        return best is not None and price <= best

    def _settle_fill(self, ticker: str, side: str, price: int, count: int,
                     notional: int) -> None:
        pos, cost = self.state.paper_positions().get(ticker, (0, 0))
        cash = self.cash
        if side == "bid":
            close_no = min(count, max(-pos, 0))
            open_yes = count - close_no
            if close_no:
                cash += (notional - price) * close_no
                cost -= cost * close_no // max(-pos, 1)
            cash -= price * open_yes
            cost += price * open_yes
            pos += count
        else:
            cover = min(count, max(pos, 0))
            naked = count - cover
            if cover:
                cash += price * cover
                cost -= cost * cover // max(pos, 1)
            cash -= (notional - price) * naked
            cost += (notional - price) * naked
            pos -= count
        self._set_cash(cash)
        self.state.paper_set_position(ticker, pos, max(cost, 0))

    def _record_fill(self, order_id: str, ticker: str, side: str, price: int,
                     count: int, fee: int, is_taker: bool, strategy: str) -> None:
        self.state.record_fill(
            fill_id=f"paper-{uuid.uuid4()}",
            order_id=order_id,
            ticker=ticker,
            side=side,
            count=count,
            price_micro=price,
            fee_micro=fee,
            is_taker=is_taker,
            ts=int(time.time()),
            strategy=strategy,
        )
