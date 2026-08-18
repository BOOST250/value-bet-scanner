"""
Web dashboard for the value bet scanner.
Run alongside value_bet_alerts.py — reads from the shared bets.db.

Usage: python dashboard.py
Then open http://localhost:5000
"""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

import db as database

try:
    database.init_db()
except Exception as _db_init_err:
    import sys
    print(f"WARNING: DB init failed at startup ({_db_init_err}), will retry on first request", file=sys.stderr)

app = Flask(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
_CACHE_TTL = 30  # seconds
_EVENT_CACHE: dict[str, tuple[float, list | None]] = {}
_BOOK_CACHE: dict[str, tuple[float, dict | None]] = {}
_KALSHI_CACHE: dict[str, tuple[float, list | None]] = {}
_KALSHI_LAST_TRY: dict[str, float] = {}


def _cached_get(cache: dict, key: str, url: str, params: dict | None = None):
    cached = cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]
    try:
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        data = None
    cache[key] = (time.time(), data)
    return data


def get_event_markets(slug: str) -> list | None:
    data = _cached_get(_EVENT_CACHE, slug, f"{GAMMA_API}/events/slug/{slug}")
    return data.get("markets", []) if data else None


def get_order_book(token_id: str) -> dict | None:
    return _cached_get(_BOOK_CACHE, token_id, f"{CLOB_API}/book", {"token_id": token_id})


def _trailing_number(title: str | None) -> float | None:
    if not title:
        return None
    m = re.search(r"[-+]?\d+\.?\d*$", title)
    return abs(float(m.group())) if m else None


def _disambiguate_by_question(candidates: list, bet_side: str, hdp: float, home: str, away: str) -> dict | None:
    """When multiple sub-markets share a title (e.g. a handicap split into two directional
    markets), use the question text -- which names the favored team right next to its
    signed line, e.g. "Vasilev (-1.5) vs Kopp (+1.5)" -- to find the one matching our side.
    Requires the sign to sit immediately next to our team's name, not just appear anywhere
    in the text, since both directional variants mention both players and both signs."""
    team_tokens = _name_tokens(home if bet_side == "home" else away)
    if not team_tokens:
        return None
    sign = "-" if hdp < 0 else "+"
    number = f"{abs(float(hdp))}".rstrip("0").rstrip(".")
    hits = []
    for m in candidates:
        q = (m.get("question") or "").lower().replace(" ", "")
        if any(f"{tok}({sign}{number}" in q for tok in team_tokens):
            hits.append(m)
    return hits[0] if len(hits) == 1 else None


def _find_market(markets: list, market_name: str, hdp, bet_side: str = "", home: str = "", away: str = "") -> dict | None:
    """Return the one sub-market matching our market+hdp, or None if zero/ambiguous matches."""
    if market_name in ("ML", "1X2", "Moneyline"):
        matches = [m for m in markets if not m.get("groupItemTitle")]
        if len(matches) == 1:
            return matches[0]
        winner_market = [m for m in markets if (m.get("groupItemTitle") or "").lower() == "match winner"]
        return winner_market[0] if len(winner_market) == 1 else None

    if market_name in ("Spread", "Map Handicap") and hdp is not None:
        target = abs(float(hdp))
        matches = [
            m for m in markets
            if any(kw in (m.get("groupItemTitle") or "").lower() for kw in ("spread", "handicap"))
            and "inning" not in (m.get("groupItemTitle") or "").lower()
            and _trailing_number(m.get("groupItemTitle")) == target
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return _disambiguate_by_question(matches, bet_side, float(hdp), home, away)
        return None

    if market_name in ("Totals", "Totals (Games)", "Total Maps") and hdp is not None:
        target = abs(float(hdp))
        matches = [
            m for m in markets
            if "o/u" in (m.get("groupItemTitle") or "").lower()
            and "inning" not in (m.get("groupItemTitle") or "").lower()
            and _trailing_number(m.get("groupItemTitle")) == target
        ]
        return matches[0] if len(matches) == 1 else None

    return None


_STOPWORDS = {"the", "fc", "sc", "cf", "afc", "club"}


def _name_tokens(name: str) -> set:
    words = re.findall(r"[a-z]+", name.lower())
    return {w for w in words if len(w) >= 4 and w not in _STOPWORDS}


def _parse_json_field(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return []
    return value or []


def _outcome_token(market: dict, market_name: str, bet_side: str, home: str, away: str) -> str | None:
    outcomes = _parse_json_field(market.get("outcomes"))
    token_ids = _parse_json_field(market.get("clobTokenIds"))
    if len(outcomes) != 2 or len(token_ids) != 2:
        return None

    if market_name in ("Totals", "Totals (Games)", "Total Maps", "Totals HT"):
        lowered = [o.lower() for o in outcomes]
        over_idx = [i for i, o in enumerate(lowered) if o.startswith("over")]
        under_idx = [i for i, o in enumerate(lowered) if o.startswith("under")]
        if len(over_idx) != 1 or len(under_idx) != 1:
            return None
        return token_ids[over_idx[0] if bet_side == "home" else under_idx[0]]

    target = _name_tokens(home if bet_side == "home" else away)
    if not target:
        return None
    hits = [i for i, o in enumerate(outcomes) if target & _name_tokens(o)]
    return token_ids[hits[0]] if len(hits) == 1 else None


def _best_ask_depth(book: dict | None) -> tuple[float, float] | tuple[None, None]:
    """Returns (price, dollar size) at the best (lowest) ask, i.e. what you'd pay right now."""
    if not book:
        return None, None
    asks = book.get("asks") or []
    if not asks:
        return None, None
    try:
        best = min(asks, key=lambda a: float(a["price"]))
        price = float(best["price"])
        size = float(best["size"])
    except (KeyError, TypeError, ValueError):
        return None, None
    return price, round(size * price, 2)


def get_kalshi_markets(event_ticker: str) -> list | None:
    data = _cached_get(_KALSHI_CACHE, event_ticker, f"{KALSHI_API}/markets", {"event_ticker": event_ticker})
    return data.get("markets", []) if data else None


_kalshi_refresh_lock = threading.Lock()


def _kick_off_kalshi_refresh(tickers: list) -> None:
    """Fetch these tickers serially in a background thread, never blocking the caller.
    Skips entirely if a previous sweep is still running so refreshes don't pile up."""
    if not tickers or not _kalshi_refresh_lock.acquire(blocking=False):
        return

    def run():
        try:
            for t in tickers:
                _KALSHI_LAST_TRY[t] = time.time()
                get_kalshi_markets(t)
        finally:
            _kalshi_refresh_lock.release()

    threading.Thread(target=run, daemon=True).start()


def _kalshi_outcome_market(markets: list, bet_side: str, home: str, away: str) -> dict | None:
    """ML-only: each Kalshi market here is a separate binary 'Will X win?' contract, one
    per team, with no draw market in this set -- so a clean single-team-name match works."""
    target = _name_tokens(home if bet_side == "home" else away)
    if not target:
        return None
    hits = [m for m in markets if target & _name_tokens(m.get("yes_sub_title") or m.get("title") or "")]
    return hits[0] if len(hits) == 1 else None


def _kalshi_best_ask(market: dict | None) -> tuple[float, float] | tuple[None, None]:
    if not market:
        return None, None
    try:
        price = float(market["yes_ask_dollars"])
        size = float(market["yes_ask_size_fp"])
    except (KeyError, TypeError, ValueError):
        return None, None
    if price <= 0 or size <= 0:
        return None, None
    return price, round(size * price, 2)


def attach_liquidity(bets: list) -> list:
    for b in bets:
        b["liquidity"] = None
        b["liquidity_price"] = None

    # Polymarket CLOB/Gamma APIs now require a paid plan — skip liquidity fetching.

    # Kalshi rate-limits on concurrency (~1 in-flight request per IP), not request volume --
    # any parallelism here returns a wave of 429s -- and the Railway-to-Kalshi network path
    # measured far slower in production than locally (one run took 36s for a 15-ticker serial
    # sweep that took ~4s from a home connection). Rather than ever block the request on that
    # unpredictable latency, kick off a background sweep (capped, oldest-tried-first, one at a
    # time via a lock so refreshes don't stack) and only ever read whatever's already cached.
    # Coverage fills in over a few of the dashboard's 15s auto-refresh polls instead of holding
    # up any single one of them.
    KALSHI_FETCH_CAP = 15
    kalshi_bets = [b for b in bets if b.get("bookmaker") == "Kalshi" and b.get("market") == "ML" and b.get("event_url")]
    tickers = {b["event_url"].rstrip("/").rsplit("/", 1)[-1].upper() for b in kalshi_bets}
    uncached_tickers = [
        t for t in tickers if not (_KALSHI_CACHE.get(t) and time.time() - _KALSHI_CACHE[t][0] < _CACHE_TTL)
    ]
    uncached_tickers.sort(key=lambda t: _KALSHI_LAST_TRY.get(t, 0.0))
    _kick_off_kalshi_refresh(uncached_tickers[:KALSHI_FETCH_CAP])

    for b in kalshi_bets:
        ticker = b["event_url"].rstrip("/").rsplit("/", 1)[-1].upper()
        cached = _KALSHI_CACHE.get(ticker)
        if not cached or not cached[1]:
            continue
        markets = cached[1].get("markets", [])
        if not markets:
            continue
        market = _kalshi_outcome_market(markets, b.get("bet_side"), b.get("home"), b.get("away"))
        price, size = _kalshi_best_ask(market)
        b["liquidity"] = size
        b["liquidity_price"] = price

    return bets


@app.route("/")
def index():
    return INDEX_HTML


EV_BUCKETS = [("0-0.5%", 100, 100.5), ("0.5-1%", 100.5, 101), ("1-2%", 101, 102),
              ("2-5%", 102, 105), ("5-10%", 105, 110), ("10-20%", 110, 120), ("20%+", 120, 9999)]
ODDS_BUCKETS = [("1.01-1.30", 1.01, 1.30), ("1.30-1.60", 1.30, 1.60),
                ("1.60-2.00", 1.60, 2.00), ("2.00-2.50", 2.00, 2.50),
                ("2.50-3.50", 2.50, 3.50), ("3.50+", 3.50, 9999)]


def apply_filters(bets: list) -> list:
    # Multi-select preference filters (sport/bookmaker/market checkboxes, EV/odds bucket
    # checkboxes) sent as repeated query params -- an empty list for a group means "no
    # filter" (show everything), matching how the dashboard's preferences popup behaves.
    sports = request.args.getlist("sport")
    bookmakers = request.args.getlist("bookmaker")
    markets = request.args.getlist("market")
    ev_bucket_labels = request.args.getlist("ev_bucket")
    odds_bucket_labels = request.args.getlist("odds_bucket")

    if sports:
        bets = [b for b in bets if b["sport"] in sports]
    if bookmakers:
        bets = [b for b in bets if b["bookmaker"] in bookmakers]
    if markets:
        bets = [b for b in bets if b["market"] in markets]
    if ev_bucket_labels:
        ranges = [(lo, hi) for label, lo, hi in EV_BUCKETS if label in ev_bucket_labels]
        bets = [
            b for b in bets if b["expected_value"] is not None
            and any(lo <= b["expected_value"] < hi for lo, hi in ranges)
        ]
    if odds_bucket_labels:
        ranges = [(lo, hi) for label, lo, hi in ODDS_BUCKETS if label in odds_bucket_labels]
        bets = [
            b for b in bets if b["odds"] is not None
            and any(lo <= b["odds"] < hi for lo, hi in ranges)
        ]
    return bets


@app.route("/api/live")
def api_live():
    conn = database.get_conn()
    bets = database.fetchall(conn, "SELECT * FROM bets WHERE status='pending' ORDER BY detected_at DESC")
    database.close(conn)
    bets = apply_filters(bets)
    attach_liquidity(bets)
    return jsonify(bets)


@app.route("/api/results")
def api_results():
    conn = database.get_conn()
    bets = database.fetchall(conn, "SELECT * FROM bets WHERE status IN ('won','lost','push','void') ORDER BY graded_at DESC")
    database.close(conn)
    bets = apply_filters(bets)
    return jsonify(bets)


@app.route("/api/track", methods=["POST"])
def api_track():
    data = request.get_json(force=True) or {}
    bet_id = data.get("id")
    if not bet_id:
        return jsonify({"error": "missing id"}), 400
    conn = database.get_conn()
    database.execute(conn, "UPDATE bets SET tracked=? WHERE id=?", (bool(data.get("tracked")), bet_id))
    database.commit(conn)
    database.close(conn)
    return jsonify({"ok": True})


@app.route("/api/grade", methods=["POST"])
def api_grade():
    # Manual override for markets the grader can't settle itself (e.g. Bookings/Corners
    # Totals -- the events feed has no corner/booking counts). Only allowed starting from
    # 'void' so it can't be used to overwrite an already-correct auto-graded result.
    data = request.get_json(force=True) or {}
    bet_id = data.get("id")
    status = data.get("status")
    if not bet_id or status not in ("won", "lost", "push"):
        return jsonify({"error": "missing id or invalid status"}), 400
    conn = database.get_conn()
    row = database.fetchone(conn, "SELECT status FROM bets WHERE id = ?", (bet_id,))
    if not row or row["status"] != "void":
        database.close(conn)
        return jsonify({"error": "bet not found or not void"}), 400
    database.execute(
        conn,
        "UPDATE bets SET status=?, graded_at=? WHERE id=?",
        (status, datetime.now(timezone.utc).isoformat(), bet_id),
    )
    database.commit(conn)
    database.close(conn)
    return jsonify({"ok": True})


@app.route("/api/tracker")
def api_tracker():
    conn = database.get_conn()
    bets = database.fetchall(conn, "SELECT * FROM bets WHERE tracked ORDER BY detected_at DESC")
    database.close(conn)

    settled = sorted(
        (b for b in bets if b["status"] in ("won", "lost", "push")),
        key=lambda b: b["graded_at"] or "",
    )

    def profit_of(b):
        if b["status"] == "won":
            return b["odds"] - 1
        if b["status"] == "lost":
            return -1.0
        return 0.0

    daily: dict[str, float] = {}
    cumulative = []
    running = 0.0
    for b in settled:
        p = profit_of(b)
        running += p
        date = (b["graded_at"] or "")[:10]
        daily[date] = round(daily.get(date, 0.0) + p, 2)
        cumulative.append({"date": b["graded_at"], "profit": round(running, 2)})

    wins = sum(1 for b in settled if b["status"] == "won")
    losses = sum(1 for b in settled if b["status"] == "lost")
    n = wins + losses
    profit = sum(profit_of(b) for b in settled if b["status"] in ("won", "lost"))

    return jsonify({
        "bets": bets,
        "daily": daily,
        "cumulative": cumulative,
        "total": len(bets),
        "pending": sum(1 for b in bets if b["status"] == "pending"),
        "won": wins,
        "lost": losses,
        "win_rate": round(wins / n * 100, 1) if n else 0,
        "roi": round(profit / n * 100, 1) if n else 0,
        "profit_units": round(profit, 2),
    })


# Polymarket charges takers 3% on sports markets; Kalshi charges takers 7% flat.
# Both formulas are fee = feeRate * C * p * (1-p), where C = contracts and p = contract
# price (~implied probability, p = 1/odds). As a fraction of stake the per-contract terms
# collapse to feeRate * (1 - p) -- i.e. fee bites hardest on long-odds underdog bets, not
# coin-flip bets, since longshots require buying many more cheap contracts per dollar staked.
FEE_RATE_SQL = "CASE WHEN bookmaker='Polymarket' THEN 0.03 WHEN bookmaker='Kalshi' THEN 0.07 ELSE 0 END"
FEE_SQL = f"(({FEE_RATE_SQL}) * (1 - 1.0/odds))"

# Betfair Exchange charges 2.5% commission on net winnings only -- a flat cut of profit,
# not the per-contract formula above. No commission on a loss or push (no profit to take
# a cut from).
BETFAIR_COMMISSION_SQL = "CASE WHEN bookmaker='Betfair Exchange' THEN 0.025 * (odds - 1) ELSE 0 END"

NET_PROFIT_SQL = f"""CASE
    WHEN status='won' THEN (odds - 1) - {FEE_SQL} - ({BETFAIR_COMMISSION_SQL})
    WHEN status='lost' THEN -1 - {FEE_SQL}
    WHEN status='push' THEN -{FEE_SQL}
  END"""


@app.route("/api/stats")
def api_stats():
    bm_filter = request.args.get("bookmaker", "")
    sp_filter = request.args.get("sport", "")
    filters = []
    params_base: tuple = ()
    if bm_filter:
        filters.append("bookmaker = ?")
        params_base += (bm_filter,)
    if sp_filter:
        filters.append("sport = ?")
        params_base += (sp_filter,)
    extra_clause = ("AND " + " AND ".join(filters)) if filters else ""
    bm_clause = extra_clause
    bm_params_1 = params_base

    conn = database.get_conn()
    total = database.fetchone(conn, f"SELECT COUNT(*) total FROM bets WHERE 1=1 {bm_clause}", bm_params_1)["total"]
    counts = {}
    for r in database.fetchall(conn, f"SELECT status, COUNT(*) c FROM bets WHERE 1=1 {bm_clause} GROUP BY status", bm_params_1):
        counts[r["status"]] = r["c"]

    wins = counts.get("won", 0)
    losses = counts.get("lost", 0)
    pushes = counts.get("push", 0)
    settled = wins + losses + pushes

    profit = 0
    if wins:
        profit = sum(
            r["odds"] - 1 for r in database.fetchall(conn, f"SELECT odds FROM bets WHERE status='won' {bm_clause}", bm_params_1)
        )
    roi = ((profit - losses) / settled * 100) if settled else 0

    net_profit = database.fetchone(
        conn, f"SELECT COALESCE(SUM({NET_PROFIT_SQL}), 0) net FROM bets WHERE status IN ('won','lost','push') {bm_clause}", bm_params_1
    )["net"]
    net_roi = (net_profit / settled * 100) if settled else 0

    BREAKDOWN_COLS = f"""COUNT(*) total,
               COALESCE(SUM(CASE WHEN status='won' THEN 1 END), 0) w,
               COALESCE(SUM(CASE WHEN status='lost' THEN 1 END), 0) l,
               COALESCE(SUM(CASE WHEN status='pending' THEN 1 END), 0) p,
               COALESCE(SUM(CASE WHEN status='won' THEN odds - 1 END), 0) profit_w,
               COALESCE(SUM(CASE WHEN status='lost' THEN 1 END), 0) loss_units,
               COALESCE(SUM({NET_PROFIT_SQL}), 0) net_profit,
               AVG(clv_true) avg_clv_true,
               COUNT(clv_raw) clv_n"""

    by_bookmaker = database.fetchall(conn, f"SELECT bookmaker, {BREAKDOWN_COLS} FROM bets WHERE 1=1 {bm_clause} GROUP BY bookmaker", bm_params_1)
    by_sport = database.fetchall(conn, f"SELECT sport, {BREAKDOWN_COLS} FROM bets WHERE 1=1 {bm_clause} GROUP BY sport ORDER BY total DESC", bm_params_1)
    by_market = database.fetchall(conn, f"SELECT market, {BREAKDOWN_COLS} FROM bets WHERE 1=1 {bm_clause} GROUP BY market ORDER BY total DESC", bm_params_1)

    ev_buckets = []
    for label, lo, hi in EV_BUCKETS:
        r = database.fetchone(conn, f"""
            SELECT {BREAKDOWN_COLS}
            FROM bets WHERE expected_value >= ? AND expected_value < ? {bm_clause}
        """, (lo, hi) + bm_params_1)
        ev_buckets.append({"label": label, **r})

    odds_buckets = []
    for label, lo, hi in ODDS_BUCKETS:
        r = database.fetchone(conn, f"""
            SELECT {BREAKDOWN_COLS}
            FROM bets WHERE odds >= ? AND odds < ? {bm_clause}
        """, (lo, hi) + bm_params_1)
        odds_buckets.append({"label": label, **r})

    time_buckets = []
    for label, lo_h, hi_h in [("0-1h", 0, 1), ("1-3h", 1, 3), ("3-6h", 3, 6),
                                ("6-12h", 6, 12), ("12-24h", 12, 24), ("24h+", 24, 99999)]:
        r = database.fetchone(conn, f"""
            SELECT {BREAKDOWN_COLS}
            FROM bets
            WHERE match_date IS NOT NULL AND detected_at IS NOT NULL
              AND (julianday(match_date) - julianday(detected_at)) * 24 >= ?
              AND (julianday(match_date) - julianday(detected_at)) * 24 < ?
              {bm_clause}
        """, (lo_h, hi_h) + bm_params_1)
        time_buckets.append({"label": label, **r})

    database.close(conn)
    return jsonify({
        "total": total,
        "pending": counts.get("pending", 0),
        "won": wins,
        "lost": losses,
        "push": pushes,
        "void": counts.get("void", 0),
        "settled": settled,
        "win_rate": round(wins / settled * 100, 1) if settled else 0,
        "roi": round(roi, 1),
        "profit_units": round(profit - losses, 2),
        "net_roi": round(net_roi, 1),
        "net_profit_units": round(net_profit, 2),
        "by_bookmaker": by_bookmaker,
        "by_sport": by_sport,
        "by_market": by_market,
        "ev_buckets": ev_buckets,
        "odds_buckets": odds_buckets,
        "time_buckets": time_buckets,
    })


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Value Bet Dashboard</title>
<style>
  :root {
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #2a2d3a;
    --text: #e1e4ea;
    --dim: #8b8fa3;
    --green: #22c55e;
    --red: #ef4444;
    --yellow: #eab308;
    --blue: #3b82f6;
    --purple: #a855f7;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font-family:'Segoe UI',system-ui,sans-serif; }

  .header {
    padding:20px 32px; border-bottom:1px solid var(--border);
    display:flex; align-items:center; justify-content:space-between;
  }
  .header h1 { font-size:20px; font-weight:600; }
  .header .live-dot { width:8px; height:8px; background:var(--green); border-radius:50%; display:inline-block; margin-right:8px; animation:pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .header .status { font-size:13px; color:var(--dim); }

  .stats-row {
    display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
    gap:12px; padding:20px 32px;
  }
  .stat-card {
    background:var(--card); border:1px solid var(--border); border-radius:10px;
    padding:16px; text-align:center;
  }
  .stat-card .value { font-size:28px; font-weight:700; }
  .stat-card .label { font-size:12px; color:var(--dim); margin-top:4px; text-transform:uppercase; letter-spacing:.5px; }
  .stat-card .value.green { color:var(--green); }
  .stat-card .value.red { color:var(--red); }
  .stat-card .value.yellow { color:var(--yellow); }
  .stat-card .value.blue { color:var(--blue); }

  .tabs {
    display:flex; gap:0; padding:0 32px; border-bottom:1px solid var(--border);
  }
  .tab {
    padding:12px 24px; cursor:pointer; font-size:14px; color:var(--dim);
    border-bottom:2px solid transparent; transition:.2s;
  }
  .tab:hover { color:var(--text); }
  .tab.active { color:var(--text); border-bottom-color:var(--blue); }

  .filters {
    padding:12px 32px; display:flex; gap:10px; align-items:center;
  }
  .filters select {
    background:var(--card); color:var(--text); border:1px solid var(--border);
    border-radius:6px; padding:6px 12px; font-size:13px; cursor:pointer;
  }
  .filters select:focus { outline:none; border-color:var(--blue); }

  .content { padding:0 32px 32px; }

  table { width:100%; border-collapse:collapse; font-size:13px; }
  thead th {
    text-align:left; padding:10px 12px; color:var(--dim); font-weight:500;
    border-bottom:1px solid var(--border); font-size:11px; text-transform:uppercase;
    letter-spacing:.5px; position:sticky; top:0; background:var(--bg);
  }
  tbody td { padding:10px 12px; border-bottom:1px solid var(--border); }
  tbody tr:hover { background:rgba(59,130,246,.05); }

  .badge {
    display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600;
  }
  .badge.won { background:rgba(34,197,94,.15); color:var(--green); }
  .badge.lost { background:rgba(239,68,68,.15); color:var(--red); }
  .badge.push { background:rgba(234,179,8,.15); color:var(--yellow); }
  .badge.pending { background:rgba(59,130,246,.1); color:var(--blue); }
  .badge.void { background:rgba(139,143,163,.15); color:var(--dim); }

  .manual-grade {
    margin-left:6px; font-size:11px; background:var(--card); color:var(--text);
    border:1px solid var(--border); border-radius:4px; padding:1px 4px;
  }

  .ev-positive { color:var(--green); font-weight:600; }

  .event-btn {
    display:inline-block; padding:4px 10px; border-radius:6px; font-size:11px; font-weight:600;
    color:var(--blue); border:1px solid var(--border); text-decoration:none; white-space:nowrap;
  }
  .event-btn:hover { background:rgba(59,130,246,.1); border-color:var(--blue); }

  .breakdown { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:16px; }
  .breakdown-card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; }
  .breakdown-card h3 { font-size:14px; margin-bottom:12px; color:var(--dim); font-weight:600; text-transform:uppercase; letter-spacing:.5px; }
  .bucket-table { width:100%; border-collapse:collapse; font-size:12px; }
  .bucket-table thead th { text-align:left; padding:6px 8px; color:var(--dim); font-weight:500; border-bottom:1px solid var(--border); font-size:10px; text-transform:uppercase; letter-spacing:.5px; }
  .bucket-table tbody td { padding:6px 8px; border-bottom:1px solid var(--border); }
  .bucket-table tbody tr:hover { background:rgba(59,130,246,.05); }
  .bucket-table .green { color:var(--green); font-weight:600; }
  .bucket-table .red { color:var(--red); font-weight:600; }
  .bucket-table tr.low-sample { opacity:.55; }
  .sample-badge { font-size:9px; text-transform:uppercase; letter-spacing:.5px; color:var(--dim); border:1px solid var(--border); border-radius:4px; padding:1px 5px; margin-left:6px; cursor:help; }

  .filter-toggle-btn {
    background:var(--card); color:var(--text); border:1px solid var(--border);
    border-radius:6px; padding:6px 14px; font-size:13px; cursor:pointer; font-weight:600;
  }
  .filter-toggle-btn:hover { border-color:var(--blue); }
  .filter-toggle-btn.active { border-color:var(--blue); color:var(--blue); }
  .filter-count {
    display:inline-block; background:var(--blue); color:#fff; border-radius:10px;
    font-size:10px; padding:1px 6px; margin-left:6px; font-weight:700;
  }
  .clear-filters-btn {
    background:none; color:var(--dim); border:1px solid var(--border); border-radius:6px;
    padding:6px 12px; font-size:12px; cursor:pointer;
  }
  .clear-filters-btn:hover { color:var(--text); border-color:var(--red); }

  .modal-overlay {
    position:fixed; inset:0; background:rgba(0,0,0,.6); display:flex;
    align-items:center; justify-content:center; z-index:100;
  }
  .modal-box {
    background:var(--card); border:1px solid var(--border); border-radius:12px;
    width:min(560px, 92vw); max-height:85vh; display:flex; flex-direction:column;
  }
  .modal-header {
    display:flex; justify-content:space-between; align-items:center;
    padding:16px 20px; border-bottom:1px solid var(--border);
  }
  .modal-header h3 { font-size:15px; font-weight:600; }
  .modal-close {
    background:none; border:none; color:var(--dim); font-size:20px; cursor:pointer; line-height:1;
  }
  .modal-close:hover { color:var(--text); }
  .modal-body { padding:16px 20px; overflow-y:auto; flex:1; }
  .filter-group { margin-bottom:18px; }
  .filter-group h4 {
    font-size:11px; text-transform:uppercase; letter-spacing:.5px; color:var(--dim);
    margin-bottom:8px; font-weight:600;
  }
  .checkbox-list { display:flex; flex-wrap:wrap; gap:8px; }
  .checkbox-list label {
    display:flex; align-items:center; gap:5px; font-size:12px; background:var(--bg);
    border:1px solid var(--border); border-radius:6px; padding:5px 9px; cursor:pointer;
  }
  .checkbox-list label:hover { border-color:var(--blue); }
  .checkbox-list input { cursor:pointer; }
  .modal-footer {
    display:flex; justify-content:flex-end; gap:10px; padding:14px 20px;
    border-top:1px solid var(--border);
  }
  .filter-apply-btn {
    background:var(--blue); color:#fff; border:none; border-radius:6px;
    padding:7px 16px; font-size:13px; font-weight:600; cursor:pointer;
  }
  .filter-apply-btn:hover { opacity:.9; }

  .track-btn {
    background:none; border:1px solid var(--border); border-radius:6px; cursor:pointer;
    padding:4px 8px; font-size:14px; line-height:1; color:var(--dim); margin-right:6px;
  }
  .track-btn:hover { border-color:var(--yellow); }
  .track-btn.tracked { color:var(--yellow); border-color:var(--yellow); background:rgba(234,179,8,.1); }

  .tracker-layout { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:16px; }
  .tracker-card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; }
  .tracker-card h3 { font-size:14px; margin-bottom:12px; color:var(--dim); font-weight:600; text-transform:uppercase; letter-spacing:.5px; }

  .cal-nav { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; }
  .cal-nav button { background:none; border:1px solid var(--border); border-radius:6px; color:var(--text); cursor:pointer; padding:4px 10px; }
  .cal-nav button:hover { border-color:var(--blue); }
  .cal-month-label { font-size:13px; font-weight:600; }
  .cal-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:4px; }
  .cal-dow { font-size:10px; color:var(--dim); text-align:center; padding-bottom:4px; text-transform:uppercase; }
  .cal-day {
    aspect-ratio:1; border-radius:6px; font-size:11px; display:flex; align-items:center;
    justify-content:center; border:1px solid var(--border); position:relative; cursor:default;
  }
  .cal-day.empty { border:none; }
  .cal-day .num { position:absolute; top:3px; left:5px; font-size:9px; color:var(--dim); }
  .cal-day[title]:hover { border-color:var(--text); }

  .empty-tracker { text-align:center; padding:40px; color:var(--dim); font-size:13px; }

  .empty { text-align:center; padding:60px; color:var(--dim); }

  @media (max-width:768px) {
    .stats-row { grid-template-columns:repeat(3,1fr); }
    .breakdown { grid-template-columns:1fr; }
    .header, .filters, .content { padding-left:16px; padding-right:16px; }
    .tabs { padding:0 16px; }
  }
</style>
</head>
<body>

<div class="header">
  <div><h1><span class="live-dot"></span>Value Bet Dashboard</h1></div>
  <div class="status" id="status">Loading...</div>
</div>

<div class="stats-row" id="stats-row"></div>

<div class="tabs">
  <div class="tab active" data-tab="live">Live Bets</div>
  <div class="tab" data-tab="results">Results</div>
  <div class="tab" data-tab="breakdown">Breakdown</div>
  <div class="tab" data-tab="tracker">Tracker</div>
</div>

<div class="filters">
  <button class="filter-toggle-btn" id="filter-toggle-btn">Filters<span class="filter-count" id="filter-count" style="display:none"></span></button>
  <button class="clear-filters-btn" id="clear-filters-btn" style="display:none">Clear filters</button>
</div>

<div class="modal-overlay" id="filter-modal-overlay" style="display:none">
  <div class="modal-box">
    <div class="modal-header">
      <h3>Filter Preferences</h3>
      <button class="modal-close" id="filter-modal-close">&times;</button>
    </div>
    <div class="modal-body">
      <div class="filter-group"><h4>Sports</h4><div class="checkbox-list" id="cb-sport"></div></div>
      <div class="filter-group"><h4>Bookmakers</h4><div class="checkbox-list" id="cb-bookmaker"></div></div>
      <div class="filter-group"><h4>Markets</h4><div class="checkbox-list" id="cb-market"></div></div>
      <div class="filter-group"><h4>Value (EV) buckets</h4><div class="checkbox-list" id="cb-ev_bucket"></div></div>
      <div class="filter-group"><h4>Odds ranges</h4><div class="checkbox-list" id="cb-odds_bucket"></div></div>
    </div>
    <div class="modal-footer">
      <button class="clear-filters-btn" id="modal-clear-btn">Clear all</button>
      <button class="filter-apply-btn" id="modal-apply-btn">Apply</button>
    </div>
  </div>
</div>

<div class="content">
  <div id="tab-live"></div>
  <div id="tab-results" style="display:none"></div>
  <div id="tab-breakdown" style="display:none"></div>
  <div id="tab-tracker" style="display:none"></div>
</div>

<script>
let currentTab = 'live';
let stats = {};

document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    currentTab = t.dataset.tab;
    document.querySelectorAll('[id^="tab-"]').forEach(x => x.style.display = 'none');
    document.getElementById('tab-' + currentTab).style.display = '';
    const showFilters = currentTab === 'live' || currentTab === 'results';
    document.querySelector('.filters').style.display = showFilters ? '' : 'none';
    refresh();
  });
});

// Filter preferences persist across reloads via localStorage -- the dashboard keeps
// logging every bet regardless; this only controls what's *shown*.
const EV_BUCKET_LABELS = ['0-0.5%', '0.5-1%', '1-2%', '2-5%', '5-10%', '10-20%', '20%+'];
const ODDS_BUCKET_LABELS = ['1.01-1.30', '1.30-1.60', '1.60-2.00', '2.00-2.50', '2.50-3.50', '3.50+'];
const FILTER_GROUPS = ['sport', 'bookmaker', 'market', 'ev_bucket', 'odds_bucket'];

function loadFilterPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem('dashboardFilters') || '{}');
    const prefs = {};
    FILTER_GROUPS.forEach(g => { prefs[g] = Array.isArray(saved[g]) ? saved[g] : []; });
    return prefs;
  } catch(e) {
    const prefs = {};
    FILTER_GROUPS.forEach(g => prefs[g] = []);
    return prefs;
  }
}

let filterPrefs = loadFilterPrefs();

function saveFilterPrefs() {
  localStorage.setItem('dashboardFilters', JSON.stringify(filterPrefs));
}

function updateFilterUi() {
  const count = FILTER_GROUPS.reduce((n, g) => n + filterPrefs[g].length, 0);
  document.getElementById('filter-count').style.display = count ? '' : 'none';
  document.getElementById('filter-count').textContent = count;
  document.getElementById('filter-toggle-btn').classList.toggle('active', count > 0);
  document.getElementById('clear-filters-btn').style.display = count ? '' : 'none';
}

function populateCheckboxList(id, group, options) {
  const el = document.getElementById(id);
  const checked = new Set(filterPrefs[group]);
  el.innerHTML = options.map(o =>
    '<label><input type="checkbox" value="' + o + '"' + (checked.has(o) ? ' checked' : '') + '> ' + o + '</label>'
  ).join('');
}

function populateFilterModal(s) {
  const sports = (s.by_sport || []).map(r => r.sport).filter(Boolean).sort();
  const bookmakers = (s.by_bookmaker || []).map(r => r.bookmaker).filter(Boolean).sort();
  const markets = (s.by_market || []).map(r => r.market).filter(Boolean).sort();
  populateCheckboxList('cb-sport', 'sport', sports);
  populateCheckboxList('cb-bookmaker', 'bookmaker', bookmakers);
  populateCheckboxList('cb-market', 'market', markets);
  populateCheckboxList('cb-ev_bucket', 'ev_bucket', EV_BUCKET_LABELS);
  populateCheckboxList('cb-odds_bucket', 'odds_bucket', ODDS_BUCKET_LABELS);
}

document.getElementById('filter-toggle-btn').addEventListener('click', () => {
  document.getElementById('filter-modal-overlay').style.display = 'flex';
});
document.getElementById('filter-modal-close').addEventListener('click', () => {
  document.getElementById('filter-modal-overlay').style.display = 'none';
});
document.getElementById('filter-modal-overlay').addEventListener('click', (e) => {
  if (e.target.id === 'filter-modal-overlay') document.getElementById('filter-modal-overlay').style.display = 'none';
});

document.getElementById('modal-apply-btn').addEventListener('click', () => {
  FILTER_GROUPS.forEach(g => {
    filterPrefs[g] = [...document.querySelectorAll('#cb-' + g + ' input:checked')].map(i => i.value);
  });
  saveFilterPrefs();
  updateFilterUi();
  document.getElementById('filter-modal-overlay').style.display = 'none';
  refresh();
});

document.getElementById('modal-clear-btn').addEventListener('click', () => {
  FILTER_GROUPS.forEach(g => filterPrefs[g] = []);
  document.querySelectorAll('.checkbox-list input').forEach(i => i.checked = false);
  saveFilterPrefs();
  updateFilterUi();
});

document.getElementById('clear-filters-btn').addEventListener('click', () => {
  FILTER_GROUPS.forEach(g => filterPrefs[g] = []);
  saveFilterPrefs();
  updateFilterUi();
  refresh();
});

function qs() {
  const p = new URLSearchParams();
  FILTER_GROUPS.forEach(g => filterPrefs[g].forEach(v => p.append(g, v)));
  return p.toString() ? '?' + p.toString() : '';
}

function fmtEv(v) { return '+' + (v - 100).toFixed(1) + '%'; }
function fmtOdds(v) { return v ? parseFloat(v).toFixed(2) : '-'; }
function fmtDate(d) {
  if (!d) return '-';
  const dt = new Date(d);
  return dt.toLocaleDateString('en-US', {month:'short',day:'numeric'}) + ' ' +
         dt.toLocaleTimeString('en-US', {hour:'2-digit',minute:'2-digit',hour12:false});
}

function renderStats(s) {
  stats = s;
  const cards = [
    { value: s.total, label: 'Total Bets', cls: '' },
    { value: s.pending, label: 'Pending', cls: 'blue' },
    { value: s.won, label: 'Won', cls: 'green' },
    { value: s.lost, label: 'Lost', cls: 'red' },
    { value: s.settled ? s.win_rate + '%' : '-', label: 'Win Rate', cls: s.win_rate >= 50 ? 'green' : 'red' },
    { value: s.settled ? (s.roi >= 0 ? '+' : '') + s.roi + '%' : '-', label: 'ROI (gross)', cls: s.roi >= 0 ? 'green' : 'red' },
    { value: s.settled ? (s.profit_units >= 0 ? '+' : '') + s.profit_units.toFixed(1) + 'u' : '-', label: 'Profit (gross)', cls: s.profit_units >= 0 ? 'green' : 'red' },
    { value: s.settled ? (s.net_roi >= 0 ? '+' : '') + s.net_roi + '%' : '-', label: 'ROI (after fees)', cls: s.net_roi >= 0 ? 'green' : 'red' },
    { value: s.settled ? (s.net_profit_units >= 0 ? '+' : '') + s.net_profit_units.toFixed(1) + 'u' : '-', label: 'Profit (after fees)', cls: s.net_profit_units >= 0 ? 'green' : 'red' },
  ];
  document.getElementById('stats-row').innerHTML = cards.map(c =>
    '<div class="stat-card"><div class="value ' + c.cls + '">' + c.value + '</div><div class="label">' + c.label + '</div></div>'
  ).join('');
}

const BOOKMAKER_DOMAINS = {
  'Polymarket': 'polymarket.com',
  'Kalshi': 'kalshi.com',
  'Stake': 'stake.com',
};

function slugify(s) {
  return (s || '').toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

// Stake's API-provided href is missing its category path (e.g. gives
// stake.com/sports/{id}-{slug}/all instead of the full
// stake.com/sports/{sport}/{country}/{league}/{id}-{slug}), which makes it
// slow/unreliable to land on directly. Reconstruct the full path best-effort from our
// stored sport+league fields. Confirmed against two real examples (Tennis, Football);
// the federation-prefix stripping below is extrapolated from the one confirmed case
// ("FIFA World Cup" -> "world-cup") to other federations we haven't verified. Esports
// is excluded -- our "league" field there is "{game} - {competition}", not a
// country/league pair, so there's no safe pattern to apply. Falls back to the raw
// href, then to a search link, if this can't produce a confident guess.
const STAKE_SPORT_MAP = { 'football': 'soccer' }; // our "Football" (soccer) -> Stake's "soccer"
const STAKE_FEDERATION_PREFIXES = ['fifa', 'uefa', 'concacaf', 'conmebol', 'caf', 'ofc', 'afc', 'icc', 'fiba', 'fivb', 'ihf', 'avc', 'cev'];

function stripFederationPrefix(words) {
  return words[0] && STAKE_FEDERATION_PREFIXES.includes(words[0].toLowerCase()) ? words.slice(1) : words;
}

function stakeUrl(b) {
  if (!b.event_url) return null;
  if (b.sport === 'Esports') return null;
  const m = b.event_url.match(/\\/sports\\/([^\\/]+?)(?:\\/all)?$/);
  if (!m) return null;
  const idSlug = m[1];
  const rawSport = (b.sport || '').toLowerCase();
  const sport = STAKE_SPORT_MAP[rawSport] || rawSport;
  const league = b.league || '';

  if (rawSport === 'tennis') {
    const tourMatch = league.match(/^(WTA|ATP)\\s*-\\s*([^,]+)/i);
    if (tourMatch) {
      const tour = tourMatch[1].toLowerCase();
      const tournament = slugify(tourMatch[2]);
      const genderSuffix = tour === 'wta' ? 'women-singles' : 'men-singles';
      return `https://stake.com/sports/tennis/${tour}/${tournament}-${genderSuffix}/${idSlug}`;
    }
    return null;
  }

  // Team sports: league is "{Country/Region} - {League name}[, qualifiers...]".
  // Drop any ", qualifier" suffix (group/stage/gender) and a leading federation
  // acronym, since Stake's category page is named after the competition itself.
  const dashIdx = league.indexOf(' - ');
  if (dashIdx === -1 || !sport) return null;
  const country = slugify(league.slice(0, dashIdx));
  const leagueNameRaw = league.slice(dashIdx + 3).split(',')[0].trim();
  const leagueWords = stripFederationPrefix(leagueNameRaw.split(/\\s+/));
  const leagueSlug = slugify(leagueWords.join(' '));
  if (!country || !leagueSlug) return null;
  return `https://stake.com/sports/${sport}/${country}/${leagueSlug}/${idSlug}`;
}

function eventUrl(b) {
  if (b.bookmaker === 'Stake') {
    const guess = stakeUrl(b);
    if (guess) return guess;
  }
  if (b.event_url) return b.event_url;
  const domain = BOOKMAKER_DOMAINS[b.bookmaker];
  const query = (b.home||'') + ' ' + (b.away||'');
  if (domain) {
    return 'https://www.google.com/search?q=' + encodeURIComponent('site:' + domain + ' ' + query);
  }
  return 'https://www.google.com/search?q=' + encodeURIComponent((b.bookmaker||'') + ' ' + query);
}

const TOTALS_MARKETS = new Set(['Totals', 'Totals (Games)', 'Total Maps', 'Totals HT', 'Bookings Totals', 'Corners Totals', 'Team Total Home', 'Team Total Away']);

function sideLabel(b) {
  if (TOTALS_MARKETS.has(b.market)) {
    if (b.bet_side === 'home') return 'Over';
    if (b.bet_side === 'away') return 'Under';
  }
  if (b.bet_side === 'home') return b.home || 'Home';
  if (b.bet_side === 'away') return b.away || 'Away';
  if (b.bet_side === 'draw') return 'Draw';
  return b.bet_side || '-';
}

function fmtLiquidity(b) {
  if (b.liquidity == null) return '-';
  const cents = b.liquidity_price != null ? Math.round(b.liquidity_price * 100) : null;
  return '$' + Math.round(b.liquidity).toLocaleString() + (cents != null ? ' @ ' + cents + '¢' : '');
}

async function toggleTrack(id, currentlyTracked) {
  await fetch('/api/track', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: id, tracked: !currentlyTracked}),
  });
  refresh();
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('.track-btn');
  if (btn) toggleTrack(btn.dataset.id, btn.dataset.tracked === '1');
});

function trackBtn(b) {
  const tracked = !!b.tracked;
  return '<button class="track-btn' + (tracked ? ' tracked' : '') + '" title="' +
    (tracked ? 'Tracked -- click to untrack' : 'Track this bet') + '" data-id="' + b.id + '" data-tracked="' + (tracked ? '1' : '0') + '">' +
    (tracked ? '★' : '☆') + '</button>';
}

async function manualGrade(id, status) {
  if (!status) return;
  await fetch('/api/grade', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: id, status: status}),
  });
  refresh();
}

document.addEventListener('change', (e) => {
  const sel = e.target.closest('.manual-grade');
  if (sel) manualGrade(sel.dataset.id, sel.value);
});

function manualGradeControl(b) {
  if (b.status !== 'void') return '';
  return ' <select class="manual-grade" title="Grade manually" data-id="' + b.id + '">' +
    '<option value="">Grade…</option>' +
    '<option value="won">Won</option>' +
    '<option value="lost">Lost</option>' +
    '<option value="push">Push</option>' +
    '</select>';
}

function fmtClv(b) {
  if (b.clv_true == null) return '-';
  const cls = b.clv_true >= 0 ? 'green' : 'red';
  const title = 'vs Pinnacle closing ' + b.closing_odds.toFixed(3) + ' (de-vigged CLV; raw CLV ' + (b.clv_raw>=0?'+':'') + b.clv_raw.toFixed(1) + '%)';
  return '<span class="' + cls + '" title="' + title + '">' + (b.clv_true>=0?'+':'') + b.clv_true.toFixed(1) + '%</span>';
}

function renderTable(bets, showResult) {
  if (!bets.length) return '<div class="empty">No bets found</div>';
  let h = '<table><thead><tr>' +
    '<th></th><th>Match</th><th>Sport</th><th>Bookmaker</th><th>Side</th><th>Market</th><th>Odds</th><th>EV</th>' +
    (showResult ? '<th>Score</th><th>Result</th><th>CLV</th>' : '<th>Liquidity</th><th>Kick-off</th><th>Detected</th>') +
    '<th></th>' +
    '</tr></thead><tbody>';
  for (const b of bets) {
    h += '<tr>';
    h += '<td>' + trackBtn(b) + '</td>';
    h += '<td><strong>' + (b.home||'?') + '</strong> vs <strong>' + (b.away||'?') + '</strong></td>';
    h += '<td>' + (b.sport||'-') + '</td>';
    h += '<td>' + (b.bookmaker||'-') + '</td>';
    h += '<td>' + sideLabel(b) + '</td>';
    h += '<td>' + (b.market||'-') + (b.hdp ? ' ('+b.hdp+')' : '') + '</td>';
    h += '<td>' + fmtOdds(b.odds) + '</td>';
    h += '<td class="ev-positive">' + fmtEv(b.expected_value) + '</td>';
    if (showResult) {
      h += '<td>' + (b.home_score != null ? b.home_score + '-' + b.away_score : '-') + '</td>';
      h += '<td><span class="badge ' + b.status + '">' + b.status.toUpperCase() + '</span>' + manualGradeControl(b) + '</td>';
      h += '<td>' + fmtClv(b) + '</td>';
    } else {
      h += '<td title="Size available at the current best price for your side, live from the bookmaker">' + fmtLiquidity(b) + '</td>';
      h += '<td>' + fmtDate(b.match_date) + '</td>';
      h += '<td>' + fmtDate(b.detected_at) + '</td>';
    }
    h += '<td><a class="event-btn" href="' + eventUrl(b) + '" target="_blank" rel="noopener">Go to Event</a></td>';
    h += '</tr>';
  }
  h += '</tbody></table>';
  return h;
}

const MIN_SAMPLE_SIZE = 30;

function bucketTable(title, rows, nameKey) {
  let h = '<div class="breakdown-card"><h3>' + title + '</h3>';
  h += '<table class="bucket-table"><thead><tr><th>' + (nameKey === 'label' ? 'Range' : nameKey.charAt(0).toUpperCase()+nameKey.slice(1)) + '</th><th>Total</th><th>W</th><th>L</th><th>Pending</th><th>Win%</th><th>ROI</th><th>Profit</th><th>Net ROI</th><th>Net Profit</th><th title="Avg CLV vs Pinnacle closing line (de-vigged), where matchable">CLV</th></tr></thead><tbody>';
  for (const r of rows) {
    if (r.total === 0) continue;
    const st = r.w + r.l;
    const lowSample = st > 0 && st < MIN_SAMPLE_SIZE;
    const wr = st ? (r.w/st*100).toFixed(1)+'%' : '-';
    const profit = st ? ((r.profit_w || 0) - r.l) : null;
    const roi = st ? (profit / st * 100).toFixed(1) : '-';
    const netProfit = st ? (r.net_profit || 0) : null;
    const netRoi = st ? (netProfit / st * 100).toFixed(1) : '-';
    const roiCls = roi !== '-' ? (parseFloat(roi) >= 0 ? 'green' : 'red') : '';
    const netRoiCls = netRoi !== '-' ? (parseFloat(netRoi) >= 0 ? 'green' : 'red') : '';
    const wrCls = wr !== '-' ? (parseFloat(wr) >= 50 ? 'green' : 'red') : '';
    const profitCls = profit !== null ? (profit >= 0 ? 'green' : 'red') : '';
    const netProfitCls = netProfit !== null ? (netProfit >= 0 ? 'green' : 'red') : '';
    const clvCls = r.avg_clv_true != null ? (r.avg_clv_true >= 0 ? 'green' : 'red') : '';
    const clvText = r.avg_clv_true != null
      ? (r.avg_clv_true>=0?'+':'') + r.avg_clv_true.toFixed(1) + '% (n=' + r.clv_n + ')'
      : '-';
    h += '<tr' + (lowSample ? ' class="low-sample"' : '') + '>';
    h += '<td><strong>' + r[nameKey] + '</strong>' + (lowSample ? ' <span class="sample-badge" title="Fewer than ' + MIN_SAMPLE_SIZE + ' settled bets — not statistically meaningful yet">low sample</span>' : '') + '</td>';
    h += '<td>' + r.total + '</td>';
    h += '<td>' + r.w + '</td>';
    h += '<td>' + r.l + '</td>';
    h += '<td>' + r.p + '</td>';
    h += '<td class="' + (lowSample ? '' : wrCls) + '">' + wr + '</td>';
    h += '<td class="' + (lowSample ? '' : roiCls) + '">' + (roi !== '-' ? (parseFloat(roi)>=0?'+':'') + roi + '%' : '-') + '</td>';
    h += '<td class="' + (lowSample ? '' : profitCls) + '">' + (profit !== null ? (profit>=0?'+':'') + profit.toFixed(1) + 'u' : '-') + '</td>';
    h += '<td class="' + (lowSample ? '' : netRoiCls) + '">' + (netRoi !== '-' ? (parseFloat(netRoi)>=0?'+':'') + netRoi + '%' : '-') + '</td>';
    h += '<td class="' + (lowSample ? '' : netProfitCls) + '">' + (netProfit !== null ? (netProfit>=0?'+':'') + netProfit.toFixed(1) + 'u' : '-') + '</td>';
    h += '<td class="' + clvCls + '">' + clvText + '</td>';
    h += '</tr>';
  }
  h += '</tbody></table></div>';
  return h;
}


function renderBreakdown(s) {
  let h = '<div class="breakdown">';
  h += bucketTable('EV Buckets', s.ev_buckets, 'label');
  h += bucketTable('Odds Buckets', s.odds_buckets, 'label');
  h += bucketTable('Time Before Kick-off', s.time_buckets, 'label');
  h += bucketTable('By Sport', s.by_sport, 'sport');
  h += bucketTable('By Bookmaker', s.by_bookmaker, 'bookmaker');
  h += bucketTable('By Market', s.by_market, 'market');
  h += '</div>';
  return h;
}

async function refresh() {
  try {
    const tasks = [fetch('/api/stats'), fetch('/api/live' + qs()), fetch('/api/results' + qs())];
    if (currentTab === 'tracker') tasks.push(refreshTracker());
    const [sRes, lRes, rRes] = await Promise.all(tasks);
    const s = await sRes.json();
    const live = await lRes.json();
    const results = await rRes.json();

    renderStats(s);
    document.getElementById('tab-live').innerHTML = renderTable(live, false);
    document.getElementById('tab-results').innerHTML = renderTable(results, true);
    document.getElementById('tab-breakdown').innerHTML = renderBreakdown(s);

    // Populated from the (unfiltered) stats breakdowns, not the filtered live/results
    // lists -- otherwise checking a filter would shrink the list of options to pick from.
    populateFilterModal(s);
    updateFilterUi();

    document.getElementById('status').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('status').textContent = 'Error: ' + e.message;
  }
}

function renderTrackerStats(t) {
  const n = t.won + t.lost;
  const cards = [
    { value: t.total, label: 'Tracked Bets', cls: '' },
    { value: t.pending, label: 'Pending', cls: 'blue' },
    { value: t.won, label: 'Won', cls: 'green' },
    { value: t.lost, label: 'Lost', cls: 'red' },
    { value: n ? t.win_rate + '%' : '-', label: 'Win Rate', cls: t.win_rate >= 50 ? 'green' : 'red' },
    { value: n ? (t.roi >= 0 ? '+' : '') + t.roi + '%' : '-', label: 'ROI', cls: t.roi >= 0 ? 'green' : 'red' },
    { value: n ? (t.profit_units >= 0 ? '+' : '') + t.profit_units.toFixed(1) + 'u' : '-', label: 'Profit', cls: t.profit_units >= 0 ? 'green' : 'red' },
  ];
  return cards.map(c =>
    '<div class="stat-card"><div class="value ' + c.cls + '">' + c.value + '</div><div class="label">' + c.label + '</div></div>'
  ).join('');
}

let calOffset = 0; // months back from current month

function renderCalendar(daily) {
  const now = new Date();
  const view = new Date(now.getFullYear(), now.getMonth() + calOffset, 1);
  const year = view.getFullYear(), month = view.getMonth();
  const monthLabel = view.toLocaleDateString('en-US', {month:'long', year:'numeric'});
  const firstDow = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month+1, 0).getDate();

  let h = '<div class="cal-nav"><button id="cal-prev">‹</button><div class="cal-month-label">' + monthLabel + '</div><button id="cal-next">›</button></div>';
  h += '<div class="cal-grid">';
  ['S','M','T','W','T','F','S'].forEach(d => h += '<div class="cal-dow">' + d + '</div>');
  for (let i=0; i<firstDow; i++) h += '<div class="cal-day empty"></div>';
  for (let d=1; d<=daysInMonth; d++) {
    const dateStr = year + '-' + String(month+1).padStart(2,'0') + '-' + String(d).padStart(2,'0');
    const profit = daily[dateStr];
    let bg = 'transparent', color = 'var(--dim)', title = dateStr + ': no settled bets';
    if (profit !== undefined) {
      const intensity = Math.min(Math.abs(profit) / 5, 1);
      bg = profit >= 0 ? 'rgba(34,197,94,' + (0.15 + intensity*0.5) + ')' : 'rgba(239,68,68,' + (0.15 + intensity*0.5) + ')';
      color = profit >= 0 ? 'var(--green)' : 'var(--red)';
      title = dateStr + ': ' + (profit>=0?'+':'') + profit.toFixed(1) + 'u';
    }
    h += '<div class="cal-day" style="background:' + bg + ';color:' + color + '" title="' + title + '"><span class="num">' + d + '</span>' +
      (profit !== undefined ? (profit>=0?'+':'') + profit.toFixed(1) : '') + '</div>';
  }
  h += '</div>';
  return h;
}

function renderChart(cumulative) {
  if (!cumulative.length) return '<div class="empty-tracker">No settled tracked bets yet</div>';
  const w = 480, ht = 180, pad = 24;
  const profits = cumulative.map(c => c.profit);
  const min = Math.min(0, ...profits), max = Math.max(0, ...profits);
  const range = (max - min) || 1;
  const x = i => pad + (i / Math.max(cumulative.length - 1, 1)) * (w - pad*2);
  const y = v => ht - pad - ((v - min) / range) * (ht - pad*2);
  const points = cumulative.map((c,i) => x(i) + ',' + y(c.profit)).join(' ');
  const zeroY = y(0);
  const last = profits[profits.length-1];
  const lineColor = last >= 0 ? 'var(--green)' : 'var(--red)';
  return '<svg viewBox="0 0 ' + w + ' ' + ht + '" style="width:100%;height:200px">' +
    '<line x1="' + pad + '" y1="' + zeroY + '" x2="' + (w-pad) + '" y2="' + zeroY + '" stroke="var(--border)" stroke-dasharray="3,3"/>' +
    '<polyline points="' + points + '" fill="none" stroke="' + lineColor + '" stroke-width="2"/>' +
    '<circle cx="' + x(cumulative.length-1) + '" cy="' + y(last) + '" r="3" fill="' + lineColor + '"/>' +
    '</svg>';
}

function renderTrackerList(bets) {
  if (!bets.length) return '<div class="empty-tracker">No tracked bets yet -- click the ☆ on any bet in Live Bets or Results to start tracking it.</div>';
  let h = '<table><thead><tr><th></th><th>Match</th><th>Sport</th><th>Bookmaker</th><th>Side</th><th>Market</th><th>Odds</th><th>Score</th><th>Status</th></tr></thead><tbody>';
  for (const b of bets) {
    h += '<tr>';
    h += '<td>' + trackBtn(b) + '</td>';
    h += '<td><strong>' + (b.home||'?') + '</strong> vs <strong>' + (b.away||'?') + '</strong></td>';
    h += '<td>' + (b.sport||'-') + '</td>';
    h += '<td>' + (b.bookmaker||'-') + '</td>';
    h += '<td>' + sideLabel(b) + '</td>';
    h += '<td>' + (b.market||'-') + (b.hdp ? ' ('+b.hdp+')' : '') + '</td>';
    h += '<td>' + fmtOdds(b.odds) + '</td>';
    h += '<td>' + (b.home_score != null ? b.home_score + '-' + b.away_score : '-') + '</td>';
    h += '<td><span class="badge ' + b.status + '">' + b.status.toUpperCase() + '</span>' + manualGradeControl(b) + '</td>';
    h += '</tr>';
  }
  h += '</tbody></table>';
  return h;
}

async function refreshTracker() {
  const res = await fetch('/api/tracker');
  const t = await res.json();
  let h = '<div class="stats-row" style="padding:0 0 16px">' + renderTrackerStats(t) + '</div>';
  h += '<div class="tracker-layout">';
  h += '<div class="tracker-card"><h3>Daily P&L</h3><div id="cal-container">' + renderCalendar(t.daily) + '</div></div>';
  h += '<div class="tracker-card"><h3>Cumulative Profit</h3>' + renderChart(t.cumulative) + '</div>';
  h += '</div>';
  h += '<div class="tracker-card" style="margin-top:16px"><h3>Tracked Bets</h3>' + renderTrackerList(t.bets) + '</div>';
  document.getElementById('tab-tracker').innerHTML = h;

  document.getElementById('cal-prev').addEventListener('click', () => { calOffset--; document.getElementById('cal-container').innerHTML = renderCalendar(t.daily); });
  document.getElementById('cal-next').addEventListener('click', () => { calOffset++; document.getElementById('cal-container').innerHTML = renderCalendar(t.daily); });
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(debug=True, port=5000)
