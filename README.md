# Kalshi Daily-Temperature Trading Bot

A Python bot that trades **YES** on Kalshi daily-temperature markets using a
simple, fully-specified rule set:

- **Entry:** buy YES in a temperature *range* market whose volume is **≥ 2/3 of
  the maximum-volume range market**, and whose **estimated chance falls inside
  the configured band** (`BUY_CHANCE_MIN_CENTS`–`BUY_CHANCE_MAX_CENTS`, default
  90–95; 1¢ = 1%). The estimate is built from market data rather than the noisy
  displayed last-trade chance: a depth-weighted order-book midpoint
  (microprice), EWMA-smoothed over time, renormalized across the event's
  buckets (which must sum to ~100%), and gated by a maximum bid/ask spread.
- **Size:** deploy **1/3 of the current portfolio** on each trade.
- **Exit:** liquidity-aware — the bot watches the order book and **sells right
  before the bid liquidity needed to exit runs out** (when total YES-bid depth
  falls to ≤ `LIQUIDITY_EXIT_BUFFER` × position size). An optional **stop-loss**
  (`MIN_SELL_PRICE_CENTS`) sells once the bid falls to/below a floor. Sells are
  only ever attempted while the book has at least one bid; a worthless position
  with an empty book is held quietly instead of spamming doomed sell orders.
- **Concurrency:** up to **`MAX_POSITIONS`** positions at once (default 1). A
  position whose market has no exit liquidity (no YES bid) doesn't consume a slot,
  so a stuck position can't block new trades.
- **Settlement:** a position that hits neither exit simply **rides through
  market close and settles** (there is no forced sell before close).
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
   IDLE ──find candidate──▶ BUYING ──filled──▶ HOLDING ──bid depth low──▶ IDLE      │
    ▲   (vol ≥ 2/3 max &     (limit buy at      (watch order-book                    │
    │    est. chance in       the ask, capped    bid depth; stop-loss)                │
    │    90–95% band)         at band max)          │                                 │
    └────────────────────────── sold / settled ◀────┘                                │
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
| `BUY_CHANCE_MIN_CENTS` | `90` | bottom of the buy band for the estimated chance (¢ = %) |
| `BUY_CHANCE_MAX_CENTS` | `95` | top of the buy band (legacy `BUY_CHANCE_CENTS`/`BUY_YES_PRICE_CENTS` seed both) |
| `MAX_SPREAD_CENTS` | `5` | ignore markets whose bid/ask spread is wider than this |
| `CHANCE_SMOOTHING_SECONDS` | `30` | EWMA half-life for the microprice estimate |
| `LIQUIDITY_EXIT_BUFFER` | `2.0` | sell when YES-bid depth ≤ this × position size |
| `LIQUIDITY_POLL_SECONDS` | `5.0` | order-book depth poll cadence per held position |
| `MIN_SELL_PRICE_CENTS` | `0` | stop-loss: sell if YES bid ≤ this (`0` = off; never fires on an empty book) |
| `MAX_POSITIONS` | `1` | max concurrent positions (no-liquidity ones don't count) |
| `VOLUME_THRESHOLD_RATIO` | `0.6667` | fraction of max volume required (2/3) |
| `PORTFOLIO_FRACTION` | `0.3333` | fraction of portfolio per trade (1/3) |
| `MAX_VOLUME_SCOPE` | `global` | `global` or `event` volume comparison |
| `POLL_INTERVAL_SECONDS` | `1.0` | decision-loop cadence |
| `SCAN_INTERVAL_SECONDS` | `5.0` | REST market re-scan cadence |
| `USE_WEBSOCKET` | `true` | realtime ticker updates |
| `MIN_SECONDS_TO_CLOSE` | `300` | don't enter markets closing this soon |
| `KALSHI_ORDER_API` | `v2` | `v2` (current) or `legacy` order endpoint |

---

## Design notes & interpretations

A few points in the spec needed a concrete reading; these are the choices made
(all configurable):

- **"Chance" = an estimate of what the market genuinely believes**, not the
  number Kalshi displays (that's just the last trade, which can be stale or
  moved by a single contract). The estimate is the order book's depth-weighted
  midpoint (Stoikov microprice — heavy bidding pressure pulls it toward the
  ask), smoothed with an EWMA (`CHANCE_SMOOTHING_SECONDS` half-life), then
  renormalized by the sum of the event's bucket midpoints, since mutually
  exclusive buckets must truly sum to 100% (this strips the structural
  overround / longshot bias). Markets whose spread exceeds `MAX_SPREAD_CENTS`
  are skipped entirely — a wide book carries no probability information. The
  entry order is a limit at the YES ask, capped at the top of the band, so the
  bot never pays more than `BUY_CHANCE_MAX_CENTS`.
- **Liquidity exit:** while holding, the bot polls the market's order book and
  sums the resting YES-bid quantity. When that depth falls to or below
  `LIQUIDITY_EXIT_BUFFER × position size`, it sells immediately — capturing the
  ride up while there is still enough liquidity left to actually fill the exit.
- **"Portfolio" = available cash balance.** Since only one trade runs and it
  starts from cash, the cash balance equals portfolio value at entry.
- **Market close:** positions are deliberately carried through close and left
  to settle; only the liquidity exit and the stop-loss ever sell.
- **Force-sell mechanism** (used by those exits): a market order on the legacy
  API, or — since the v2 schema requires a price — an aggressive
  immediate-or-cancel sell at the 1¢ floor, which sweeps all resting bids
  (best price first) to flatten the position.
- **Entry order** is a good-till-canceled limit buy at the ask (capped at the
  band max), sized to 1/3 of the portfolio; partial fills are kept and managed,
  and an unfilled order is cancelled after `BUY_TIMEOUT_SECONDS`.
- **Restart safety:** on startup (live mode) the bot adopts any existing YES
  position and resumes managing it across restarts.

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
