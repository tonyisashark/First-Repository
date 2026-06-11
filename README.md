# Kalshi Daily-Temperature Trading Bot

A Python bot that trades **YES** on Kalshi daily-temperature markets using a
simple, fully-specified rule set:

- **Entry:** buy YES in a temperature *range* market whose volume is **≥ 2/3 of
  the maximum-volume range market**, but **only when the YES ask is exactly
  90¢**.
- **Size:** deploy **1/3 of the current portfolio** on each trade.
- **Exit:** rest a sell at **exactly 99¢** (take-profit), with an optional
  **stop-loss** (`MIN_SELL_PRICE_CENTS`) that sells once the bid falls to/below a
  floor.
- **Concurrency:** up to **`MAX_POSITIONS`** positions at once (default 1). A
  position whose market has no exit liquidity (no YES bid) doesn't consume a slot,
  so a stuck position can't block new trades.
- **No overnight risk:** if a position hasn't sold by the time the market is
  about to close (midnight), it is **force-sold regardless of price** so nothing
  is held through the close.
- **Fast data:** market data is refreshed continuously via a 1s REST scan plus a
  realtime WebSocket ticker feed.

> ⚠️ **Financial risk.** This software places real orders when configured to do
> so. It ships in **demo + dry-run (paper)** mode by default. Read the Safety
> section before going live. Use at your own risk; no warranty.

---

## How the strategy works

```
                 ┌─────────── scan temperature markets (REST, every ~5s) ───────────┐
                 │            overlay realtime prices (WebSocket ticker)             │
                 ▼                                                                   │
   IDLE ──find candidate──▶ BUYING ──filled──▶ HOLDING ──bid hits 99¢──▶ IDLE       │
    ▲   (vol ≥ 2/3 max &     (limit buy        (resting sell                         │
    │    yes_ask == 90¢)      @ 90¢)            @ 99¢)                                │
    │                                              │                                 │
    └──────────────────── force-sell ◀── near close (≤ 60s to midnight) ────────────┘
```

**"Maximum volume" is a single range market**, *not* an event aggregate. For
example, for "High in New York" on a given day there are many bucket markets
(…, 72–73°, 74–75°, …); the bot compares each bucket's own volume against the
**highest-volume single bucket**, not against the summed volume of the whole NY
event. By default this comparison is **global** (`global` scope): every
monitored range market is measured against the single highest-volume range
market across all monitored markets. Set `MAX_VOLUME_SCOPE=event` to instead
compare within each event (one city/day).

Because only one trade runs at a time, when several markets qualify the bot
enters the **highest-volume** one.

---

## Install

```bash
pip install -r requirements.txt
```

Requires Python 3.9+.

## Get API credentials

1. Log in to Kalshi → **Account → API Keys** → create a key.
2. Save the **Key ID** and download the **RSA private key** (`.pem`).
3. Copy `.env.example` to `.env` and fill in `KALSHI_API_KEY_ID` and
   `KALSHI_PRIVATE_KEY_PATH`.

Market-data commands (`list-markets`) are **public** and need no credentials.

## Verify the markets you'll trade

Series tickers drift over time, so confirm them before running:

```bash
python -m kalshi_temp_bot list-markets --series KXHIGHNY
```

This prints every open market with its volume / bid / ask and flags which ones
currently satisfy the buy rule. Put the tickers you want into
`TEMPERATURE_SERIES` in `.env`.

## Run

### Desktop GUI

```bash
python -m kalshi_temp_bot gui
```

A control panel with a **Dashboard** (Start/Stop, live state/position/balance,
streaming log) and a **Settings** tab (every knob, saved per-user). It starts in
paper mode; flip *Dry run* off and provide credentials to trade live.

**Windows installer:** `build_windows.bat` produces `KalshiTempBotSetup.exe`,
which installs a standalone app (no Python needed), a **Desktop shortcut**, and a
**Start Menu entry** so it's searchable in Windows. See
[INSTALL_WINDOWS.md](INSTALL_WINDOWS.md).

### Command line

```bash
# Paper-trade against live PROD market data (safe: reads are public, no orders):
KALSHI_ENV=prod DRY_RUN=true python -m kalshi_temp_bot run

# Check your account budget:
python -m kalshi_temp_bot balance

# Go live (real money) -- only after you've reviewed everything:
KALSHI_ENV=prod DRY_RUN=false python -m kalshi_temp_bot run
```

In **dry-run** mode the bot reads real prices and *simulates* the full
buy→hold→sell lifecycle in the logs so you can watch the strategy operate
without risking funds.

## Test

```bash
pytest -q
```

The strategy and unit-conversion logic are covered by tests that run without any
network access.

---

## Configuration reference

All settings are environment variables (see `.env.example`). Highlights:

| Variable | Default | Meaning |
|---|---|---|
| `KALSHI_ENV` | `demo` | `demo` or `prod` |
| `DRY_RUN` | `true` | `true` = paper trade (no real orders) |
| `TEMPERATURE_SERIES` | built-in list | comma-separated series tickers to monitor |
| `BUY_YES_PRICE_CENTS` | `90` | exact YES ask required to buy |
| `SELL_YES_PRICE_CENTS` | `99` | resting sell (take-profit) target |
| `MIN_SELL_PRICE_CENTS` | `0` | stop-loss: sell if YES bid ≤ this (`0` = off) |
| `MAX_POSITIONS` | `1` | max concurrent positions (no-liquidity ones don't count) |
| `VOLUME_THRESHOLD_RATIO` | `0.6667` | fraction of max volume required (2/3) |
| `PORTFOLIO_FRACTION` | `0.3333` | fraction of portfolio per trade (1/3) |
| `MAX_VOLUME_SCOPE` | `global` | `global` or `event` volume comparison |
| `POLL_INTERVAL_SECONDS` | `1.0` | decision-loop cadence |
| `SCAN_INTERVAL_SECONDS` | `5.0` | REST market re-scan cadence |
| `USE_WEBSOCKET` | `true` | realtime ticker updates |
| `FORCE_SELL_BUFFER_SECONDS` | `60` | force-sell this long before close |
| `MIN_SECONDS_TO_CLOSE` | `300` | don't enter markets closing this soon |
| `KALSHI_ORDER_API` | `v2` | `v2` (current) or `legacy` order endpoint |

---

## Design notes & interpretations

A few points in the spec needed a concrete reading; these are the choices made
(all configurable):

- **"YES price" for buying = the YES _ask_** (the price you actually pay), and
  **"sell price" = the YES _bid_** (what a buyer will pay you). So the bot buys
  when `yes_ask == 90¢` and the 99¢ sell fills when `yes_bid` reaches 99¢.
- **"Portfolio" = available cash balance.** Since only one trade runs and it
  starts from cash, the cash balance equals portfolio value at entry.
- **"Midnight / market close"** uses each market's actual `close_time` from the
  API. The position is force-sold `FORCE_SELL_BUFFER_SECONDS` *before* that time
  to guarantee the exit lands before the close.
- **Force-sell mechanism:** a market order on the legacy API, or — since the v2
  schema requires a price — an aggressive immediate-or-cancel sell at the 1¢
  floor, which sweeps all resting bids (best price first) to flatten the
  position.
- **Entry order** is a good-till-canceled limit buy at 90¢, sized to 1/3 of the
  portfolio; partial fills are kept and managed, and an unfilled order is
  cancelled after `BUY_TIMEOUT_SECONDS`.
- **Restart safety:** on startup (live mode) the bot adopts any existing YES
  position and resumes managing it, preserving the one-trade and
  no-position-through-close invariants.

## Project layout

```
kalshi_temp_bot/
  config.py         env-driven configuration (+ per-user settings save)
  paths.py          per-user config directory (%APPDATA% / ~/.config)
  money.py          dollar/cent/fixed-point unit helpers
  kalshi_client.py  REST client + RSA request signing + order schemas
  kalshi_ws.py      realtime WebSocket ticker feed (optional accelerator)
  strategy.py       pure, tested decision logic
  bot.py            the IDLE→BUYING→HOLDING→EXITING state machine
  factory.py        builds a wired bot from config (shared by CLI + GUI)
  gui.py            Tkinter desktop GUI
  main.py           CLI (run / list-markets / balance / gui)
gui_app.py          PyInstaller entry point for the GUI
kalshi_temp_bot.spec / build_windows.bat / installer/  Windows packaging
assets/make_icon.py generates the app icon
tests/              strategy & money unit tests
```

## Disclaimer

This is not financial advice. Trading on Kalshi involves risk of loss. You are
solely responsible for any orders this software places on your account. Verify
behavior thoroughly in demo / dry-run mode first.
