from kalshi_bot.models import Position
from kalshi_bot.money import cents
from kalshi_bot.paper import PaperBroker
from kalshi_bot.portfolio import PortfolioService, mark_position, series_of
from tests.conftest import mk

USD = 1_000_000


class NoNetworkClient:
    def get_markets(self, **kwargs):
        raise AssertionError("client should not be called when lookup covers tickers")


def test_mark_position_conservative_sides():
    yes_pos = Position(ticker="T", count=10)
    market = mk("T", yes_bid=cents(40), yes_ask=cents(44))
    assert mark_position(yes_pos, market) == 10 * cents(40)   # marked at bid

    no_pos = Position(ticker="T", count=-5)
    assert mark_position(no_pos, market) == 5 * cents(56)     # NO bid = 1 - yes_ask
    assert mark_position(yes_pos, None) == 0


def test_series_grouping_key():
    assert series_of(mk("KXHIGHNY-26JUN12-B85", event_ticker="KXHIGHNY-26JUN12"),
                     "KXHIGHNY-26JUN12-B85") == "KXHIGHNY"
    assert series_of(None, "INXD-26JUN12-B5400") == "INXD"


def test_paper_equity_and_exposure(state, cfg):
    broker = PaperBroker(state, cfg)
    state.paper_set_position("AAA-E1-M1", 10, 10 * cents(38))
    state.paper_set_position("BBB-E2-M1", -5, 5 * cents(60))
    state.paper_save_order("o1", "CCC-E3-M1", "bid", cents(20), 10, True, False, "mm")

    lookup = {
        "AAA-E1-M1": mk("AAA-E1-M1", event_ticker="AAA-E1",
                        yes_bid=cents(40), yes_ask=cents(44)),
        "BBB-E2-M1": mk("BBB-E2-M1", event_ticker="BBB-E2",
                        yes_bid=cents(56), yes_ask=cents(60)),
        "CCC-E3-M1": mk("CCC-E3-M1", event_ticker="CCC-E3",
                        yes_bid=cents(19), yes_ask=cents(22)),
    }
    service = PortfolioService(cfg, state, NoNetworkClient(), paper=broker)
    view = service.refresh(lookup)

    marks = 10 * cents(40) + 5 * cents(40)     # NO bid on BBB = 1 - 0.60
    escrow = 10 * cents(20)
    assert view.mtm == marks
    assert view.resting_escrow == escrow
    # paper equity must NOT double count escrow (cash was never reserved)
    assert view.equity == 1000 * USD + marks
    assert view.exposure_by_market["CCC-E3-M1"] == escrow
    assert view.exposure_by_event["AAA-E1"] == 10 * cents(40)
    assert view.exposure_by_series["AAA"] == 10 * cents(40)
    assert view.total_exposure == marks + escrow


def test_covered_ask_escrows_nothing_naked_ask_escrows_complement(state, cfg):
    broker = PaperBroker(state, cfg)
    state.paper_set_position("T-E-M", 10, 0)
    # one covered ask (6 <= position) and one naked ask on another ticker
    state.paper_save_order("o1", "T-E-M", "ask", cents(70), 6, True, False, "mm")
    state.paper_save_order("o2", "U-E-M", "ask", cents(30), 4, True, False, "mm")
    lookup = {
        "T-E-M": mk("T-E-M", yes_bid=cents(60), yes_ask=cents(70)),
        "U-E-M": mk("U-E-M", yes_bid=cents(25), yes_ask=cents(33)),
    }
    service = PortfolioService(cfg, state, NoNetworkClient(), paper=broker)
    view = service.refresh(lookup)
    assert view.resting_escrow == 4 * cents(70)   # only the naked NO collateral


def test_strategy_attribution(state, cfg):
    broker = PaperBroker(state, cfg)
    state.record_order("o1", "T-E-M", "longshot", "bid", cents(95), 5, "executed")
    state.paper_set_position("T-E-M", 5, 5 * cents(95))
    lookup = {"T-E-M": mk("T-E-M", yes_bid=cents(95), yes_ask=cents(97))}
    service = PortfolioService(cfg, state, NoNetworkClient(), paper=broker)
    view = service.refresh(lookup)
    used = service.strategy_exposure(view)
    assert used["longshot"] == 5 * cents(95)
