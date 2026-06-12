from kalshi_bot.money import cents
from kalshi_bot.strategies.market_maker import desired_quotes, select_universe
from tests.conftest import mk, simple_book


def liquid(ticker="MM-1", **kw):
    defaults = dict(yes_bid=cents(40), yes_ask=cents(44), volume_24h=10_000,
                    close_in=86_400)
    defaults.update(kw)
    return mk(ticker, **defaults)


def test_select_universe_filters_and_ranks(cfg):
    good = liquid("GOOD")
    bigger = liquid("BIGGER", volume_24h=50_000)
    tight = liquid("TIGHT", yes_bid=cents(42), yes_ask=cents(44))   # 2c spread
    thin = liquid("THIN", volume_24h=100)
    pinny = liquid("PINNY", yes_bid=cents(93), yes_ask=cents(97))
    closing = liquid("CLOSING", close_in=3_600)
    one_sided = liquid("ONESIDED", yes_ask=None)

    out = select_universe([good, bigger, tight, thin, pinny, closing, one_sided],
                          NOW_ := good.close_ts - 86_400, cfg)
    assert [m.ticker for m in out] == ["BIGGER", "GOOD"]

    cfg.mm_top_n = 1
    out = select_universe([good, bigger], NOW_, cfg)
    assert [m.ticker for m in out] == ["BIGGER"]


def test_desired_quotes_join_and_size(cfg):
    market = liquid()
    book = simple_book(cents(40), cents(44), depth=200)
    cap = 200 * 1_000_000          # $200 per-market inventory cap
    plan = desired_quotes(market, book, inventory_value=0, cap=cap, cfg=cfg)
    bid_price, bid_size = plan["bid"]
    ask_price, ask_size = plan["ask"]
    assert bid_price == cents(40) and ask_price == cents(44)      # join, no improve
    assert bid_size == int(cap * cfg.mm_quote_frac) // cents(40)
    assert ask_size == int(cap * cfg.mm_quote_frac) // cents(56)  # NO collateral


def test_desired_quotes_skew_with_inventory(cfg):
    market = liquid()
    book = simple_book(cents(40), cents(46), depth=200)
    cap = 100 * 1_000_000
    flat = desired_quotes(market, book, 0, cap, cfg)
    long_half = desired_quotes(market, book, cap // 2, cap, cfg)
    # long inventory shifts quotes down (sell more eagerly, buy less)
    assert long_half["bid"][0] < flat["bid"][0]
    assert long_half["ask"][0] <= flat["ask"][0]

    maxed = desired_quotes(market, book, cap, cap, cfg)
    assert maxed["bid"] is None and maxed["ask"] is not None      # only reduce
    shorted = desired_quotes(market, book, -cap, cap, cfg)
    assert shorted["ask"] is None and shorted["bid"] is not None


def test_desired_quotes_requires_spread(cfg):
    market = liquid()
    tight = simple_book(cents(42), cents(44), depth=100)
    plan = desired_quotes(market, tight, 0, 100 * 1_000_000, cfg)
    assert plan == {"bid": None, "ask": None}
    plan = desired_quotes(market, simple_book(cents(40), cents(44)), 0, 0, cfg)
    assert plan == {"bid": None, "ask": None}
