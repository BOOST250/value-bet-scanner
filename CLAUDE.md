# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Value bet scanner that polls the Odds-API.io `/value-bets` endpoint, logs bets to a database, auto-grades settled bets against match results, computes closing-line value (CLV) against Pinnacle, and serves a live web dashboard. Runs as two independent worker instances (one per Odds-API.io account/key, each locked to its own 2-bookmaker pair) plus one dashboard process, deployed as separate Railway services sharing one database.

## Running

```bash
# Scanner — uses DEFAULT_BOOKMAKERS (currently Polymarket, Roobet)
python value_bet_alerts.py

# Scanner with an explicit bookmaker pair (and optional sport filter)
python value_bet_alerts.py "Betfair Exchange,BC.Game"
python value_bet_alerts.py Polymarket,Kalshi baseball

# Same, but via env var (needed when the pair contains a space, e.g. "Betfair Exchange" —
# Railway's Custom Start Command field does not reliably preserve quoted args; BOOKMAKERS
# takes precedence over the argv form)
BOOKMAKERS="Betfair Exchange,BC.Game" python value_bet_alerts.py

# Dashboard (Flask app on http://localhost:5000)
python dashboard.py

# CLI stats only
python value_bet_alerts.py --stats
```

Both the scanner and dashboard read/write the same database — run them in separate terminals/processes. Locally with no `DATABASE_URL` set, this is `bets.db` (SQLite); in production it's Postgres (Supabase) — see `db.py`.

## Environment Variables

- `ODDS_API_KEY` — required; the Odds-API.io key for this worker instance
- `DATABASE_URL` — Postgres connection string; omit to fall back to local SQLite (`bets.db`)
- `BOOKMAKERS` — optional comma-separated bookmaker pair, overrides `DEFAULT_BOOKMAKERS`/argv (see note above on why this exists)
- `DISCORD_WEBHOOK_URL` — optional; Discord webhook for new-bet alerts
- `BETTINGISCOOL_API_KEY` — optional; enables CLV computation against Pinnacle closing lines. Without it, `fetch_clv()` is a no-op and CLV columns show "-"

## Architecture

**Two scanner instances, one codebase.** `value_bet_alerts.py` is deployed twice as separate Railway services (e.g. `worker` and a second service), each with its own `ODDS_API_KEY` pointing at a different Odds-API.io account and its own bookmaker pair (via `BOOKMAKERS` or argv). Odds-API.io plans are locked to 2 bookmakers per account/key — once you've queried 2 bookmakers, the account locks onto that pair via `/bookmakers/selected/select` until cleared via `/bookmakers/selected/clear`. Both instances write to the same `bets` table, so dashboard/grading/CLV are bookmaker-agnostic and need no per-instance awareness.

**`db.py`** — Database abstraction, SQLite when `DATABASE_URL` is unset, Postgres otherwise
- `execute()`/`fetchall()`/`fetchone()` take `?`-style placeholders; `_adapt_query()` rewrites them to `%s` and adapts `INSERT OR IGNORE` → `INSERT ... ON CONFLICT DO NOTHING` for Postgres
- `init_db()` creates the `bets` table and adds any new columns (`event_url`, `tracked`, `clv_raw`, `clv_true`, `closing_odds`) via best-effort `ALTER TABLE` with a short statement timeout on Postgres — failures are logged and swallowed, not fatal, since concurrent workers race to add the same column on startup

**`value_bet_alerts.py`** — Scanner + grading + CLV engine
- `fetch_value_bets()` → `GET /v3/value-bets?bookmaker=X&includeEventDetails=true`
- `filter_bets()` → keeps bets with `expectedValue > 100` (100 is break-even in API units; 105 = +5% EV)
- `log_bet()` → inserts into `bets`; aliases market name `Moneyline`/`1X2` → `ML` so esports/3-way markets aren't fragmented from `ML` in downstream aggregation
- `grade_bets()` → each cycle, groups pending bets (past `match_date`) by sport slug, fetches `GET /v3/events?sport=X&status=settled`, grades ML/Spread/Totals/Totals HT; voids tennis matches that settled 0-0 (impossible score, means cancelled/walkover)
- `fetch_clv(conn)` → matches settled bets to Pinnacle fixtures/odds via the BettingIsCool API by date + team-name token overlap, gated behind `BETTINGISCOOL_API_KEY`; runs every `CLV_EVERY_N` cycles, capped at `CLV_BATCH_LIMIT` per run. Coverage is intentionally narrow (`CLV_SPORT_IDS`/`CLV_TEAM_MARKETS`) — only sports/markets validated to match Pinnacle's fixture/market shape cleanly; Tennis is special-cased (Totals (Games) lives on a separate "(Games)"-suffixed fixture)
- `seen_ids` loaded from DB on startup so restarts don't re-alert/re-log already-seen bets
- Polling/grading/CLV cadence is rate-limit-driven, not arbitrary: see the comments by `POLL_INTERVAL`/`GRADE_EVERY_N`/`CLV_EVERY_N` for the request-budget math (Odds-API.io caps at 100 req/hr per key)

**`dashboard.py`** — Flask dashboard (single-file; HTML/JS is the `INDEX_HTML` string at the bottom of the file)
- `/api/live`, `/api/results`, `/api/tracker` (user-starred bets + daily/cumulative P&L), `/api/track` (toggle starred), `/api/stats` (EV/odds/sport/bookmaker/market breakdowns)
- `apply_filters()` — shared query-param filtering (sport/bookmaker/market/odds/EV) used by `/api/live` and `/api/results`
- `attach_liquidity()` — for pending Polymarket bets, resolves the matching CLOB sub-market + outcome token (via `_find_market`/`_outcome_token`, matching on market type + handicap + team-name tokens) and fetches best-ask order book depth; for Kalshi, same idea via `yes_ask`. Kalshi liquidity is fetched in a background thread (`_kick_off_kalshi_refresh`) rather than inline — Kalshi rate-limits on concurrency, not volume, and request latency from Railway is unpredictable, so the dashboard never blocks a request on it; coverage fills in across the frontend's 15s auto-refresh polls
- `NET_PROFIT_SQL`/`FEE_RATE_SQL` — per-bookmaker fee model used for "after fees" ROI/profit. Polymarket (3%) and Kalshi (7%) use a per-contract formula (`feeRate * (1 - 1/odds)`, since fee is charged per-contract on the `p*(1-p)` trade structure, not on stake). Betfair Exchange (2.5%) is structurally different — flat commission on net winnings only, so it's a separate term (`BETFAIR_COMMISSION_SQL`) added only to the `won` branch. Any other bookmaker defaults to 0% until its real fee is looked up
- Frontend (`INDEX_HTML`) reconstructs Stake's event URL client-side (`buildStakeUrl` et al.) since the API's `href` for Stake omits the category path the real site needs — best-effort, validated for Tennis/Football, unverified guesses elsewhere, falls back to a search link
- Frontend auto-refreshes every 15s via JS fetch; has Filters, Tracker, Calendar heatmap, and cumulative-profit Chart sections

## Grading Logic

- **ML / 1X2 / Moneyline**: higher score wins; draw = push for 1X2, but see `grade_bet_row` — esports `Moneyline` is aliased to `ML` at write time, so there's no live 3-way ML market in practice
- **Spread / Map Handicap**: apply `hdp` to the bet side's score, compare
- **Totals / Totals (Games) / Total Maps**: compare combined score to `hdp`
- **Totals HT**: same as Totals but against first-half-only score; voided if no HT score is available
- **Team Total Home / Team Total Away**: compare that one team's score alone (not combined) to `hdp`
- Final score resolution prefers overtime/extra-innings (`periods.ot`) over regulation (`periods.ft`), falling back to the top-level `scores.home/away` if no periods breakdown exists
- **Bookings Totals / Corners Totals always void by design** — the `/v3/events` feed only returns goals, no corner/booking counts, so these can never be graded with current data
- Anything else grades as `void` (not implemented)
- Sport slugs for the `/v3/events` lookup are derived by lowercasing + replacing spaces with hyphens (e.g., "American Football" → "american-football")

## API Constraints

- Each Odds-API.io account/key is locked to 2 bookmakers at a time (see "Two scanner instances" above)
- Prediction markets (Polymarket, Kalshi) have **no live/in-play odds** — `/value-bets` returns pre-game only for those
- `/odds/movements` returns no data for Polymarket/Kalshi
- 100 req/hr cap per key, shared across fetch + grading + CLV cycles for that instance — see the rate-budget comments in `value_bet_alerts.py` before changing any interval
