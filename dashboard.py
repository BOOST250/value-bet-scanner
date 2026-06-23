"""
Web dashboard for the value bet scanner.
Run alongside value_bet_alerts.py — reads from the shared bets.db.

Usage: python dashboard.py
Then open http://localhost:5000
"""

from flask import Flask, jsonify, request

import db as database

database.init_db()

app = Flask(__name__)


@app.route("/")
def index():
    return INDEX_HTML


@app.route("/api/live")
def api_live():
    sport = request.args.get("sport", "")
    bookmaker = request.args.get("bookmaker", "")
    conn = database.get_conn()
    bets = database.fetchall(conn, "SELECT * FROM bets WHERE status='pending' ORDER BY detected_at DESC")
    database.close(conn)
    if sport:
        bets = [b for b in bets if b["sport"] == sport]
    if bookmaker:
        bets = [b for b in bets if b["bookmaker"] == bookmaker]
    return jsonify(bets)


@app.route("/api/results")
def api_results():
    sport = request.args.get("sport", "")
    bookmaker = request.args.get("bookmaker", "")
    conn = database.get_conn()
    bets = database.fetchall(conn, "SELECT * FROM bets WHERE status IN ('won','lost','push','void') ORDER BY graded_at DESC")
    database.close(conn)
    if sport:
        bets = [b for b in bets if b["sport"] == sport]
    if bookmaker:
        bets = [b for b in bets if b["bookmaker"] == bookmaker]
    return jsonify(bets)


@app.route("/api/stats")
def api_stats():
    conn = database.get_conn()
    total = database.fetchone(conn, "SELECT COUNT(*) total FROM bets")["total"]
    counts = {}
    for r in database.fetchall(conn, "SELECT status, COUNT(*) c FROM bets GROUP BY status"):
        counts[r["status"]] = r["c"]

    wins = counts.get("won", 0)
    losses = counts.get("lost", 0)
    pushes = counts.get("push", 0)
    settled = wins + losses + pushes

    profit = 0
    if wins:
        profit = sum(
            r["odds"] - 1 for r in database.fetchall(conn, "SELECT odds FROM bets WHERE status='won'")
        )
    roi = ((profit - losses) / settled * 100) if settled else 0

    BREAKDOWN_COLS = """COUNT(*) total,
               COALESCE(SUM(CASE WHEN status='won' THEN 1 END), 0) w,
               COALESCE(SUM(CASE WHEN status='lost' THEN 1 END), 0) l,
               COALESCE(SUM(CASE WHEN status='pending' THEN 1 END), 0) p,
               COALESCE(SUM(CASE WHEN status='won' THEN odds - 1 END), 0) profit_w,
               COALESCE(SUM(CASE WHEN status='lost' THEN 1 END), 0) loss_units"""

    by_bookmaker = database.fetchall(conn, f"SELECT bookmaker, {BREAKDOWN_COLS} FROM bets GROUP BY bookmaker")
    by_sport = database.fetchall(conn, f"SELECT sport, {BREAKDOWN_COLS} FROM bets GROUP BY sport ORDER BY total DESC")
    by_market = database.fetchall(conn, f"SELECT market, {BREAKDOWN_COLS} FROM bets GROUP BY market ORDER BY total DESC")

    ev_buckets = []
    for label, lo, hi in [("0-1%", 100, 101), ("1-2%", 101, 102), ("2-5%", 102, 105),
                           ("5-10%", 105, 110), ("10-20%", 110, 120), ("20%+", 120, 9999)]:
        r = database.fetchone(conn, f"""
            SELECT {BREAKDOWN_COLS}
            FROM bets WHERE expected_value >= ? AND expected_value < ?
        """, (lo, hi))
        ev_buckets.append({"label": label, **r})

    odds_buckets = []
    for label, lo, hi in [("1.01-1.30", 1.01, 1.30), ("1.30-1.60", 1.30, 1.60),
                           ("1.60-2.00", 1.60, 2.00), ("2.00-2.50", 2.00, 2.50),
                           ("2.50-3.50", 2.50, 3.50), ("3.50+", 3.50, 9999)]:
        r = database.fetchone(conn, f"""
            SELECT {BREAKDOWN_COLS}
            FROM bets WHERE odds >= ? AND odds < ?
        """, (lo, hi))
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
        """, (lo_h, hi_h))
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
</div>

<div class="filters">
  <select id="filter-sport"><option value="">All Sports</option></select>
  <select id="filter-bookmaker"><option value="">All Bookmakers</option></select>
</div>

<div class="content">
  <div id="tab-live"></div>
  <div id="tab-results" style="display:none"></div>
  <div id="tab-breakdown" style="display:none"></div>
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
    refresh();
  });
});

function qs() {
  const s = document.getElementById('filter-sport').value;
  const b = document.getElementById('filter-bookmaker').value;
  const p = new URLSearchParams();
  if (s) p.set('sport', s);
  if (b) p.set('bookmaker', b);
  return p.toString() ? '?' + p.toString() : '';
}

document.getElementById('filter-sport').addEventListener('change', refresh);
document.getElementById('filter-bookmaker').addEventListener('change', refresh);

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
    { value: s.settled ? (s.roi >= 0 ? '+' : '') + s.roi + '%' : '-', label: 'ROI', cls: s.roi >= 0 ? 'green' : 'red' },
    { value: s.settled ? (s.profit_units >= 0 ? '+' : '') + s.profit_units.toFixed(1) + 'u' : '-', label: 'Profit', cls: s.profit_units >= 0 ? 'green' : 'red' },
  ];
  document.getElementById('stats-row').innerHTML = cards.map(c =>
    '<div class="stat-card"><div class="value ' + c.cls + '">' + c.value + '</div><div class="label">' + c.label + '</div></div>'
  ).join('');
}

const BOOKMAKER_DOMAINS = {
  'Polymarket': 'polymarket.com',
  'Kalshi': 'kalshi.com',
};

function eventUrl(b) {
  const domain = BOOKMAKER_DOMAINS[b.bookmaker];
  const query = (b.home||'') + ' ' + (b.away||'');
  if (domain) {
    return 'https://www.google.com/search?q=' + encodeURIComponent('site:' + domain + ' ' + query);
  }
  return 'https://www.google.com/search?q=' + encodeURIComponent((b.bookmaker||'') + ' ' + query);
}

function renderTable(bets, showResult) {
  if (!bets.length) return '<div class="empty">No bets found</div>';
  let h = '<table><thead><tr>' +
    '<th>Match</th><th>Sport</th><th>Bookmaker</th><th>Side</th><th>Market</th><th>Odds</th><th>EV</th>' +
    (showResult ? '<th>Score</th><th>Result</th>' : '<th>Kick-off</th><th>Detected</th>') +
    '<th></th>' +
    '</tr></thead><tbody>';
  for (const b of bets) {
    h += '<tr>';
    h += '<td><strong>' + (b.home||'?') + '</strong> vs <strong>' + (b.away||'?') + '</strong></td>';
    h += '<td>' + (b.sport||'-') + '</td>';
    h += '<td>' + (b.bookmaker||'-') + '</td>';
    h += '<td>' + (b.bet_side||'-') + '</td>';
    h += '<td>' + (b.market||'-') + (b.hdp ? ' ('+b.hdp+')' : '') + '</td>';
    h += '<td>' + fmtOdds(b.odds) + '</td>';
    h += '<td class="ev-positive">' + fmtEv(b.expected_value) + '</td>';
    if (showResult) {
      h += '<td>' + (b.home_score != null ? b.home_score + '-' + b.away_score : '-') + '</td>';
      h += '<td><span class="badge ' + b.status + '">' + b.status.toUpperCase() + '</span></td>';
    } else {
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
  h += '<table class="bucket-table"><thead><tr><th>' + (nameKey === 'label' ? 'Range' : nameKey.charAt(0).toUpperCase()+nameKey.slice(1)) + '</th><th>Total</th><th>W</th><th>L</th><th>Pending</th><th>Win%</th><th>ROI</th><th>Profit</th></tr></thead><tbody>';
  for (const r of rows) {
    if (r.total === 0) continue;
    const st = r.w + r.l;
    const lowSample = st > 0 && st < MIN_SAMPLE_SIZE;
    const wr = st ? (r.w/st*100).toFixed(1)+'%' : '-';
    const profit = st ? ((r.profit_w || 0) - r.l) : null;
    const roi = st ? (profit / st * 100).toFixed(1) : '-';
    const roiCls = roi !== '-' ? (parseFloat(roi) >= 0 ? 'green' : 'red') : '';
    const wrCls = wr !== '-' ? (parseFloat(wr) >= 50 ? 'green' : 'red') : '';
    const profitCls = profit !== null ? (profit >= 0 ? 'green' : 'red') : '';
    h += '<tr' + (lowSample ? ' class="low-sample"' : '') + '>';
    h += '<td><strong>' + r[nameKey] + '</strong>' + (lowSample ? ' <span class="sample-badge" title="Fewer than ' + MIN_SAMPLE_SIZE + ' settled bets — not statistically meaningful yet">low sample</span>' : '') + '</td>';
    h += '<td>' + r.total + '</td>';
    h += '<td>' + r.w + '</td>';
    h += '<td>' + r.l + '</td>';
    h += '<td>' + r.p + '</td>';
    h += '<td class="' + (lowSample ? '' : wrCls) + '">' + wr + '</td>';
    h += '<td class="' + (lowSample ? '' : roiCls) + '">' + (roi !== '-' ? (parseFloat(roi)>=0?'+':'') + roi + '%' : '-') + '</td>';
    h += '<td class="' + (lowSample ? '' : profitCls) + '">' + (profit !== null ? (profit>=0?'+':'') + profit.toFixed(1) + 'u' : '-') + '</td>';
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
    const [sRes, lRes, rRes] = await Promise.all([
      fetch('/api/stats'),
      fetch('/api/live' + qs()),
      fetch('/api/results' + qs()),
    ]);
    const s = await sRes.json();
    const live = await lRes.json();
    const results = await rRes.json();

    renderStats(s);
    document.getElementById('tab-live').innerHTML = renderTable(live, false);
    document.getElementById('tab-results').innerHTML = renderTable(results, true);
    document.getElementById('tab-breakdown').innerHTML = renderBreakdown(s);

    // populate filter dropdowns
    const sports = new Set(), bookmakers = new Set();
    [...live, ...results].forEach(b => { if(b.sport) sports.add(b.sport); if(b.bookmaker) bookmakers.add(b.bookmaker); });
    updateSelect('filter-sport', [...sports].sort());
    updateSelect('filter-bookmaker', [...bookmakers].sort());

    document.getElementById('status').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('status').textContent = 'Error: ' + e.message;
  }
}

function updateSelect(id, options) {
  const sel = document.getElementById(id);
  const cur = sel.value;
  const label = id === 'filter-sport' ? 'All Sports' : 'All Bookmakers';
  const html = '<option value="">' + label + '</option>' + options.map(o => '<option value="'+o+'"' + (o===cur?' selected':'') + '>'+o+'</option>').join('');
  if (sel.innerHTML !== html) sel.innerHTML = html;
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(debug=True, port=5000)
