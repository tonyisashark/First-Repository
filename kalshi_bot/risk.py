"""Risk layer: sizing, exposure caps, drawdown circuit breakers, kill switch.

Nothing opens a position without passing through here. Two halts exist:

- **daily halt** -- equity down more than ``daily_loss_halt_frac`` from the
  UTC-day anchor: no new entries until the next UTC day (auto-clears).
- **manual halt** -- drawdown from the high-water mark beyond
  ``max_drawdown_halt_frac``, or the operator ran ``kalshi-bot kill``:
  persists until ``kalshi-bot resume``.

All baselines and halts are **scoped per mode** (``cfg.mode_key``: paper vs
live, demo vs prod), so a $1,000 paper run can never become the loss
baseline for a smaller live bankroll. ``resume`` clears the halts *and*
re-baselines the day anchor and high-water mark to current equity --
"accept where we are and carry on" -- which is also the right tool after a
deposit or withdrawal shifts equity for reasons that are not trading P&L.

Position sizing uses fractional Kelly on the *estimated* edge, then clamps
to per-market / per-event / per-series / global exposure caps and available
cash. Estimated edges are exactly that -- estimates -- which is why the
default Kelly fraction is a quarter.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import dataclass, field as dc_field
from typing import List, Optional

from .config import Config
from .models import Market
from .portfolio import PortfolioView, series_of
from .state import StateStore

logger = logging.getLogger(__name__)

KV_DAY_ANCHOR = "risk_day_anchor"
KV_HWM = "risk_hwm_equity"
KV_HALT_DAY = "halt_day"
KV_HALT_MANUAL = "halt_manual"


@dataclass
class RiskStatus:
    halted: bool = False
    reasons: List[str] = dc_field(default_factory=list)
    day_anchor: int = 0
    day_pnl: int = 0
    hwm: int = 0
    drawdown_frac: float = 0.0


def utc_day(ts: Optional[int] = None) -> str:
    dt = _dt.datetime.now(_dt.timezone.utc) if ts is None else \
        _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
    return dt.strftime("%Y-%m-%d")


class RiskManager:
    def __init__(self, cfg: Config, state: StateStore) -> None:
        self.cfg = cfg
        self.state = state
        self.namespace = cfg.mode_key

    def _key(self, base: str) -> str:
        return f"{self.namespace}/{base}"

    # ------------------------------------------------------------- breakers
    def assess(self, view: PortfolioView, today: Optional[str] = None) -> RiskStatus:
        today = today or utc_day(view.ts)
        status = RiskStatus()

        anchor = self._day_anchor(view, today)
        status.day_anchor = anchor
        status.day_pnl = view.equity - anchor

        hwm = max(self.state.kv_get_int(self._key(KV_HWM), 0), view.equity)
        self.state.kv_set(self._key(KV_HWM), hwm)
        status.hwm = hwm
        status.drawdown_frac = 1 - (view.equity / hwm) if hwm > 0 else 0.0

        if anchor > 0 and status.day_pnl <= -self.cfg.daily_loss_halt_frac * anchor:
            if self.state.kv_get(self._key(KV_HALT_DAY)) != today:
                self.state.kv_set(self._key(KV_HALT_DAY), today)
                self.state.journal("halt_daily", {
                    "day_pnl_micro": status.day_pnl, "anchor_micro": anchor,
                    "mode": self.namespace})
                logger.warning("DAILY LOSS HALT: day P&L %.2f USD <= -%.1f%% of anchor"
                               " (run `kalshi-bot resume` to re-baseline now)",
                               status.day_pnl / 1e6,
                               self.cfg.daily_loss_halt_frac * 100)

        # equity > 0 guard: a transient bad valuation (API flake marking
        # everything at zero) must not latch the manual halt.
        if status.drawdown_frac >= self.cfg.max_drawdown_halt_frac \
                and hwm > 0 and view.equity > 0:
            if not self.state.kv_get(self._key(KV_HALT_MANUAL)):
                self.state.kv_set(self._key(KV_HALT_MANUAL), "max_drawdown")
                self.state.journal("halt_drawdown", {
                    "drawdown_frac": status.drawdown_frac, "hwm_micro": hwm,
                    "equity_micro": view.equity, "mode": self.namespace})
                logger.error("MAX DRAWDOWN HALT: %.1f%% below high-water mark; "
                             "run `kalshi-bot resume` to re-enable trading",
                             status.drawdown_frac * 100)

        halted, reasons = self.entries_blocked(today)
        status.halted = halted
        status.reasons = reasons
        return status

    def _day_anchor(self, view: PortfolioView, today: str) -> int:
        raw = self.state.kv_get(self._key(KV_DAY_ANCHOR))
        if raw:
            try:
                data = json.loads(raw)
                if data.get("date") == today and int(data.get("equity", 0)) > 0:
                    return int(data["equity"])
            except (ValueError, TypeError):
                pass
        if view.equity > 0:
            self.state.kv_set(self._key(KV_DAY_ANCHOR),
                              json.dumps({"date": today, "equity": view.equity}))
        return view.equity

    def entries_blocked(self, today: Optional[str] = None) -> tuple[bool, List[str]]:
        today = today or utc_day()
        reasons = []
        manual = self.state.kv_get(self._key(KV_HALT_MANUAL))
        if manual:
            reasons.append(f"manual/drawdown halt ({manual})")
        if self.state.kv_get(self._key(KV_HALT_DAY)) == today:
            reasons.append("daily loss halt")
        return bool(reasons), reasons

    def resume(self) -> None:
        """Clear halts AND re-baseline: current equity becomes the new day
        anchor and high-water mark on the next cycle. Without this the same
        stale baseline would re-trip the breakers immediately. Also the right
        call after a deposit/withdrawal moves equity for non-trading reasons.
        """
        self.state.kv_delete(self._key(KV_HALT_MANUAL))
        self.state.kv_delete(self._key(KV_HALT_DAY))
        self.state.kv_delete(self._key(KV_DAY_ANCHOR))
        self.state.kv_delete(self._key(KV_HWM))
        self.state.journal("resume", {"mode": self.namespace, "rebaselined": True})

    def kill(self, reason: str = "operator kill switch") -> None:
        self.state.kv_set(self._key(KV_HALT_MANUAL), reason)
        self.state.journal("halt_manual", {"reason": reason, "mode": self.namespace})

    # --------------------------------------------------------------- sizing
    def kelly_count(self, p: float, cost_per_contract: int, payout: int,
                    equity: int) -> int:
        """Fractional-Kelly contract count for a binary bet.

        ``p`` is the estimated win probability, ``cost_per_contract`` the
        all-in cost (price + expected fee), ``payout`` the settlement value.
        """
        if cost_per_contract <= 0 or cost_per_contract >= payout or equity <= 0:
            return 0
        b = (payout - cost_per_contract) / cost_per_contract
        f_star = p - (1 - p) / b
        if f_star <= 0:
            return 0
        spend = f_star * self.cfg.kelly_fraction * equity
        return int(spend // cost_per_contract)

    def allowance(
        self,
        view: PortfolioView,
        market: Market,
        *,
        strategy_budget: int,
        strategy_used: int,
        cash_reserve_frac: float = 0.02,
    ) -> int:
        """Max additional micro-dollars deployable into ``market`` right now."""
        equity = view.equity
        if equity <= 0:
            return 0
        ticker = market.ticker
        event = market.event_ticker or ticker
        series = series_of(market, ticker)
        remaining = [
            strategy_budget - strategy_used,
            int(self.cfg.max_market_exposure_frac * equity)
            - view.exposure_by_market.get(ticker, 0),
            int(self.cfg.max_event_exposure_frac * equity)
            - view.exposure_by_event.get(event, 0),
            int(self.cfg.max_series_exposure_frac * equity)
            - view.exposure_by_series.get(series, 0),
            int(self.cfg.max_total_exposure_frac * equity) - view.total_exposure,
            int(view.cash * (1 - cash_reserve_frac)),
        ]
        return max(0, min(remaining))

    def clamp_order(self, market: Market, price: int, count: int,
                    *, reduce_only: bool = False) -> tuple[int, Optional[str]]:
        """Final order-level sanity checks. Returns (count, reject_reason)."""
        if count < 1:
            return 0, "zero size"
        if count > self.cfg.max_order_contracts:
            count = self.cfg.max_order_contracts
        if not reduce_only:
            if price < self.cfg.min_entry_price:
                return 0, f"price below floor ({price})"
            if price > self.cfg.max_entry_price:
                return 0, f"price above ceiling ({price})"
        if price <= 0 or price >= market.notional:
            return 0, "price outside (0, notional)"
        return count, None
