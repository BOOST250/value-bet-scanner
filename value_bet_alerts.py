"""
Polls the Odds-API.io /value-bets endpoint, logs bets to SQLite,
grades them against settled results, and sends Discord alerts.
"""

import os
import sys
import time
import requests
from datetime import datetime, timezone

import db as database

API_BASE = "https://api.odds-api.io/v3"
POLL_INTERVAL = 90   # seconds between checks -- 2 bookmakers/cycle means <72s would alone
                      # exceed the 100 req/hr cap before grading even runs
GRADE_EVERY_N = 16   # grade settled bets every Nth cycle (~24 min) -- kept slow so the
                      # faster fetch loop has rate-limit headroom; grading isn't time-sensitive

API_KEY = os.environ.get("ODDS_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

DEFAULT_BOOKMAKERS = ["Polymarket"]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def load_seen_ids(conn) -> set[str]:
    rows = database.fetchall(conn, "SELECT id FROM bets")
    return {r["id"] for r in rows}


# The API names this market "Moneyline" for esports and "ML" everywhere else, even
# though it's the same bet type. Normalize at write time so it isn't fragmented across
# two rows in every breakdown/aggregation downstream.
MARKET_NAME_ALIASES = {"Moneyline": "ML", "1X2": "ML"}


def log_bet(conn, bet: dict) -> None:
    event = bet.get("event", {})
    market = bet.get("market", {})
    odds_obj = bet.get("bookmakerOdds", {})
    side = bet.get("betSide", "")
    market_name = market.get("name")
    market_name = MARKET_NAME_ALIASES.get(market_name, market_name)
    database.execute(
        conn,
        """INSERT OR IGNORE INTO bets
           (id, event_id, bookmaker, bet_side, market, hdp, odds, expected_value,
            home, away, sport, league, match_date, detected_at, event_url)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            bet["id"],
            bet.get("eventId"),
            bet.get("bookmaker"),
            side,
            market_name,
            market.get("hdp"),
            float(odds_obj.get(side, 0)),
            bet.get("expectedValue"),
            event.get("home"),
            event.get("away"),
            event.get("sport"),
            event.get("league"),
            event.get("date"),
            datetime.now(timezone.utc).isoformat(),
            odds_obj.get("href"),
        ),
    )
    database.commit(conn)

# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def grade_ml(home_score: int, away_score: int, bet_side: str) -> str:
    if home_score == away_score:
        winner = "draw"
    else:
        winner = "home" if home_score > away_score else "away"
    return "won" if bet_side == winner else "lost"


def grade_spread(home_score: int, away_score: int, bet_side: str, hdp: float) -> str:
    if bet_side == "home":
        diff = (home_score + hdp) - away_score
    else:
        diff = (away_score + hdp) - home_score
    if diff > 0:
        return "won"
    if diff == 0:
        return "push"
    return "lost"


def grade_totals(home_score: int, away_score: int, bet_side: str, hdp: float) -> str:
    total = home_score + away_score
    if total > hdp:
        return "won" if bet_side == "home" else "lost"
    if total < hdp:
        return "lost" if bet_side == "home" else "won"
    return "push"


def grade_bet_row(row, home_score: int, away_score: int, ht_score: tuple[int, int] | None = None) -> str:
    market = row["market"]
    side = row["bet_side"]
    if market in ("ML", "1X2", "Moneyline"):
        return grade_ml(home_score, away_score, side)
    if market in ("Spread", "Map Handicap"):
        return grade_spread(home_score, away_score, side, row["hdp"] or 0)
    if market in ("Totals", "Totals (Games)", "Total Maps"):
        return grade_totals(home_score, away_score, side, row["hdp"] or 0)
    if market == "Totals HT":
        if ht_score is None:
            return "void"
        return grade_totals(ht_score[0], ht_score[1], side, row["hdp"] or 0)
    return "void"


def grade_bets(conn) -> None:
    pending = database.fetchall(
        conn,
        "SELECT * FROM bets WHERE status = 'pending' AND match_date < ?",
        (datetime.now(timezone.utc).isoformat(),),
    )

    if not pending:
        return

    by_sport: dict[str, list] = {}
    for row in pending:
        slug = (row["sport"] or "").lower().replace(" ", "-")
        by_sport.setdefault(slug, []).append(row)

    now = datetime.now(timezone.utc).isoformat()
    graded_count = 0

    for sport_slug, rows in by_sport.items():
        if not sport_slug:
            continue
        try:
            resp = requests.get(
                f"{API_BASE}/events",
                params={"sport": sport_slug, "apiKey": API_KEY, "status": "settled"},
                timeout=30,
            )
            resp.raise_for_status()
            settled = {e["id"]: e for e in resp.json()}
        except requests.HTTPError:
            continue

        for row in rows:
            event = settled.get(row["event_id"])
            if not event:
                continue
            scores = event.get("scores", {})
            periods = scores.get("periods", {})
            final = periods.get("ot") or periods.get("ft")
            hs = final.get("home") if final else scores.get("home")
            aws = final.get("away") if final else scores.get("away")
            if hs is None or aws is None:
                continue

            ht = periods.get("p1")
            ht_score = (ht["home"], ht["away"]) if ht and ht.get("home") is not None and ht.get("away") is not None else None

            # A 0-0 final score is impossible for a completed tennis match (minimum is
            # 2-0 sets) -- it means the match was cancelled/walkover/never played, even
            # though the API reports status="settled" with no separate flag for that.
            if row["sport"] == "Tennis" and int(hs) == 0 and int(aws) == 0:
                result = "void"
            else:
                result = grade_bet_row(row, int(hs), int(aws), ht_score)
            database.execute(
                conn,
                "UPDATE bets SET status=?, home_score=?, away_score=?, graded_at=? WHERE id=?",
                (result, int(hs), int(aws), now, row["id"]),
            )
            graded_count += 1
            print(f"  Graded: {row['home']} vs {row['away']} | {row['bet_side']} {row['market']} -> {result} ({hs}-{aws})")

    if graded_count:
        database.commit(conn)
        print(f"  Graded {graded_count} bet(s)")

# ---------------------------------------------------------------------------
# CLV (closing line value) vs Pinnacle, via the BettingIsCool API
# ---------------------------------------------------------------------------

BIC_API_KEY = os.environ.get("BETTINGISCOOL_API_KEY", "")
BIC_BASE = "https://api.bettingiscool.com"
CLV_EVERY_N = 10        # run every 10th cycle (~15 min) -- closing lines don't go stale,
                         # so this only needs to keep up with newly-settled bets
CLV_BATCH_LIMIT = 300    # cap per run so a big backlog spreads across several cycles
                         # instead of one long-running call

# Team-sport markets match cleanly to Pinnacle's moneyline/spread/totals on a single
# fixture. Tennis is special-cased below: ML maps to the plain fixture's "moneyline",
# Totals (Games) maps to a separate "(Games)"-suffixed fixture's "totals" (period 0).
# Spread and sets-based Totals are skipped for Tennis -- Pinnacle doesn't offer a
# comparable sets-handicap market for ITF-tier matches.
CLV_SPORT_IDS = {"Baseball": 3, "Basketball": 4, "Football": 29, "Tennis": 33}
CLV_TEAM_MARKETS = {"ML": "moneyline", "Spread": "spread", "Totals": "totals"}
CLV_STOPWORDS = {"the", "fc", "sc", "cf", "afc", "club", "city", "united", "town", "games"}


def _clv_name_tokens(name: str) -> set:
    import re
    words = re.findall(r"[a-z]+", (name or "").lower())
    return {w for w in words if len(w) >= 3 and w not in CLV_STOPWORDS}


def _bic_get(path: str, params: dict):
    try:
        resp = requests.get(f"{BIC_BASE}{path}", params=params, headers={"X-API-Key": BIC_API_KEY}, timeout=30)
        time.sleep(0.15)
        if resp.status_code != 200:
            return None
        return resp.json()
    except requests.RequestException:
        return None


def fetch_clv(conn) -> None:
    if not BIC_API_KEY:
        return

    rows = database.fetchall(conn, """
        SELECT id, sport, market, hdp, bet_side, odds, home, away, match_date
        FROM bets
        WHERE status IN ('won','lost','push')
          AND clv_raw IS NULL
          AND sport IN ('Baseball','Basketball','Football','Tennis')
          AND (
            (sport != 'Tennis' AND market IN ('ML','Spread','Totals'))
            OR (sport = 'Tennis' AND market IN ('ML','Totals (Games)'))
          )
        ORDER BY match_date
        LIMIT ?
    """, (CLV_BATCH_LIMIT,))
    if not rows:
        return

    by_day: dict[tuple, list] = {}
    for r in rows:
        day = r["match_date"][:10]
        by_day.setdefault((r["sport"], day), []).append(r)

    fixtures_cache: dict[tuple, list] = {}
    for sport, day in by_day:
        if (sport, day) in fixtures_cache:
            continue
        data = _bic_get("/api/fixtures", {
            "sport_id": CLV_SPORT_IDS[sport],
            "starts_from": day,
            "starts_to": day,
            "limit": 1000,
            "closing_available": 1,
        })
        fixtures_cache[(sport, day)] = data or []

    matched: list[tuple] = []
    for (sport, day), day_rows in by_day.items():
        fixtures = fixtures_cache.get((sport, day), [])
        for r in day_rows:
            is_games = r["market"] == "Totals (Games)"
            cands = [f for f in fixtures if ("(Games)" in f["runner_home"]) == is_games] if sport == "Tennis" else fixtures
            home_tok = _clv_name_tokens(r["home"])
            away_tok = _clv_name_tokens(r["away"])
            hits = [
                f for f in cands
                if home_tok & _clv_name_tokens(f["runner_home"]) and away_tok & _clv_name_tokens(f["runner_away"])
            ]
            if len(hits) == 1:
                matched.append((r, hits[0]["event_id"]))

    odds_cache: dict[int, list] = {}
    for _, eid in matched:
        if eid not in odds_cache:
            odds_cache[eid] = _bic_get("/api/odds", {"event_id": eid}) or []

    updated = 0
    now = datetime.now(timezone.utc).isoformat()
    for r, eid in matched:
        odds_rows = odds_cache.get(eid, [])
        if r["sport"] == "Tennis":
            if r["market"] == "ML":
                cands = [o for o in odds_rows if o["market"] == "moneyline"]
            else:
                cands = [
                    o for o in odds_rows
                    if o["market"] == "totals" and o.get("period") == 0
                    and o.get("line") is not None and r["hdp"] is not None and abs(o["line"] - r["hdp"]) < 0.01
                ]
        else:
            market_name = CLV_TEAM_MARKETS[r["market"]]
            cands = [o for o in odds_rows if o["market"] == market_name]
            if r["market"] in ("Spread", "Totals") and r["hdp"] is not None:
                cands = [o for o in cands if o.get("line") is not None and abs(abs(o["line"]) - abs(r["hdp"])) < 0.01]
        if not cands:
            continue
        row = cands[0]
        side = r["bet_side"]
        raw, true = (row.get("odds1"), row.get("todds1")) if side == "home" else (row.get("odds2"), row.get("todds2"))
        if raw is None:
            continue
        clv_raw = (r["odds"] / raw - 1) * 100
        clv_true = (r["odds"] / true - 1) * 100 if true else None
        database.execute(
            conn,
            "UPDATE bets SET clv_raw=?, clv_true=?, closing_odds=? WHERE id=?",
            (clv_raw, clv_true, raw, r["id"]),
        )
        updated += 1

    if updated:
        database.commit(conn)
        print(f"  CLV computed for {updated} bet(s) ({len(rows)} candidates, {len(matched)} matched)")

# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(conn) -> None:
    row = database.fetchone(conn, "SELECT COUNT(*) c FROM bets")
    total = row["c"]
    if total == 0:
        print("No bets tracked yet.")
        return

    counts = {}
    for row in database.fetchall(conn, "SELECT status, COUNT(*) c FROM bets GROUP BY status"):
        counts[row["status"]] = row["c"]

    settled = counts.get("won", 0) + counts.get("lost", 0) + counts.get("push", 0)
    wins = counts.get("won", 0)
    losses = counts.get("lost", 0)

    print(f"\n{'='*50}")
    print(f"  TOTAL BETS: {total}")
    print(f"  Pending: {counts.get('pending', 0)}  |  Won: {wins}  |  Lost: {losses}  |  Push: {counts.get('push', 0)}  |  Void: {counts.get('void', 0)}")

    if settled > 0:
        won_rows = database.fetchall(conn, "SELECT odds FROM bets WHERE status='won'")
        profit = sum(r["odds"] - 1 for r in won_rows)
        roi = (profit - losses) / settled * 100
        win_rate = wins / settled * 100
        print(f"  Win rate: {win_rate:.1f}%  |  ROI: {roi:+.1f}%")

    print(f"\n  By bookmaker:")
    for row in database.fetchall(conn, """
        SELECT bookmaker,
               COUNT(*) total,
               SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) w,
               SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) l
        FROM bets GROUP BY bookmaker
    """):
        s = row["w"] + row["l"]
        wr = f"{row['w']/s*100:.0f}%" if s else "n/a"
        print(f"    {row['bookmaker']}: {row['total']} bets, {row['w']}W/{row['l']}L (win rate {wr})")

    print(f"\n  By EV bucket:")
    for label, lo, hi in [("5-10%", 105, 110), ("10-20%", 110, 120), ("20%+", 120, 9999)]:
        row = database.fetchone(conn, """
            SELECT COUNT(*) total,
                   COALESCE(SUM(CASE WHEN status='won' THEN 1 ELSE 0 END), 0) w,
                   COALESCE(SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END), 0) l
            FROM bets WHERE expected_value >= ? AND expected_value < ?
        """, (lo, hi))
        s = row["w"] + row["l"]
        wr = f"{row['w']/s*100:.0f}%" if s else "n/a"
        print(f"    {label}: {row['total']} bets, {row['w']}W/{row['l']}L (win rate {wr})")
    print(f"{'='*50}\n")

# ---------------------------------------------------------------------------
# API + Discord
# ---------------------------------------------------------------------------

def fetch_value_bets(bookmaker: str, sport: str | None = None) -> list[dict]:
    params = {
        "apiKey": API_KEY,
        "bookmaker": bookmaker,
        "includeEventDetails": "true",
    }
    if sport:
        params["sport"] = sport
    resp = requests.get(f"{API_BASE}/value-bets", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def filter_bets(bets: list[dict]) -> list[dict]:
    return [b for b in bets if b.get("expectedValue", 0) > 100]


def format_discord_embed(bet: dict) -> dict:
    ev_pct = bet["expectedValue"] - 100
    event = bet.get("event", {})
    odds = bet.get("bookmakerOdds", {})
    market = bet.get("market", {})

    home = event.get("home", "?")
    away = event.get("away", "?")
    match_date = event.get("date", "")
    if match_date:
        dt = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
        match_date = dt.strftime("%b %d %H:%M UTC")

    side = bet.get("betSide", "?")
    book_odds = odds.get(side, "?")

    return {
        "title": f"\U0001f514 {home} vs {away}",
        "color": 0x00CC66,
        "fields": [
            {"name": "Sport / League", "value": f"{event.get('sport', '?')} — {event.get('league', '?')}", "inline": False},
            {"name": "Bet Side", "value": side.capitalize(), "inline": True},
            {"name": "Market", "value": market.get("name", "?"), "inline": True},
            {"name": "Odds", "value": str(book_odds), "inline": True},
            {"name": "Expected Value", "value": f"**+{ev_pct:.1f}%**", "inline": True},
            {"name": "Bookmaker", "value": bet.get("bookmaker", "?"), "inline": True},
            {"name": "Kick-off", "value": match_date or "?", "inline": True},
        ],
        "footer": {"text": f"Detected {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"},
    }


def send_discord_alert(bets: list[dict]) -> None:
    embeds = [format_discord_embed(b) for b in bets[:10]]
    payload = {
        "username": "Value Bet Scanner",
        "content": f"**{len(bets)} new value bet(s) found**",
        "embeds": embeds,
    }
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    resp.raise_for_status()
    print(f"  Discord alert sent for {len(bets)} bet(s)")

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once(conn, seen_ids: set[str], bookmakers: list[str], sport: str | None = None, do_grade: bool = True, do_clv: bool = False) -> int:
    all_new: list[dict] = []

    for bookmaker in bookmakers:
        try:
            bets = fetch_value_bets(bookmaker, sport)
            high_ev = filter_bets(bets)
            new_bets = [b for b in high_ev if b["id"] not in seen_ids]
            all_new.extend(new_bets)
            for b in new_bets:
                seen_ids.add(b["id"])
        except requests.HTTPError as e:
            print(f"  API error ({bookmaker}): {e}")

    if not all_new:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] No new value bets")
    else:
        for b in all_new:
            ev_pct = b["expectedValue"]
            event = b.get("event", {})
            print(f"  +{ev_pct:.1f}% EV | {b.get('bookmaker','?')} | {event.get('home','?')} vs {event.get('away','?')} | {b.get('betSide','?')} @ {b.get('bookmakerOdds',{}).get(b.get('betSide',''),'?')}")
            log_bet(conn, b)

        if DISCORD_WEBHOOK_URL:
            send_discord_alert(all_new)
        else:
            print("  (DISCORD_WEBHOOK_URL not set -- skipping alert)")

    if do_grade:
        grade_bets(conn)
    if do_clv:
        fetch_clv(conn)
    return len(all_new)


def main():
    if "--stats" in sys.argv:
        conn = database.init_db()
        print_stats(conn)
        database.close(conn)
        return

    if not API_KEY:
        sys.exit("Error: set ODDS_API_KEY environment variable")

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    bookmakers = args[0].split(",") if args else DEFAULT_BOOKMAKERS
    sport = args[1] if len(args) > 1 else None

    conn = database.init_db()
    seen_ids = load_seen_ids(conn)
    print(f"Value-bet scanner started | bookmakers={','.join(bookmakers)} sport={sport or 'all'}")
    print(f"Loaded {len(seen_ids)} previously seen bets from DB")
    print(f"Polling every {POLL_INTERVAL}s, grading every {GRADE_EVERY_N} cycles, "
          f"CLV every {CLV_EVERY_N} cycles ({'on' if BIC_API_KEY else 'off -- no BETTINGISCOOL_API_KEY'}). "
          f"Press Ctrl+C to stop.\n")

    cycle = 0
    while True:
        cycle += 1
        do_grade = (cycle % GRADE_EVERY_N == 0)
        do_clv = (cycle % CLV_EVERY_N == 0)
        try:
            run_once(conn, seen_ids, bookmakers, sport, do_grade=do_grade, do_clv=do_clv)
        except Exception as e:
            print(f"  Unexpected error: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
