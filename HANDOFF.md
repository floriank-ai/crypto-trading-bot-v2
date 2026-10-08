# HANDOFF — Cross-Bot Context (Crypto ↔ Stock)

> **For the new Claude Code session in this directory.**
> Read this file first. Then check MEMORY.md (auto-loaded) for the
> "Deployed bot location" note.

---

## TL;DR

Florian (simonfloriank@gmail.com) runs **two bots in parallel**:

1. **Crypto-Bot v2** — *this directory* — Kraken via Alpaca-bridge, deployed on Railway.
2. **Stock-Bot v1** — `/Users/floriankretschman/Downloads/stock-trading-bot-v1/`
   — Alpaca paper account, deployed on Railway as a third service.
   Telegram: `@fk_stockbot`. Started today (2026-05-04, Monday).

The two bots are **separate repos, separate Railway services, separate
Telegram tokens**. Gemini API key is shared. Both follow the same
architectural pattern (this v2 codebase was the fork-source).

---

## Why this file exists

The stock-bot was just built in a separate Claude Code session. Some
patterns/improvements that emerged there are worth knowing about here,
because they may be portable back into crypto.

---

## What was built in the stock-bot session today

### 1. Two-Tier Universe (Core + Discovery)

The stock-bot trades **10 fixed core symbols** (SPY, QQQ, AAPL, …) AND
a **dynamic discovery layer** of ~15 symbols pulled fresh each cycle
from Alpaca's screener endpoints.

- Core symbols: full sentiment confirmation, normal/strong tiers.
- Discovery symbols: **no sentiment** (would burn the Gemini quota),
  **MIN tier only** (~$50 size cap), tagged `momentum_discovered` so
  the kill-switch buckets them separately.

**File:** `scanner.py` in stock-bot — `DiscoveryScanner` class.
- Hits `/v1beta1/screener/stocks/most-actives` (volume) and
  `/v1beta1/screener/stocks/movers` (price + percent_change).
- Merges both responses by symbol, bulk-quotes missing prices.
- Filters: min price $5, min 1M avg volume, max 0.5% spread.
- 60s cache.

**Why this matters here:** Crypto-bot already has `gainer_scanner.py`.
Compare patterns — the stock-bot's bucketing of discovered vs core
in the kill-switch (`momentum` vs `momentum_discovered`) is cleaner
and worth porting if you have not already.

### 2. Sentiment-Confirm-Only pattern

Already present in this v2 codebase. Re-confirmed as the right model
in the stock-bot: sentiment **gates** signals, never originates them.
Keeps Gemini calls to <50/day so the free tier holds.

### 3. Kill-Switch Buckets

Stock-bot tracks separate pause-states per-strategy AND per-source:
`momentum`, `momentum_discovered`, `gap`, `sentiment`. A burst of
losses on discovered names won't pause core momentum. Worth verifying
this v2 has the same granularity (check `risk_manager.py`).

### 4. Position Tiers

Stock-bot uses MIN ($50) / NORMAL ($150) / STRONG ($300) tiers driven
by signal strength. Discovered universe is hard-locked to MIN.
Crypto-bot has equivalents via config — sanity-check the values are
similar in spirit.

---

## Ecosystem facts (good to know)

- **Alpaca paper account** for stock-bot: `PA3T4Y5RZWA4`.
  - API key: `PKNHCEJDSDCSL2TQAX22NTJR2L`
  - Self-tracked sandbox cap of $1000 (Alpaca default $100k).
  - No KYC needed — paper account, 5-min signup.
- **Gemini key** (shared, free tier): `AIzaSyBPu5pfoFj7yPo0dMpZdbGOP3NVHFKoIiE`
  - Model fallback chain: `2.5-flash` → `2.5-flash-lite` → `2.0-flash`.
  - `2.0-flash` has hit 429 quota; the chain works around it.
- **Railway**: three services in the same project — crypto-bot, stock-bot,
  (and one other). Use `railway redeploy -y -s <service>` after env
  changes; auto-redeploy on env update is unreliable.
- **Telegram tokens are separate per bot.** Stock-bot uses
  `8044329158:AAEJy5k43ZHs5adQcC20YFbmtT8YdcfVDxQ` for `@fk_stockbot`.
  Don't cross-wire.

---

## User context

- Email: simonfloriank@gmail.com
- Today's date: 2026-05-04 (Monday)
- User runs **two Claude Code windows** going forward — one per bot
  directory — for clean separation.
- Deployment preference: **the v2 repo here in `Downloads/` is the
  running one**, NOT the stub at `~/crypto-trading-bot/`. Don't edit
  that stub.
- User communicates in German + English mixed; he's fine with either.

---

## Suggested first actions for the new session

1. Read `MEMORY.md` (auto-loaded) — confirms the deployed-bot location.
2. Skim this file (you just did).
3. If user asks about cross-pollination from stock-bot, the candidates are:
   - Tighter kill-switch buckets per source (core vs discovered).
   - Audit `gainer_scanner.py` against `scanner.py` from stock-bot — the
     stock version handles the screener-endpoint-schema-mismatch
     elegantly with bulk-quote fallback.
   - Confirm position-tier hard-cap on discovered/scanned symbols.
4. If the request is purely crypto-bot work, ignore the stock-bot stuff
   and just operate normally.

---

*Generated 2026-05-04 by the stock-bot session. Safe to delete once read.*
