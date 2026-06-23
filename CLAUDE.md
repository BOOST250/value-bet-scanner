# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Value bet scanner that polls the Odds-API.io `/value-bets` endpoint for Polymarket and Kalshi, logs all bets to SQLite, auto-grades settled bets against match results, and serves a live web dashboard.

## Running

```bash
# Scanner (polls every 120s, logs to bets.db, sends Discord alerts)
python value_bet_alerts.py

# Dashboard (Flask app on http://localhost:5000, reads from bets.db)
python dashboard.py

# CLI stats only
python value_bet_alerts.py --stats

# Custom bookmakers / sport filter
python value_bet_alerts.py Polymarket,Kalshi baseball
```

Both scripts share `bets.db` (SQLite) — run them in separate terminals.

## Environment Variables

- `ODDS_API_KEY` — required for the scanner; set as a Windows user environment variable
- `DISCORD_WEBHOOK_URL` — optional; Discord webhook for new-bet alerts

## Architecture

**`value_bet_alerts.py`** — Scanner + grading engine
- `fetch_value_bets()` → calls `GET /v3/value-bets?bookmaker=X&includeEventDetails=true`
- `filter_bets()` → keeps bets with EV > 100 (break-even baseline in API units)
- `log_bet()` → inserts into SQLite `bets` table
- `grade_bets()` → each cycle, queries pending bets past their match_date, fetches `GET /v3/events?sport=X&status=settled`, compares scores to grade ML/Spread markets as won/lost/push
- `seen_ids` loaded from DB on startup to survive restarts

**`dashboard.py`** — Flask web dashboard (single-file, HTML inlined as string)
- `/api/live` — pending bets
- `/api/results` — graded bets
- `/api/stats` — aggregated stats including EV buckets, odds buckets, by-sport, by-bookmaker, by-market breakdowns
- Frontend auto-refreshes every 15 seconds via JS fetch

## API Constraints

- Plan is locked to 2 bookmakers: **Polymarket** and **Kalshi**
- These are prediction markets — **no live/in-play odds available**
- `/value-bets` returns pre-game only
- EV values from the API use 100 as break-even (105 = +5% EV)
- `/odds/movements` endpoint returns no data for these bookmakers

## Grading Logic

- **ML / 1X2**: higher score wins; draw = push
- **Spread**: apply `hdp` to the bet side's score, compare
- **Totals / Moneyline / other**: currently graded as `void` (not implemented)
- Sport slugs are derived by lowercasing + replacing spaces with hyphens (e.g., "American Football" → "american-football")
