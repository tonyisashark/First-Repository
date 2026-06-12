from kalshi_bot.money import cents
from kalshi_bot.strategies.longshot import appraise, scan_candidates
from tests.conftest import NOW, mk


def test_appraise_ev_gate(cfg):
    market = mk("T")
    # 95c ask, +1c edge: EV = 96 - 95 - 0.3325 = +0.6675c  -> tradeable
    p_hat, ev, cost = appraise(market, "yes", cents(95), cfg)
    assert abs(p_hat - 0.96) < 1e-9
    assert ev == 6_675
    assert cost == cents(95) + 3_325

    assert appraise(market, "yes", cents(89), cfg) is None     # below band
    assert appraise(market, "yes", cents(98), cfg) is None     # above band

    cfg.longshot_edge = 0                                       # no assumed edge
    assert appraise(market, "yes", cents(95), cfg) is None      # fees eat it


def test_scan_finds_both_sides_and_filters(cfg):
    yes_fav = mk("YES-FAV", yes_bid=cents(93), yes_ask=cents(95),
                 close_in=2 * 86_400, volume_24h=5_000)
    no_fav = mk("NO-FAV", yes_bid=cents(5), yes_ask=cents(8),
                close_in=2 * 86_400, volume_24h=5_000)   # NO ask at 95c
    thin = mk("THIN", yes_bid=cents(93), yes_ask=cents(95),
              close_in=2 * 86_400, volume_24h=10)
    too_far = mk("FAR", yes_bid=cents(93), yes_ask=cents(95),
                 close_in=30 * 86_400, volume_24h=5_000)
    too_soon = mk("SOON", yes_bid=cents(93), yes_ask=cents(95),
                  close_in=600, volume_24h=5_000)
    midprice = mk("MID", yes_bid=cents(48), yes_ask=cents(52),
                  close_in=2 * 86_400, volume_24h=5_000)

    out = scan_candidates([yes_fav, no_fav, thin, too_far, too_soon, midprice],
                          NOW, cfg)
    found = {(c.market.ticker, c.side) for c in out}
    assert ("YES-FAV", "yes") in found
    assert ("NO-FAV", "no") in found
    assert all(t not in {"THIN", "FAR", "SOON", "MID"} for t, _ in found)
    # EV-sorted descending
    assert out[0].ev >= out[-1].ev
