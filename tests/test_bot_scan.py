"""Regression tests for the market-scan throttle.

A scan that returns no markets must NOT cause the bot to re-scan on every tick
(which previously caused a self-amplifying rate-limit storm).
"""

from kalshi_temp_bot import bot as botmod
from kalshi_temp_bot.bot import TradingBot
from kalshi_temp_bot.config import Config


class _CountingClient:
    auth = None

    def __init__(self, markets=None):
        self.calls = 0
        self._markets = markets or []

    def get_markets(self, series_ticker=None, status="open"):
        self.calls += 1
        return list(self._markets)


def _make_bot(client):
    cfg = Config.from_env()
    cfg.dry_run = True
    cfg.temperature_series = ["A"]      # one series -> one request per scan
    cfg.scan_interval_seconds = 5.0
    return TradingBot(client=client, config=cfg)


def test_empty_scan_is_throttled_not_repeated(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(botmod.time, "time", lambda: clock["t"])
    client = _CountingClient(markets=[])  # always empty
    bot = _make_bot(client)

    bot._get_markets()
    assert client.calls == 1            # first scan happens
    bot._get_markets()
    assert client.calls == 1            # within interval + empty -> must NOT re-scan
    clock["t"] += 6                     # advance past the scan interval
    bot._get_markets()
    assert client.calls == 2            # now it re-scans


def test_scan_refreshes_after_interval(monkeypatch):
    clock = {"t": 5000.0}
    monkeypatch.setattr(botmod.time, "time", lambda: clock["t"])
    client = _CountingClient(markets=[{"ticker": "T1", "event_ticker": "E", "volume_fp": "10.00"}])
    bot = _make_bot(client)

    bot._get_markets()
    assert client.calls == 1
    clock["t"] += 2                     # still within the 5s interval
    bot._get_markets()
    assert client.calls == 1
    clock["t"] += 4                     # now past it
    bot._get_markets()
    assert client.calls == 2
