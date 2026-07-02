# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal quant decision-support platform (not an auto-trading system). It answers three
questions in order: should I be aggressive in the market right now (V1 macro sentiment) →
which sector should money go to (V2 sector rotation) → which specific stock, and when should
I get nervous (V3 stock scorecard + V4 watchlist alerts) → how to express that view with
options and what's the expected payoff (V5 options analysis).

Full functional spec, data sources, and design system rules live in `docs/guidebook.md` —
read it before making non-trivial changes, especially before touching scoring logic, config
schema, or the frontend. `README.md` has the user-facing quick-start.

## Commands

**Requires Python 3.10+** (the codebase uses `X | None` union-type syntax throughout, e.g.
`run_options.py`). The system `python3` on this machine is Xcode's 3.9 and will fail with
`TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'` on import. Use
`/Users/xp/.local/bin/python3.11` instead. The project venv is `.venv311` (not the more
generic `.venv` name) — always activate that one:

```bash
# setup
/Users/xp/.local/bin/python3.11 -m venv .venv311 && source .venv311/bin/activate
pip install -r requirements.txt
export ZHIPU_API_KEY=...   # needed for narrative layer (Zhipu GLM); scoring/charts still work without it

# run the daily pipeline (V1 -> V2 -> V3 -> V4)
python run_daily.py
python run_daily.py --date 2026-06-15   # backfill/replay a specific date

# run V5 options analysis (on-demand, not part of the daily pipeline)
python run_options.py --symbol NVDA
python run_options.py --symbol NVDA --expected-price 210 --expected-date 2026-08-21

# tests
python -m pytest tests/ -v
python -m pytest tests/test_scoring.py -v          # single file
python -m pytest tests/test_scoring.py::test_name  # single test
```

Tests use synthetic data and don't hit the network. Real fetcher behavior (yfinance, AAII/CNN
scraping) must be verified manually on a machine with outbound access — some environments
(corporate proxy, some CI/sandboxes) block those domains.

## Architecture

**Layering discipline (do not violate):** fetch → score → narrate → render, always in that
order, and always deterministic-code-then-LLM. Scoring and dimension math is 100% Python; the
narrative layer (`core/narrative/generate.py`, currently wired to Zhipu GLM via its
OpenAI-compatible `chat/completions` endpoint, `ZHIPU_API_KEY` env var) only translates
already-computed structured results into Chinese prose. Never let the LLM compute numbers.

- `core/fetchers/` — one `MarketFetcher`/`OptionsFetcher` abstract interface (`base.py`,
  `options_base.py`), concrete implementations behind it (`us_market.py`, `us_stock.py`,
  `us_breadth.py`, `sector_holdings.py`, `sentiment_scrape.py`, `options_yfinance.py`,
  `options_financial_service.py`). A CN/akshare implementation is meant to drop in behind
  the same interface later without touching scoring/narrative code — don't leak US-market
  assumptions into the abstract interface.
- `core/scoring/` — one file per module (`v1_composite.py`...`v4_alerts.py`), pure functions
  over fetcher output, no I/O beyond what's passed in.
- `core/options/` — V5's math layer: Black-Scholes pricing/greeks (`pricing.py`), scenario
  engine (`scenarios.py`), the 3x3 leg-strategy recommender (`recommender.py`), options
  sentiment snapshot (`sentiment.py`).
- `core/render/` — Jinja2 templates + data injection only; never build HTML strings by hand.
  One `render_vN.py` per module plus `report.py` for the shared daily markdown report.
- `core/watchlist.py` — shared by V3/V4, loads from `config.yaml` (`source: config`) or a CSV
  (`source: csv`).
- `core/db.py` — single SQLite file (`db/market.sqlite`), two tables: `daily_metrics` (one row
  per date/module/dimension, always written even on failure) and `daily_composite`. Every
  metric is persisted unconditionally, including `status=missing` rows — this history is the
  only planned validation method for whether the scoring framework is any good (see M2 in the
  guidebook).
- `run_daily.py` — orchestrates V1-V4 in one SQLite transaction per run; `run_options.py` is
  the separate on-demand V5 entrypoint (not part of the daily cron).

**Failure handling contract:** a `FetchResult(ok=False, ...)` must propagate as a `missing`
status on the affected dimension, which then gets excluded from that day's composite score
(remaining dimension weights are renormalized — see `v1_composite.py`). Never silently reuse
stale data or fabricate a value to paper over a failed fetch. This applies symmetrically in
V4 alerts and V5 options data (see next point).

**V5 data source is a snapshot-file contract, not an HTTP client.** `financial_service` (the
preferred options data source per the guidebook) is actually a local Claude Code skill
invoked interactively, not a service this pipeline calls. The integration is: a Claude Code
session generates `data/options_snapshots/<SYMBOL>.json` following the schema in
`docs/financial_service_snapshot.md`, and `options_financial_service.py` reads + validates
that file (schema + `max_age_hours` freshness, default 24h — stale options data is worse than
none). If the snapshot dir doesn't exist and `v5.allow_fallback: true`, it warns and falls
back to `options_yfinance.py`; if `false`, it errors outright. It never silently switches
sources. **`config.yaml`'s `v5.provider` is currently set to `yfinance`**, not
`financial_service` — no snapshot files exist yet and the installed financial-analysis plugin
skills (DCF/comps/LBO) don't actually fetch live option chain data, so financial_service isn't
usable as-is. Switch back once real snapshots are being produced.

**Config is the single source of truth for tunables.** All weights/thresholds/tickers/
watchlist live in `config.yaml`, never hardcoded. V1 weights currently start equal-weighted by
design (see guidebook M2 milestone) — don't "improve" them without being asked; recalibration
is a deliberate, dated, human-reviewed step after 2-4 weeks of paper comparison.

## Frontend

Static HTML only (`output/site/*.html`), generated fresh on every `run_daily.py` run via
Jinja2 — no backend, deployable as-is to GitHub Pages/Vercel. Design system rules (dark-tech
data panel, single accent color, IBM Plex Mono for numbers, no em-dashes, four states per data
card: loading/empty/error/ok, etc.) are fully specified in `docs/guidebook.md` section 7 —
follow them exactly rather than improvising, they're adapted from the `design-taste-frontend`
skill's rules for a dashboard context.

## Current status

V1-V5 are all implemented (see git log / README for detail). The project is in the M2
calibration period: scoring weights are intentionally equal-weighted and unvalidated against
real outcomes — don't treat the composite scores as proven signal, and don't hardcode
"improved" weights without an explicit calibration pass backed by the SQLite history.
