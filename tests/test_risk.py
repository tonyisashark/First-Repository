from kalshi_bot.config import Config
from kalshi_bot.money import cents
from kalshi_bot.portfolio import PortfolioView
from kalshi_bot.risk import RiskManager
from tests.conftest import mk

USD = 1_000_000


def view_with(equity_cash: int, **kwargs) -> PortfolioView:
    view = PortfolioView(ts=1_700_000_000, cash=equity_cash, positions={},
                         orders=[], markets={})
    view.equity = equity_cash
    for key, value in kwargs.items():
        setattr(view, key, value)
    return view


def test_kelly_count_known_value(cfg, state):
    risk = RiskManager(cfg, state)
    # p=.96 on a 95.3325c all-in cost: f* ~ 0.143, quarter-Kelly on $1000
    count = risk.kelly_count(0.96, 953_325, USD, 1000 * USD)
    assert count == 37
    assert risk.kelly_count(0.90, 953_325, USD, 1000 * USD) == 0  # negative edge
    assert risk.kelly_count(0.99, 0, USD, 1000 * USD) == 0
    assert risk.kelly_count(0.99, USD, USD, 1000 * USD) == 0


def test_allowance_takes_strictest_cap(cfg, state):
    risk = RiskManager(cfg, state)
    market = mk("AAA-E1-M1", event_ticker="AAA-E1")
    view = view_with(
        1000 * USD,
        exposure_by_market={"AAA-E1-M1": 30 * USD},
        exposure_by_event={"AAA-E1": 30 * USD},
        exposure_by_series={"AAA": 30 * USD},
        total_exposure=30 * USD,
    )
    allowance = risk.allowance(view, market, strategy_budget=400 * USD,
                               strategy_used=0)
    # per-market cap 5% of equity = $50, already $30 -> $20 is binding
    assert allowance == 20 * USD

    # strategy budget binds when smaller
    allowance = risk.allowance(view, market, strategy_budget=35 * USD,
                               strategy_used=30 * USD)
    assert allowance == 5 * USD

    # never negative
    view.exposure_by_market["AAA-E1-M1"] = 60 * USD
    assert risk.allowance(view, market, strategy_budget=400 * USD,
                          strategy_used=0) == 0


def test_daily_halt_sets_and_autoclears_next_day(cfg, state):
    risk = RiskManager(cfg, state)
    risk.assess(view_with(1000 * USD), today="2026-06-12")
    assert risk.entries_blocked("2026-06-12") == (False, [])

    status = risk.assess(view_with(940 * USD), today="2026-06-12")  # -6%
    assert status.halted and any("daily" in r for r in status.reasons)

    # next UTC day: halt expires, anchor resets
    status = risk.assess(view_with(940 * USD), today="2026-06-13")
    assert not status.halted


def test_drawdown_halt_requires_manual_resume(cfg, state):
    risk = RiskManager(cfg, state)
    risk.assess(view_with(1000 * USD), today="2026-06-12")
    status = risk.assess(view_with(840 * USD), today="2026-06-12")  # -16% dd
    assert status.halted
    # even next day it stays halted
    status = risk.assess(view_with(900 * USD), today="2026-06-13")
    assert status.halted
    risk.resume()
    blocked, _ = risk.entries_blocked("2026-06-13")
    assert not blocked


def test_kill_switch(cfg, state):
    risk = RiskManager(cfg, state)
    risk.kill("test")
    assert risk.entries_blocked()[0]
    risk.resume()
    assert not risk.entries_blocked()[0]


def test_resume_rebaselines_instead_of_relatching(cfg, state):
    """After resume, the SAME low equity must not re-trip the breakers."""
    risk = RiskManager(cfg, state)
    risk.assess(view_with(1000 * USD), today="2026-06-12")
    status = risk.assess(view_with(700 * USD), today="2026-06-12")  # -30%
    assert status.halted                       # daily + drawdown both latched

    risk.resume()
    status = risk.assess(view_with(700 * USD), today="2026-06-12")
    assert not status.halted
    assert status.day_anchor == 700 * USD      # current equity is the new anchor
    assert status.hwm == 700 * USD             # ...and the new high-water mark
    assert status.drawdown_frac == 0.0


def test_paper_baselines_never_contaminate_live(cfg, state):
    """The reported bug: $1,000 paper run, then live mode with a $100 balance
    must not be perceived as a 90% loss."""
    paper_risk = RiskManager(cfg, state)                      # dry_run=True
    paper_risk.assess(view_with(1000 * USD), today="2026-06-12")

    live_cfg = Config(env="prod", dry_run=False,
                      state_db_path=cfg.state_db_path)
    live_risk = RiskManager(live_cfg, state)
    assert live_risk.namespace != paper_risk.namespace

    status = live_risk.assess(view_with(100 * USD), today="2026-06-12")
    assert not status.halted
    assert status.day_anchor == 100 * USD
    assert status.hwm == 100 * USD

    # and halts stay scoped: killing paper does not block live, or vice versa
    paper_risk.kill("paper experiment")
    assert paper_risk.entries_blocked()[0]
    assert not live_risk.entries_blocked()[0]


def test_clamp_order(cfg, state):
    risk = RiskManager(cfg, state)
    market = mk("T")
    assert risk.clamp_order(market, cents(50), 0) == (0, "zero size")
    count, reason = risk.clamp_order(market, cents(50), 999_999)
    assert count == cfg.max_order_contracts and reason is None
    assert risk.clamp_order(market, cents(1), 10)[1]          # below entry floor
    assert risk.clamp_order(market, cents(99), 10)[1]         # above entry ceiling
    assert risk.clamp_order(market, cents(99), 10, reduce_only=True)[1] is None
    assert risk.clamp_order(market, 0, 10, reduce_only=True)[1]


def test_config_validation_guards_live(state, tmp_path):
    cfg = Config(env="prod", dry_run=False, api_key_id="k",
                 private_key_pem="x", state_db_path=str(tmp_path / "s.db"))
    try:
        cfg.validate()
        assert False, "expected refusal without ack"
    except ValueError as err:
        assert "LIVE_TRADING_ACK" in str(err)
    cfg.live_ack = "I_UNDERSTAND_THE_RISKS"
    cfg.validate()  # ok now
