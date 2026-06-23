"""
Database abstraction — uses PostgreSQL when DATABASE_URL is set, SQLite otherwise.
"""

import os
import sqlite3

DATABASE_URL = os.environ.get("DATABASE_URL", "")

if DATABASE_URL:
    import psycopg2
    import psycopg2.extras


def _sqlite_connect():
    from pathlib import Path
    conn = sqlite3.connect(Path(__file__).parent / "bets.db")
    conn.row_factory = sqlite3.Row
    return conn


def _pg_connect():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def get_conn():
    if DATABASE_URL:
        return _pg_connect()
    return _sqlite_connect()


def execute(conn, query, params=()):
    if DATABASE_URL:
        query = _adapt_query(query)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        cur = conn.cursor()
    cur.execute(query, params)
    return cur


def fetchall(conn, query, params=()):
    cur = execute(conn, query, params)
    rows = cur.fetchall()
    cur.close()
    if DATABASE_URL:
        return [dict(r) for r in rows]
    return [dict(r) for r in rows]


def fetchone(conn, query, params=()):
    cur = execute(conn, query, params)
    row = cur.fetchone()
    cur.close()
    if row is None:
        return None
    return dict(row)


def commit(conn):
    conn.commit()


def close(conn):
    conn.close()


def _adapt_query(query):
    import re
    query = query.replace("?", "%s")
    query = query.replace("INSERT OR IGNORE", "INSERT")
    if "ON CONFLICT" not in query and "INSERT" in query:
        query = query.rstrip().rstrip(";")
        query += " ON CONFLICT DO NOTHING"
    query = re.sub(
        r"\(julianday\((\w+)\)\s*-\s*julianday\((\w+)\)\)\s*\*\s*24",
        r"EXTRACT(EPOCH FROM (\1::timestamp - \2::timestamp)) / 3600",
        query,
    )
    return query


CREATE_TABLE_SQLITE = """
    CREATE TABLE IF NOT EXISTS bets (
        id            TEXT PRIMARY KEY,
        event_id      INTEGER,
        bookmaker     TEXT,
        bet_side      TEXT,
        market        TEXT,
        hdp           REAL,
        odds          REAL,
        expected_value REAL,
        home          TEXT,
        away          TEXT,
        sport         TEXT,
        league        TEXT,
        match_date    TEXT,
        detected_at   TEXT,
        status        TEXT DEFAULT 'pending',
        home_score    INTEGER,
        away_score    INTEGER,
        graded_at     TEXT,
        event_url     TEXT
    )
"""

CREATE_TABLE_PG = """
    CREATE TABLE IF NOT EXISTS bets (
        id            TEXT PRIMARY KEY,
        event_id      BIGINT,
        bookmaker     TEXT,
        bet_side      TEXT,
        market        TEXT,
        hdp           DOUBLE PRECISION,
        odds          DOUBLE PRECISION,
        expected_value DOUBLE PRECISION,
        home          TEXT,
        away          TEXT,
        sport         TEXT,
        league        TEXT,
        match_date    TEXT,
        detected_at   TEXT,
        status        TEXT DEFAULT 'pending',
        home_score    INTEGER,
        away_score    INTEGER,
        graded_at     TEXT,
        event_url     TEXT
    )
"""


def init_db():
    conn = get_conn()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute(CREATE_TABLE_PG)
        cur.execute("ALTER TABLE bets ADD COLUMN IF NOT EXISTS event_url TEXT")
        cur.close()
    else:
        conn.execute(CREATE_TABLE_SQLITE)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(bets)").fetchall()}
        if "event_url" not in existing:
            conn.execute("ALTER TABLE bets ADD COLUMN event_url TEXT")
    conn.commit()
    return conn
