# UI_DESIGN.md — ARGUS dashboard (MVP, Phase 4)

**Purpose:** review demo/live results daily without SQL or Telegram scrolling; understand *why* each trade happened; watch safety state. Read-mostly: the only write actions are the same ones Telegram offers (`halt`, `resume`, `approve/reject`, `close`), each behind a confirm dialog and journaled.

**References (information architecture only, not visual):** FreqUI (trade table, pair list, profit summary, bot controls), Nautilus/`nautilus-dashboard` (portfolio/positions/orders views, event timeline). ARGUS adds what neither has: evidence lineage, calibration, data-quality and consensus views.

## 1. Stack (fits a €5 VPS, no build pipeline)
- **FastAPI** (already needed for Telegram webhooks) + **HTMX** for partial updates + **Jinja** templates + **Tailwind** (CDN or prebuilt CSS) + **TradingView Lightweight Charts** (equity curve, price-with-markers) + **Chart.js** for bars/scatter. No React/Node build.
- Auth: served on `127.0.0.1` only and reached through `tailscale serve`, which terminates TLS and applies the tailnet's device authentication (F-134); an SSH tunnel to the same port is the fallback. No app-level auth: `argus ui` refuses any non-loopback bind unless `--allow-remote` is passed, and that flag is an escape hatch, not a deployment mode. Never exposed publicly.
- Data: reads SQLite via the same SQLAlchemy Core models; light caching (30s) for dashboard queries; a `dashboard_views` migration adds a few SQL views (equity curve, open risk, calibration).
- Process: `argus ui` under systemd; ~50 MB RAM.

## 2. Visual direction
Dark, dense, calm. One accent for profit (teal), one for loss (amber), red reserved for safety states. Monospace numerals. Every number that comes from a source shows its provenance chip on hover (`T1 · okx · 12m ago`). Status strip at the top on every page: mode (DEMO/LIVE-AUTO/LIVE-APPROVE), HALT state, last watchdog tick, unprotected positions (must read 0), LLM quota headroom, next scheduled run.

## 3. Screens

| # | Screen | Content |
|---|---|---|
| 1 | **Overview** | Equity curve with drawdown shading; today/week/month PnL; open positions summary; plan funnel for the last 7 days (proposed → gated → filled → closed) with rejection reasons; regime badge and risk multiplier; kill-switch board (active levels, distance to trigger) |
| 2 | **Positions** | Table: symbol, side, size/notional/lev, entry, mark, PnL (R, $, %), stop & trail state, TP ladder progress, time in trade vs time stop, thesis status from last review, protection state chip; row → `/why` |
| 3 | **Plans & pipeline** | All plans (incl. NO_TRADE, REJECTED with gate reasons, INSUFFICIENT_BALANCE), filter by run/asset/status; queued plans with approve/reject buttons when in approve mode; shadow-ledger outcome for rejected plans |
| 4 | **Trade explorer (`/why`)** | Timeline for one trade: dossier excerpt with provenance → factor scores + cited evidence → red-team note → risk-debater stances → trader plan (+ consensus verdict) → gate result with adjustments → orders/fills/fees → reviews → exit → post-mortem. Price chart with entry/SL/TP/trail/exit markers |
| 5 | **Calibration & performance** | Win rate / avg R by score bucket, by factor, by regime, by asset, by small-account step; expectancy, PF, Sharpe, DD; fee+funding drag; exit-reason mix; consensus disagreement stats (how often, who was right per shadow ledger) |
| 6 | **Data quality & sources** | Per-source freshness/quality over 7 days; trap events; price-consensus disagreements; sources that vetoed trades |
| 7 | **LLM & quotas** | Calls per provider/model/agent; schema pass rate; latency; quota headroom; weekly model-eval results; paid-fallback spend |
| 8 | **Runs & events** | Every run with duration, stages completed, errors; watchdog events; circuit breakers; kill-switch triggers; Telegram command log |
| 9 | **Knowledge** | Active lessons (with support counts), pending catalyst candidates (confirm/decline), core/whitelist universe with asset cards, blocklist |

## 4. Acceptance (MVP)
- Loads in < 1s on the VPS for 10k journal rows.
- Every trade explains itself end-to-end on screen 4 without opening the DB.
- Status strip is never green if any position is unprotected.
- Approve/reject/halt/close actions require confirmation and appear in the Telegram event feed and the journal.

## 5. Later
Mobile-friendly layout; per-asset pages with asset memory; Polymarket watch panel; multi-bot view when the stocks instance exists; public read-only "track record" page if you ever go SaaS.
