# kalshi-bot

An autonomous, risk-managed trading bot for [Kalshi](https://kalshi.com) event
markets. Point it at an API key, leave it running, and it hunts structural
edges across the whole exchange while compounding its bankroll: **every
position is sized as a fraction of current portfolio equity**, so wins
automatically scale future positions up and losses scale them down.

```
pip install -e .
cp .env.example .env        # add your API key (demo keys are free)
kalshi-bot run              # paper mode by default -- watch it trade risk-free
kalshi-bot gui              # ...or use the desktop control panel
```

Prefer an app? Grab **`KalshiBot.exe`** from the latest
[build-windows-exe workflow artifact](../../actions/workflows/build-windows.yml)
(or build it yourself with `build_windows.bat`) -- no Python required.

> ⚠️ **This is not a money printer.** Prediction markets are competitive,
> fees are real, and any strategy can lose -- including all of these. Nothing
> here is financial advice and no profit is guaranteed or even implied. The
> bot ships in paper mode, requires a triple opt-in to touch real money, and
> carries circuit breakers for a reason. Start in demo, stay small, and only
> trade money you can afford to lose.

---

## How it makes (and protects) money

### The compounding engine

Each cycle the bot computes **equity** = cash + resting-order escrow + a
*conservative* mark-to-market of every position (longs marked at the bid).
Strategy budgets, Kelly sizing, and exposure caps are all fractions of that
live number. There is nothing else to configure: deposit more, and sizes
grow; lose, and the bot automatically de-risks. Equity snapshots are stored
in SQLite so `kalshi-bot status` can show the curve.

### Strategy 1: mutually-exclusive event arbitrage (`arb`)

In a Kalshi event flagged *mutually exclusive* (e.g. "S&P closes in range
X/Y/Z"), exactly one market settles YES. Whenever the books drift so that:

- the sum of YES asks < $1 → **buy every outcome** (the set pays $1 no
  matter what), or
- the sum of YES bids > $1 → **sell every outcome** (all legs but one pay),

the bot locks in the difference, net of taker fees, sized against the
visible depth with a haircut. Long sets additionally require proof the
outcomes are *exhaustive* -- contiguous floor/cap strikes tiling the real
line, or a market-consensus fallback (Σ bids ≥ 97¢). All legs fire in a
single batched IOC request; imbalances are chased once within a slippage
budget, then unwound reduce-only. This is the closest thing to free money on
an exchange, which is precisely why opportunities are rare and small --
the bot simply never sleeps on them.

### Strategy 2: favorite harvesting (`longshot`)

Prediction markets persistently exhibit **favorite-longshot bias**: cheap
lottery tickets trade rich, near-certainties trade cheap. The bot buys the
favorite side (YES *or* NO) when it is priced 90-97¢, resolves within two
weeks, and clears an EV threshold after fees under a configurable
calibration assumption (default: favorites are ~1¢ underpriced).

Don't trust the default -- measure it:

```
kalshi-bot calibrate --days 60
```

pulls every market settled in the window and prints realized vs. implied
frequency by price bucket, with a suggested `LONGSHOT_EDGE_CENTS`. Sizing is
quarter-Kelly on the assumed edge, throttled by hard diversification caps
(2% of equity per market, 8% per event, 20% per series) because the tail
risk of correlated favorites all failing together is the real cost here.

### Strategy 3: passive market making (`mm`)

In the top-N liquid markets with wide spreads and mid-range prices, the bot
rests post-only quotes on both sides, joining (never improving) the touch,
skewing away from accumulated inventory, and capping inventory per market.
Quotes are pulled near close, in tight books, and on any halt. Maker fees
are modeled. **Paper-mode fills for this strategy are optimistic** (no queue
position), so treat simulated MM profits as an upper bound and validate in
demo before allocating real budget.

### The risk layer (always on)

Nothing trades without passing, in order:

| Guard | Default | Effect |
|---|---|---|
| Strategy budget | 30% / 40% / 20% of equity | caps each strategy's deployment |
| Per-market cap | 5% of equity | no single market can hurt much |
| Per-event cap | 8% | bounds event-level wipeouts (incl. arb sets) |
| Per-series cap | 20% | bounds correlated clusters (same city/index/etc.) |
| Global cap | 80% | always keeps dry powder |
| Entry price band | 2¢-98¢ | never chases pin-risk extremes |
| Daily loss halt | -5% from day anchor | stops entries until next UTC day |
| Max drawdown halt | -15% from high-water mark | stops until `kalshi-bot resume` |
| Kill switch | `kalshi-bot kill [--flatten]` | instant manual stop |

Halts cancel resting orders and block all entries; reduce-only exits remain
allowed. State survives restarts (SQLite).

Two details worth knowing:

- **Baselines are scoped per mode** (`paper:demo`, `live:prod`, ...). Paper
  equity never becomes the loss baseline for your live account, so switching
  modes cannot trip a phantom halt.
- **`kalshi-bot resume` re-baselines.** It clears the halts *and* makes
  current equity the new day anchor and high-water mark -- "accept where we
  are, carry on." Use it after any halt you've reviewed, and after a
  **deposit or withdrawal** (an external cash movement looks like P&L to the
  breakers; resume tells the bot it wasn't).

---

## Setup

### 1. Install

```bash
pip install -e .          # Python 3.10+
pytest -q                 # 84 offline tests, no network needed
```

### 2. Credentials

1. Create an account -- **demo**: <https://demo.kalshi.co>, **prod**:
   <https://kalshi.com> (prod requires funding before API keys work).
2. Account → API Keys → create key. Save the **Key ID** and download the
   **RSA private key** `.pem`.
3. `cp .env.example .env`, set `KALSHI_API_KEY_ID` and
   `KALSHI_PRIVATE_KEY_PATH`.

The API requires a key even for market data, so paper mode wants (free)
demo credentials too.

### 3. Run -- the safety ladder

```bash
# Rung 1 (default): paper trade on the demo exchange
kalshi-bot run

# Rung 2: paper trade against REAL prod market data (still zero orders)
KALSHI_ENV=prod kalshi-bot run

# Rung 3: real orders on the demo exchange (play money)
DRY_RUN=false kalshi-bot run

# Rung 4: real money. All three switches are required, deliberately.
KALSHI_ENV=prod DRY_RUN=false LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISKS kalshi-bot run
```

Climb one rung at a time and let each run for days, not minutes. Compare the
paper equity curve (`kalshi-bot status`) against what you'd accept live.

### Desktop app (GUI / Windows .exe)

```bash
kalshi-bot gui            # needs Tk (`apt install python3-tk` on Debian/Ubuntu)
```

The control panel has a dashboard (Start/Stop, live equity, day P&L,
drawdown, per-strategy deployment, a 24h equity chart, streaming logs), a
**Kill switch** / **Resume** pair, and a Settings tab covering credentials,
the safety switches, strategy budgets and risk limits. Settings, state and
logs live in a per-user folder (`%APPDATA%\KalshiBot` on Windows,
`~/.config/kalshi-bot` on Linux, `~/Library/Application Support/KalshiBot`
on macOS), so the packaged app works from a read-only install location.

**Windows executable**: every push runs the `build-windows-exe` GitHub
Actions workflow, which tests the code, builds a single-file windowed
`KalshiBot.exe` with PyInstaller, and uploads it as an artifact -- open the
latest green run and download `KalshiBot-windows`. To build locally on a
Windows machine instead, install Python 3.10+ from python.org and run
`build_windows.bat`; the exe lands in `dist\KalshiBot.exe`. The app starts
in paper mode and enforces the same triple opt-in before touching real
money. (Unsigned PyInstaller exes can trip SmartScreen/antivirus
heuristics -- "More info -> Run anyway", or build it yourself.)

### Commands

```
kalshi-bot run            trading loop (Ctrl-C cancels resting orders and exits)
kalshi-bot gui            desktop control panel
kalshi-bot status         equity, halts, recent activity
kalshi-bot balance        exchange or paper balance + positions
kalshi-bot markets        top open markets by 24h volume
kalshi-bot scan           one-shot arbitrage scan, read-only
kalshi-bot calibrate      favorite/longshot bias report from settled markets
kalshi-bot kill           halt + cancel resting orders (--flatten also exits positions)
kalshi-bot resume         clear halts
kalshi-bot paper-reset    fresh simulated bankroll
```

### Unattended operation

Docker:

```bash
docker build -t kalshi-bot .
docker run -d --name kalshi-bot --restart unless-stopped \
  -v kalshi_state:/app/state --env-file .env kalshi-bot
```

systemd (`/etc/systemd/system/kalshi-bot.service`):

```ini
[Unit]
Description=Kalshi trading bot
After=network-online.target

[Service]
WorkingDirectory=/opt/kalshi-bot
EnvironmentFile=/opt/kalshi-bot/.env
ExecStart=/usr/bin/python3 -m kalshi_bot run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

The bot is restart-safe: orders, fills, halts, and the paper book live in
`state/kalshi_bot.sqlite3`, and on boot it re-adopts whatever it finds at
the exchange.

---

## Configuration

Everything is an environment variable with a safe default --
see [`.env.example`](.env.example) for the full annotated list. The ones
worth thinking about:

| Variable | Default | Why you might change it |
|---|---|---|
| `LONGSHOT_EDGE_CENTS` | `1.0` | set from your own `calibrate` output |
| `*_BUDGET_FRAC` | `0.30/0.40/0.20` | shift weight toward what works for you |
| `MM_BUDGET_FRAC` | `0.20` | set `0` until MM proves itself in demo |
| `DAILY_LOSS_HALT_FRAC` | `0.05` | tighter = calmer |
| `KELLY_FRACTION` | `0.25` | lower = smaller, smoother |
| `TAKER_FEE_BPS` / `MAKER_FEE_BPS` | `700` / `175` | match the current fee schedule |

## Architecture

```
kalshi_bot/
  money.py       exact integer micro-dollar arithmetic (no floats in P&L)
  fees.py        published fee model (round-up-per-fill), used in every EV gate
  auth.py        RSA-PSS request signing
  client.py      rate-limited, retrying, paginated REST client (trade-api/v2)
  models.py      typed parsing: markets, unified orderbook, orders, fills
  config.py      env-driven config, safe defaults, live-trading triple opt-in
  state.py       SQLite: orders, fills, equity snapshots, paper book, journal
  paper.py       paper broker -- simulated fills against LIVE orderbooks
  portfolio.py   equity & exposure valuation (the compounding base)
  risk.py        Kelly sizing, caps, circuit breakers, kill switch
  execution.py   one order gateway for live + paper, with final clamps
  strategies/    arbitrage.py, longshot.py, market_maker.py (pure logic, tested)
  bot.py         market cache + reconciliation + main loop
  cli.py         run / gui / status / scan / calibrate / kill / resume / ...
  gui.py         Tkinter control panel (bot on a worker thread, queues between)
  gui_settings.py / paths.py   GUI logic with no Tk dependency (tested headless)
gui_app.py       PyInstaller entry point; build_windows.bat + CI build the exe
tests/           84 offline tests incl. end-to-end scripted-exchange cycles
```

Design choices that matter:

- **All prices are integer micro-dollars** end to end. The current API
  speaks decimal-dollar strings with sub-cent ticks; floats would corrupt
  EV math at exactly the margins this bot trades.
- **Fees are modeled pessimistically** (taker formula + round-up per fill)
  inside every gate. A market that actually charges less just clears the
  bar more easily.
- **The paper broker consumes real visible depth** for taker fills, so paper
  taker P&L is close to honest; maker fills assume no queue, so paper MM
  P&L is flattering. The README says this twice because it matters.
- **The exchange is the source of truth** for cash and positions in live
  mode; the local DB only attributes them to strategies and remembers
  history. Restarts therefore can't double-spend.

## Troubleshooting

**"Daily loss halt / drawdown halt right after switching modes or
depositing/withdrawing."** The breakers compare equity against a stored
baseline; an external change (different bankroll, cash moved on/off the
exchange) looks like a giant loss or gain. Run `kalshi-bot resume` (or the
GUI Resume button) -- it clears the halts and re-baselines to current
equity. Since v1.2 baselines are mode-scoped, so the paper-to-live switch
can't cause this in the first place; deposits/withdrawals still warrant a
resume.

**"I want a completely fresh start."** `kalshi-bot paper-reset` wipes the
simulated portfolio, its history and its risk baselines (live state is
untouched). For live mode, the exchange is the source of truth -- deleting
the state DB (`%APPDATA%\KalshiBot\state.sqlite3` for the app,
`state/kalshi_bot.sqlite3` for the CLI) only loses local history and
strategy attribution, never positions or cash.

## Known limitations

- REST polling only (no websockets): the bot reacts in seconds, not
  milliseconds. It will lose races for the juiciest arbs against HFT and
  should be expected to capture the slower remainder.
- Strategy attribution after a restart maps each market to the last
  strategy that traded it -- good enough for budgets, not forensic.
- `mutually_exclusive` exhaustiveness is verified structurally where
  possible; the consensus fallback is a heuristic (see `ARB_EXHAUSTIVE_BID_FLOOR`).
- Fee tiers, maker-fee series, and rate limits drift; all are env-tunable
  (`TAKER_FEE_BPS`, `MAKER_FEE_BPS`, `READ_RPS`, `WRITE_RPS`).

## Disclaimer

Trading event contracts involves substantial risk of loss. This software is
provided as-is, without warranty; you are solely responsible for orders
placed by your API key. Past performance of any strategy -- simulated or
live -- does not guarantee future results.
