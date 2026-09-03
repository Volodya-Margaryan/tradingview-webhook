# TradingView Paper-Trading Webhook Receiver

> **⚠️ Educational / paper-trading project.** This system never places a real
> order and is not investment advice. It exists to demonstrate webhook
> ingestion, risk-managed signal processing, and strategy backtesting
> end-to-end — not to recommend trades or strategies to anyone.

A self-hosted webhook receiver that turns TradingView alerts into
**simulated** trades: it authenticates and validates each incoming signal,
runs it through a configurable risk engine, executes it against a local
paper-trading account, and gives you a live dashboard, JSON API, and CLI to
inspect everything. Includes a separate suite of backtesting scripts used to
research and stress-test the trading strategy against 8 years of real
historical market data before trusting any of it.

## Why this exists

Most "TradingView bot" tutorials skip straight to placing real orders. This
project deliberately stops one layer short of that: it's the piece that
should exist *before* any real execution — proving a signal pipeline is
authenticated, validated, rate-limited, risk-bounded, and auditable, and
proving a strategy has a real statistical edge (not just a good-looking
backtest) before it's ever allowed near real capital.

## Architecture

```
TradingView Alert (webhook, JSON)
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│  POST /webhook  (app/main.py)                              │
│    1. Shared-secret auth        (app/security.py)          │
│    2. Schema/type validation    (app/schemas.py, Pydantic)  │
│    3. Kill switch check         (app/killswitch.py)         │
│    4. Duplicate-signal filter   (app/dedup.py)              │
│    5. Live-price sanity check   (app/market_data.py)        │
│         — informational only, never blocks execution        │
└───────────────────────────────────────────────────────────┘
        │ accepted signal
        ▼
┌───────────────────────────────────────────────────────────┐
│  Risk engine                    (app/risk.py)               │
│    position sizing, max open positions, max daily loss,     │
│    max position size — all configurable via .env            │
└───────────────────────────────────────────────────────────┘
        │ sized order
        ▼
┌───────────────────────────────────────────────────────────┐
│  Paper-trading engine           (app/paper_engine.py)       │
│    simulated fill, no broker call, ever                     │
└───────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│  Storage (SQLite)               (app/storage.py)            │
│    signals, positions, trades, settings — full audit trail  │
└───────────────────────────────────────────────────────────┘
        │
        ▼
   Dashboard (static/dashboard.html) · JSON API · cli.py
```

Every rejection (bad secret, malformed payload, duplicate, kill switch,
risk-limit breach) is logged with a reason — nothing fails silently.

## Features

| Requirement | Where |
|---|---|
| HTTP POST webhook receiver | `app/main.py` → `POST /webhook` |
| Shared-secret auth (reject + log on failure) | `app/security.py`, checked first in the handler |
| Payload validation (types, symbol format, BUY/SELL enum) | `app/schemas.py` (Pydantic) |
| Timestamped logging of every signal, accepted or rejected | `app/storage.py` (`signals` table) + `app/logging_conf.py` |
| Paper-trading execution engine (simulated fills) | `app/paper_engine.py` |
| Configurable position sizing / max-risk rules | `app/risk.py`, tunable via `.env` |
| Duplicate-signal protection | `app/dedup.py` |
| Kill switch (instant, no restart needed) | `app/killswitch.py` + `python cli.py killswitch on\|off` |
| Local storage, full audit trail | SQLite file at `data/trading.db` |
| Inspection interface | Web dashboard at `/dashboard`, JSON API, and `cli.py` |
| Live market price + unrealized P&L (display only) | `app/market_data.py` (Yahoo Finance, delayed quotes) |
| Price-sanity check on incoming signals | logged as `price_check`, informational only, never blocks execution |
| Session-scoped reporting | `python cli.py session-marker` then `python cli.py summary` |
| Multi-period strategy backtesting with overfitting checks | `backtest_*.py`, see [Strategy research](#strategy-research--backtesting) |

**Important boundary on live prices:** `app/market_data.py` is used only for
dashboard display and the informational price-sanity flag. It is never
called from `app/paper_engine.py` or `app/risk.py` — a signal always fills
at the price the webhook payload actually sent, and a Yahoo Finance outage
can never block or alter trade execution.

## Repository layout

```
app/                  FastAPI application (webhook, risk, paper engine, storage)
static/dashboard.html Live dashboard (vanilla HTML/JS, no build step)
cli.py                Terminal control: kill switch, signals, positions, performance
tests/                pytest suite (schemas, security, dedup, risk, paper engine, API)
backtest_*.py         Strategy research scripts (see below) — each is a self-contained,
                       runnable experiment against real historical data
optimize_bracket.py   Parameter grid-search + overfitting check (PBO / Deflated Sharpe)
data/                 Generated at runtime: SQLite DBs, logs, CSV exports (gitignored)
```

## 1. Setup

```bash
git clone <this-repo-url>
cd tradingview-webhook
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create your local config (never commit this file — it's gitignored):

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste the generated string into `.env` as `WEBHOOK_SECRET=...`. This is the
only credential in this project — a random token you generate yourself, not
a broker or exchange credential — and it's read from `.env` at runtime via
`app/config.py`. It is never hardcoded in source.

Review the other `.env` values while you're in there — `STARTING_EQUITY`,
`MAX_RISK_PER_TRADE_PCT`, `MAX_POSITION_NOTIONAL_PCT`, `MAX_OPEN_POSITIONS`,
`MAX_DAILY_LOSS_PCT`, `ALLOW_SHORTS`, `DEDUP_WINDOW_SECONDS` — these drive
the paper-trading risk rules.

## 2. Run the server

```bash
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Sanity check:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/status
```

Open the dashboard: `http://localhost:8000/dashboard`.

## 3. Give it a public HTTPS URL so TradingView can reach it

TradingView needs to POST to a public HTTPS endpoint — `localhost` isn't
reachable from their servers. Pick one:

### Option A — ngrok (quickest, for testing)

```bash
brew install ngrok      # or download from ngrok.com
ngrok config add-authtoken <your-authtoken>   # free account, one-time
ngrok http 8000
```

ngrok prints a URL like `https://abcd1234.ngrok-free.app`. Your webhook URL
is `https://abcd1234.ngrok-free.app/webhook`. It changes every time you
restart ngrok on the free tier — update the TradingView alert if so.

### Option B — Cloudflare Tunnel (also free, no random-URL churn if you have a domain)

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8000
```

This also prints a temporary `https://<random>.trycloudflare.com` URL. For a
stable hostname, authenticate with a Cloudflare account (`cloudflared tunnel
login`) and run a named tunnel mapped to your own domain.

### Option C — Real deployment (for anything beyond testing)

Run this on a small VPS or PaaS (Fly.io, Render, Railway):

1. Copy this project to the server, install deps, create `.env` there
   (same rule: never commit it).
2. Run the app under a process manager, e.g. systemd:
   ```ini
   # /etc/systemd/system/tv-webhook.service
   [Service]
   WorkingDirectory=/opt/tradingview-webhook
   ExecStart=/opt/tradingview-webhook/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
   Restart=always
   User=tvwebhook
   ```
3. Put a reverse proxy in front for TLS — Caddy is the simplest (automatic
   Let's Encrypt certs):
   ```
   # Caddyfile
   your-domain.com {
       reverse_proxy 127.0.0.1:8000
   }
   ```
4. Point TradingView at `https://your-domain.com/webhook`.

Treat the tunnel/deployment URL as sensitive-ish — anyone who has it (and
the correct secret) can send it webhook traffic, though the shared-secret
check means a bare URL alone can't trigger a trade.

## 4. Create the matching TradingView alert

1. On your chart, open **Create Alert**.
2. Set your condition as usual (indicator crossover, price level, strategy
   order event, whatever your strategy uses).
3. Under **Notifications**, enable **Webhook URL** and paste:
   `https://<your-tunnel-or-domain>/webhook`
4. In the **Message** box, replace the default text with JSON matching the
   schema this server expects. Use TradingView's placeholders so the alert
   fills in live values:

   ```json
   {
     "secret": "PASTE_YOUR_WEBHOOK_SECRET_HERE",
     "symbol": "{{ticker}}",
     "price": {{close}},
     "timeframe": "{{interval}}",
     "action": "BUY",
     "strategy": "ema-cross"
   }
   ```

   - `secret` — the exact value of `WEBHOOK_SECRET` from your `.env`.
   - `symbol` — `{{ticker}}` fills in the chart's symbol (e.g. `AAPL`,
     `BINANCE:BTCUSDT`). Both bare and `EXCHANGE:TICKER` forms pass
     validation.
   - `price` — `{{close}}` (no quotes — it must be a JSON number).
   - `timeframe` — `{{interval}}` fills in the chart timeframe (e.g. `5`,
     `240`, `1D`).
   - `action` — hardcode `"BUY"` or `"SELL"` per alert (you'll typically
     create one alert for entries and a separate one for exits, each with
     the matching literal action).
   - `strategy` — any label you want to tag signals from this alert with.

5. Save the alert, then confirm it arrived via `curl http://localhost:8000/signals`
   or the `/dashboard` page.

If you use a Pine Script `strategy()` alert, TradingView also gives you
`{{strategy.order.action}}` — substitute it into `action` (uppercased via
`str.upper()` in Pine) since this server requires literal `BUY`/`SELL`.

## 5. Kill switch

Two ways to stop all signal processing immediately:

```bash
.venv/bin/python cli.py killswitch on      # takes effect on the very next request
.venv/bin/python cli.py killswitch off
.venv/bin/python cli.py killswitch status
```

or set `KILL_SWITCH=true` in `.env` and restart the server (persists across
restarts until you flip it back).

While active, every incoming webhook is logged and answered with
`{"status": "rejected", "reason": "kill switch active..."}` — nothing is
executed.

## 6. Inspecting signals, positions, and performance

Web dashboard: `http://localhost:8000/dashboard` (auto-refreshes every 5s).

JSON API: `GET /status`, `/signals`, `/positions`, `/trades`, `/performance`, `/quotes`.

CLI:
```bash
.venv/bin/python cli.py status
.venv/bin/python cli.py signals --limit 20
.venv/bin/python cli.py positions --status open
.venv/bin/python cli.py trades --limit 20
.venv/bin/python cli.py performance
.venv/bin/python cli.py summary            # one-shot report, scoped to the current session
```

## Strategy research & backtesting

The `backtest_*.py` scripts are a research log, not a single finished
product — each one is a self-contained experiment run against 8 years of
real daily price history (via `yfinance`), reusing the same risk/execution
code as the live paper engine where relevant, and each is honest about
negative results, not just the wins:

- **`backtest_regime.py` / `backtest_bracket*.py`** — a trend-following
  entry (price above both its 20- and 50-day average, with positive
  momentum) combined with a stop-loss/take-profit exit bracket. Tested with
  and without a take-profit cap, and with intraday (High/Low) vs.
  close-only stop checking, to see how each choice actually affected
  results — not assumed.
- **`backtest_bracket_multi.py`** — the same strategy replayed across 14
  independent, non-overlapping ~6-month windows spanning 2018–2026, to
  check whether a result holds up across different market regimes (bull,
  bear, choppy) instead of trusting a single lucky window.
- **`optimize_bracket.py`** — a parameter grid-search paired with
  **Probability of Backtest Overfitting (PBO)** and **Deflated Sharpe
  Ratio (DSR)** checks (combinatorially-symmetric cross-validation, per
  Bailey/López de Prado), specifically to catch the case where a
  "best" configuration only looks good because many configurations were
  tried — a failure mode most casual backtests never check for.
- **`backtest_meanrev.py`, `backtest_daytrade*.py`** — short-holding-period
  and same-day trading variants (mean-reversion pullbacks, gap-continuation,
  gap-fade, volume-confirmed gaps). Documented here deliberately even though
  none of them beat the trend-following baseline: the point of a real
  research process is ruling things out, not just reporting what worked.

Run any of them directly, e.g.:

```bash
.venv/bin/python backtest_bracket_multi.py
.venv/bin/python optimize_bracket.py
```

Each prints a full results table to stdout and writes an isolated SQLite DB
under `data/` (never the same file as the live paper account), which you can
point a second instance of the dashboard at:

```bash
DB_PATH="$(pwd)/data/backtest_bracket_p1.db" .venv/bin/uvicorn app.main:app --port 8001
```

## 7. Tests

```bash
.venv/bin/pytest -q
```

Covers payload validation, secret rejection, duplicate suppression, risk
sizing/circuit breakers, paper-engine fills/P&L, live-quote handling, and
the full webhook HTTP flow (401/422/200 paths, kill switch, duplicates).

## Design notes / limitations (read before relying on this)

- **Fills are simulated at the signal's reported price** — there's no
  slippage model in the live webhook path itself (the backtest scripts do
  model trading-cost friction). Good enough for validating the
  alert → signal → paper-trade pipeline; treat backtest results as
  research, not a guarantee.
- **One open position per symbol** in the live paper engine — a second
  `BUY` while a long is already open on that symbol is rejected (no
  pyramiding), to keep sizing/risk math simple. Adjust `app/paper_engine.py`
  if you want scaling-in behavior.
- **Shorts are off by default** (`ALLOW_SHORTS=false`) — a `SELL` with no
  open long is logged but not executed unless you enable it.
- **This is paper-only, by design.** There is no broker integration
  anywhere in this codebase — wiring in real execution is a deliberate,
  separate later step that should sit behind its own human-confirmation and
  hard-risk-limit gate, not something this server does implicitly.

## License

MIT — see [LICENSE](LICENSE).
