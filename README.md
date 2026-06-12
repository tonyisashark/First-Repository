# Kalshi Daily-Temperature Trading Bot

A Python bot that trades Kalshi daily-temperature markets by estimating each
market's **true probability purely from market data** and buying whichever side
— **YES or NO** — offers the greatest genuine edge. The model is self-tuned:
there are no strategy knobs to configure.

- **Probability estimate:** a multi-level, depth-weighted order-book midpoint
  (microprice: resting pressure near the touch pulls the estimate toward the
  opposite quote, deeper levels count at exponentially decaying weight),
  EWMA-smoothed over time, then **renormalized across the event's buckets** —
  mutually exclusive temperature ranges must sum to exactly 100%, and the
  correction is *arbitrage-grounded*: estimates scale down only when the
  event's bids sum above 100¢ (selling every bucket would lock a profit) and
  up only when its asks sum below 100¢, so stale minimum-tick quotes in dead
  events can't fabricate edges. Rail-priced buckets (≤3¢/≥97¢) are pinned —
  they're settlement certainty, not opinion. Books too wide to mean anything
  produce no estimate at all.
- **Entry:** every market is priced on both sides as a taker (YES at the ask,
  NO at `100 − bid`), **including Kalshi's taker fee**. Sides with at least
  **+2¢ net edge** (estimate vs all-in cost) are ranked by **expected
  log-growth of the bankroll**, and the single best one is bought.
- **Size:** the configured bankroll fraction (default **1/3**), automatically
  **capped at the trade's Kelly fraction** (thin edges deploy less) and by the
  order book's visible depth on both the entry and exit sides. Sizing uses
  Kalshi's **fractional contracts (0.01 granularity)**, so the dollar budget is
  deployed almost exactly; markets without fractional trading enabled fall
  back to whole contracts automatically.
- **Exit:** two self-tuning rules — **liquidity** (sell right before the exit
  side's resting depth runs out: depth ≤ 2× position) and **edge reversal**
  (sell when the market's bid overprices the held side by ≥ 3¢ net of the exit
  fee — cashing out beats holding, win or lose). A position that hits neither
  **rides through close and settles**. Sells are only ever attempted while the
  book has a bid; an exited market is not re-entered for 5 minutes.
- **Concurrency:** up to **`MAX_POSITIONS`** positions at once (default 1). A
  position with no exit liquidity doesn't consume a slot, so a stuck position
  can't block new trades.
- **Fast data:** a continuous REST scan plus a realtime WebSocket ticker feed;
  order books are fetched on a budgeted, prefiltered schedule so the bot never
  bursts past API rate limits.

> ⚠️ **Financial risk.** This software places real orders when configured to do
> so. It ships in **demo + dry-run (paper)** mode by default. Read the Safety
> section before going live. Use at your own risk; no warranty.

---

## How the strategy works

```
              ┌────────── scan temperature markets (REST + WebSocket) ──────────┐
              ▼                                                                  │
   estimate true probability            pick the single best side               │
   ───────────────────────────         ───────────────────────────              │
   order-book microprice                YES @ ask  /  NO @ 100−bid              │
   → EWMA smoothing            ──▶      net edge ≥ 2¢ (fees included)   ──▶  BUY (Kelly-capped size)
   → event renormalization              max expected log-growth                  │
                                                                                 ▼
        settle through close  ◀── neither exit hit ──  HOLDING ── depth ≤ 2× position → SELL
                                                          │
                                                          └── bid overprices side ≥ 3¢ → SELL
```

**Why renormalization is the edge:** each temperature event's buckets are
mutually exclusive and exhaustive, so their true probabilities sum to exactly
100%. When the event's resting *bids* sum above 100¢, or its *asks* below,
real money is provably mispriced — and not uniformly across buckets.
Rescaling the microprice by that proven deviation recovers a calibrated
probability and exposes which individual buckets are over- or under-priced —
on either side. Grounding the correction in actual bids/asks (rather than
mid-sums) means stale 1¢/3¢ quotes left in settled events can't fabricate
phantom edges.

**Why expected log-growth (not raw edge) ranks trades:** a 2¢ edge on a 20¢
contract is far more valuable per dollar than a 2¢ edge on a 94¢ contract, but
also more volatile. Log-growth at the actually-deployed fraction weighs both
correctly, and the Kelly cap keeps a thin edge from ever being over-bet.

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

This prints every open market with its volume / bid / ask / estimated chance
and each market's best side + edge. Put the tickers you want into
`TEMPERATURE_SERIES` in `.env`.

## Run

### Desktop GUI

```bash
python -m kalshi_temp_bot gui
```

A control panel with a **Dashboard** (Start/Stop, live state/position/balance,
streaming log) and a **Settings** tab (the handful of operator choices, saved
per-user). It starts in paper mode; flip *Dry run* off and provide credentials
to trade live.

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

Only operator-level choices are configurable (see `.env.example`); the
probability model, edge thresholds, exit rules and timing are self-tuned.

| Variable | Default | Meaning |
|---|---|---|
| `KALSHI_ENV` | `demo` | `demo` or `prod` |
| `DRY_RUN` | `true` | `true` = paper trade (no real orders) |
| `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` | — | API credentials (live trading) |
| `TEMPERATURE_SERIES` | built-in list | comma-separated series tickers to monitor |
| `PORTFOLIO_FRACTION` | `0.3333` | max bankroll fraction per trade (Kelly may deploy less) |
| `MAX_POSITIONS` | `1` | max concurrent positions (no-liquidity ones don't count) |
| `PAPER_BALANCE_CENTS` | `100000` | sizing balance while paper trading without credentials |
| `KALSHI_ORDER_API` | `v2` | `v2` (current) or `legacy` order endpoint |

Fixed, self-tuned internals (for the curious): minimum net edge **2¢**; edge
reversal exit **3¢**; liquidity exit at **2×** position depth; EWMA half-life
**20 s**; estimates stale after **60 s**; spreads wider than **20¢** carry no
information; book-level weight halves every **3¢** from the touch; taker fee
**0.07·P·(1−P)**; books polled every **5 s** (5 fetches/tick budget, tracking
every market within **8¢** of the edge bar); no entry within **5 min** of
close; **5 min** re-entry cooldown after an exit; buy orders cancelled after
**30 s** unfilled.

---

## Design notes & interpretations

- **"True probability" is estimated, not displayed.** The number Kalshi shows
  is just the last trade — stale and movable by a single contract. The bot's
  estimate comes from the whole order book (depth-weighted microprice),
  smoothed over time, and disciplined by the event-level constraint that
  bucket probabilities must sum to 100%.
- **NO is a first-class side.** A bucket whose YES is overpriced is exactly a
  bucket whose NO is underpriced; the bot prices both sides of every market
  and the edge math (including fees) decides. NO orders are expressed to the
  exchange as their YES-equivalents (buy NO at `q` = sell YES at `100−q`) on
  Kalshi's unified book.
- **Fees are part of the price.** Kalshi's taker fee (`0.07·P·(1−P)` per
  contract) is added to the entry cost and subtracted from exit value before
  any edge comparison — a "2¢ edge" is 2¢ *after* fees.
- **"Portfolio" = available cash balance**, refreshed at each entry.
- **Market close:** positions are deliberately carried through close and left
  to settle; only the liquidity and edge-reversal exits ever sell.
- **Force-sell mechanism** (used by the exits): a market order on the legacy
  API, or — since the v2 schema requires a price — an aggressive
  immediate-or-cancel at the book's edge, which sweeps all resting bids.
- **Entry order** is a good-till-canceled limit at the side's current ask,
  sized to the Kelly-capped fraction and the book's visible depth; partial
  fills are kept and managed, and an unfilled order is cancelled after 30 s.
- **Restart safety:** on startup (live mode) the bot adopts any existing
  position — YES or NO — and resumes managing it across restarts.

## Project layout

```
kalshi_temp_bot/
  config.py         operator-level configuration (+ per-user settings save)
  paths.py          per-user config directory (%APPDATA% / ~/.config)
  money.py          dollar/cent unit helpers + order-book summaries
  kalshi_client.py  REST client + RSA request signing + order schemas
  kalshi_ws.py      realtime WebSocket ticker feed (optional accelerator)
  strategy.py       pure, tested decision logic (estimator, edge, growth)
  bot.py            the BUYING→HOLDING→EXITING loop around the strategy
  factory.py        builds a wired bot from config (shared by CLI + GUI)
  gui.py            Tkinter desktop GUI
  main.py           CLI (run / list-markets / balance / gui)
gui_app.py          PyInstaller entry point for the GUI
kalshi_temp_bot.spec / build_windows.bat / installer/  Windows packaging
assets/make_icon.py generates the app icon
tests/              strategy, bot-loop & money unit tests
```

## Disclaimer

This is not financial advice. Trading on Kalshi involves risk of loss. You are
solely responsible for any orders this software places on your account. Verify
behavior thoroughly in demo / dry-run mode first.
