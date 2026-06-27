"""
Standalone WebSocket-based signal pipeline.

Compares Stake's live odds against a devigged BetOnline.ag "sharp" line (via a
second Odds-API.io account's WebSocket add-on) and logs positive-EV bets into the
same `bets` table the main scanner (value_bet_alerts.py) uses, so they get
graded/tracked/CLV'd identically to everything else. Polymarket scanning on the
first account is untouched -- this is a fully separate process.

Run as its own process/service:  python stake_ws_scanner.py
Requires:
  ODDS_API_KEY        -- the first account's key, reused here only to look up
                          event metadata (team names/sport/league) via REST,
                          since the WebSocket itself only gives a numeric event id.
  ODDS_API_KEY_SHARP   -- the second account's key, with WebSocket access and
                          Stake + BetOnline.ag selected as its 2 bookmakers.
  DATABASE_URL         -- same Supabase connection as the rest of the app.
"""

import asyncio
import json
import os
import time

import requests
import websockets

import db as database
from value_bet_alerts import API_BASE, log_bet

WS_API_KEY = os.environ.get("ODDS_API_KEY_SHARP", "")
FIRST_API_KEY = os.environ.get("ODDS_API_KEY", "")
WS_URL = f"wss://api.odds-api.io/v3/ws?apiKey={WS_API_KEY}&markets=ML,Spread,Totals&channels=odds"

TARGET_BOOK = "Stake"
SHARP_BOOK = "BetOnline.ag"

# Same sport list as the bets table's distinct values; slugged the same way
# grade_bets() does it elsewhere in the app (lowercase + hyphenate).
SPORT_SLUGS = [
    "baseball", "basketball", "football", "tennis", "esports",
    "cricket", "american-football", "handball", "volleyball",
]
METADATA_REFRESH_INTERVAL = 900  # seconds (15 min). This shares ODDS_API_KEY's 100 req/hr
                                   # cap with the main worker's Polymarket polling + grading
                                   # (~57/hr already). 9 sport slugs x 4 refreshes/hr = 36/hr,
                                   # leaving headroom. A shorter interval here was hitting 429s.

# event_cache[event_id] = {"home":..., "away":..., "sport":..., "league":...}
event_cache: dict[int, dict] = {}

# odds_state[event_id][bookie][market_name][line_key] = {"odds": {...}, "url": ...}
# line_key is None for ML, the hdp/line value otherwise.
odds_state: dict = {}


def refresh_metadata_cache() -> None:
    """Pull Stake's currently pending/live events per sport via REST (first key) to
    resolve team names/sport/league -- the WebSocket only gives a numeric event id."""
    new_cache: dict[int, dict] = {}
    for slug in SPORT_SLUGS:
        try:
            resp = requests.get(
                f"{API_BASE}/events",
                params={"apiKey": FIRST_API_KEY, "sport": slug, "bookmaker": TARGET_BOOK, "status": "pending,live"},
                timeout=20,
            )
            resp.raise_for_status()
            for e in resp.json():
                new_cache[e["id"]] = {
                    "home": e.get("home"),
                    "away": e.get("away"),
                    "sport": (e.get("sport") or {}).get("name"),
                    "league": (e.get("league") or {}).get("name"),
                }
        except requests.RequestException as exc:
            print(f"  metadata refresh error ({slug}): {exc}")
    event_cache.clear()
    event_cache.update(new_cache)
    print(f"  metadata cache refreshed: {len(event_cache)} events")


def devig_power(implied_probs: list[float]) -> list[float]:
    """Power-method devig: find k such that sum(p_i^(1/k)) == 1, then true_p_i =
    p_i^(1/k) / sum. Falls back to returning the input unchanged for degenerate
    single-outcome cases."""
    if len(implied_probs) <= 1:
        return implied_probs
    lo, hi = 0.01, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        total = sum(p ** (1 / mid) for p in implied_probs)
        if total > 1:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2
    adjusted = [p ** (1 / k) for p in implied_probs]
    s = sum(adjusted)
    return [a / s for a in adjusted]


def build_bet_id(event_id: int, market_name: str, side: str, hdp) -> str:
    base = f"{event_id}-{market_name}-{side}-{TARGET_BOOK}"
    return f"{base}-{hdp}" if hdp is not None else base


def maybe_signal(conn, event_id: int, market_name: str, line_key, event_date: str) -> None:
    """Compare Stake vs BetOnline for this exact event+market+line. Logs a bet if
    Stake offers positive EV against the devigged sharp price."""
    event_odds = odds_state.get(event_id, {})
    stake_row = event_odds.get(TARGET_BOOK, {}).get(market_name, {}).get(line_key)
    sharp_row = event_odds.get(SHARP_BOOK, {}).get(market_name, {}).get(line_key)
    if not stake_row or not sharp_row:
        return

    # Totals uses over/under fields; ML and Spread use home/draw/away.
    if market_name == "Totals":
        sides = [s for s in ("over", "under") if sharp_row["odds"].get(s)]
    else:
        sides = [s for s in ("home", "draw", "away") if sharp_row["odds"].get(s)]
    if len(sides) < 2:
        return

    try:
        sharp_odds = [float(sharp_row["odds"][s]) for s in sides]
        implied = [1 / o for o in sharp_odds]
    except (TypeError, ValueError, ZeroDivisionError):
        return
    true_probs = devig_power(implied)

    meta = event_cache.get(event_id)
    if not meta:
        return  # not in cache yet -- will be retried on a future odds update

    for side, true_p in zip(sides, true_probs):
        stake_odds_raw = stake_row["odds"].get(side)
        if not stake_odds_raw:
            continue
        try:
            stake_odds_val = float(stake_odds_raw)
        except (TypeError, ValueError):
            continue
        ev_frac = stake_odds_val * true_p - 1
        if ev_frac <= 0:
            continue

        # The rest of the app stores Totals bet_side as home=Over, away=Under
        # (see dashboard.py's sideLabel()/TOTALS_MARKETS) -- match that convention.
        bet_side = side
        if market_name == "Totals":
            bet_side = "home" if side == "over" else "away"

        hdp = line_key
        bet_id = build_bet_id(event_id, market_name, bet_side, hdp)

        bet_dict = {
            "id": bet_id,
            "eventId": event_id,
            "bookmaker": TARGET_BOOK,
            "betSide": bet_side,
            "market": {"name": market_name, "hdp": hdp},
            "bookmakerOdds": {bet_side: stake_odds_val, "href": stake_row.get("url")},
            "expectedValue": 100 + ev_frac * 100,
            "event": {
                "home": meta.get("home"),
                "away": meta.get("away"),
                "sport": meta.get("sport"),
                "league": meta.get("league"),
                "date": event_date,
            },
        }
        try:
            log_bet(conn, bet_dict)
            print(
                f"  [SIGNAL] {meta.get('home')} vs {meta.get('away')} | {market_name}"
                f"{f' ({hdp})' if hdp is not None else ''} {bet_side} @ {stake_odds_val} "
                f"| EV={ev_frac * 100:+.1f}% vs sharp {sharp_odds}"
            )
        except Exception as exc:
            print(f"  log_bet error: {exc}")


def handle_message(conn, msg: dict) -> None:
    if msg.get("type") not in ("created", "updated"):
        return
    bookie = msg.get("bookie")
    if bookie not in (TARGET_BOOK, SHARP_BOOK):
        return
    try:
        event_id = int(msg["id"])
    except (KeyError, TypeError, ValueError):
        return
    event_date = msg.get("date")

    for m in msg.get("markets", []):
        market_name = m.get("name")
        if market_name not in ("ML", "Spread", "Totals"):
            continue
        for odds_row in m.get("odds", []):
            line_key = odds_row.get("hdp")  # None for ML
            odds_state.setdefault(event_id, {}).setdefault(bookie, {}).setdefault(market_name, {})[line_key] = {
                "odds": odds_row,
                "url": msg.get("url"),
            }
            maybe_signal(conn, event_id, market_name, line_key, event_date)


async def metadata_refresh_loop() -> None:
    loop = asyncio.get_event_loop()
    while True:
        await loop.run_in_executor(None, refresh_metadata_cache)
        await asyncio.sleep(METADATA_REFRESH_INTERVAL)


async def run() -> None:
    conn = database.init_db()
    asyncio.create_task(metadata_refresh_loop())

    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                print("Connected to OddsAPI WebSocket")
                async for raw in ws:
                    for line in raw.strip().split("\n"):
                        if not line.strip():
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("type") == "welcome":
                            print(f"  welcome: bookmakers={msg.get('bookmakers')}")
                            continue
                        if msg.get("type") == "resync_required":
                            print(f"  resync_required: {msg}")
                            continue
                        handle_message(conn, msg)
        except Exception as exc:
            print(f"  WebSocket error: {exc}; reconnecting in 5s")
            try:
                conn.rollback()
            except Exception:
                pass
            await asyncio.sleep(5)


if __name__ == "__main__":
    if not WS_API_KEY:
        raise SystemExit("Error: set ODDS_API_KEY_SHARP environment variable")
    if not FIRST_API_KEY:
        raise SystemExit("Error: set ODDS_API_KEY environment variable")
    print(f"Stake-vs-{SHARP_BOOK} WebSocket signal scanner starting...")
    asyncio.run(run())
