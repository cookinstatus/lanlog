"""SQLite schema and helpers for the LAN device logger."""

import os
import sqlite3
import time

DB_PATH = os.environ.get(
    "LANLOG_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lanlog.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    ip           TEXT PRIMARY KEY,
    mac          TEXT,
    vendor       TEXT,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS queries (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    client   TEXT NOT NULL,
    qname    TEXT NOT NULL,
    qtype    TEXT NOT NULL,
    outcome  TEXT NOT NULL,      -- query | cached | forwarded | reply | blocked
    detail   TEXT,
    source   TEXT NOT NULL DEFAULT 'pihole'
);

CREATE INDEX IF NOT EXISTS idx_queries_ts     ON queries(ts);
CREATE INDEX IF NOT EXISTS idx_queries_client ON queries(client);
CREATE INDEX IF NOT EXISTS idx_queries_name   ON queries(qname);

-- Which addresses a name resolved to. The query log's reply/cached lines carry
-- the answer but NOT the client that asked, so this is keyed on the name alone
-- and aggregated rather than stored as one row per reply: a cached name is
-- answered dozens of times a minute, and an event log of that would grow
-- without bound for no extra information. hits/last_seen carry the "how often,
-- how recently" that the raw lines would have.
CREATE TABLE IF NOT EXISTS answers (
    qname      TEXT NOT NULL,
    ip         TEXT NOT NULL,
    qtype      TEXT,             -- A / AAAA, inferred from the address family
    kind       TEXT,             -- reply | cached | cached-stale
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (qname, ip)
);

CREATE INDEX IF NOT EXISTS idx_answers_name ON answers(qname);
CREATE INDEX IF NOT EXISTS idx_answers_seen ON answers(last_seen);
"""


def connect(path=DB_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Two writers share this DB (ingest + discovery), and WAL still serialises
    # writes. Two fixes were needed:
    #   1. busy_timeout -- so a loser waits instead of raising "database is
    #      locked" immediately.
    #   2. isolation_level=None (autocommit) -- python's sqlite3 otherwise holds
    #      a write transaction open until commit(). Ingest only committed every
    #      25 queries, so on a quiet LAN it held the write lock for minutes and
    #      discovery always timed out. Autocommit means no lock is ever held
    #      across a sleep.
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn):
    """Add columns introduced after a database was first created.

    executescript(CREATE TABLE IF NOT EXISTS) silently leaves an old table
    alone, so a pre-existing DB keeps its original shape and any query naming a
    newer column fails. Adding the column here makes upgrades non-breaking.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(queries)")}
    if "source" not in cols:
        conn.execute(
            "ALTER TABLE queries ADD COLUMN source TEXT NOT NULL DEFAULT 'pihole'"
        )
    dev_cols = {r["name"] for r in conn.execute("PRAGMA table_info(devices)")}
    if "vendor" not in dev_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN vendor TEXT")
    # hostname was written by nothing, so every row held NULL and naming moved
    # to read time (lanlog/identify.py). Dropped rather than left in place so
    # the schema matches what actually stores data. sqlite >= 3.35 can drop a
    # column; on anything older the column simply stays, which is harmless.
    if "hostname" in dev_cols:
        try:
            conn.execute("ALTER TABLE devices DROP COLUMN hostname")
        except sqlite3.OperationalError:
            pass


def touch_device(conn, ip, mac=None, vendor=None, when=None):
    when = when if when is not None else time.time()
    conn.execute(
        """
        INSERT INTO devices (ip, mac, vendor, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(ip) DO UPDATE SET
            mac       = COALESCE(excluded.mac, devices.mac),
            vendor    = COALESCE(excluded.vendor, devices.vendor),
            last_seen = excluded.last_seen
        """,
        (ip, mac, vendor, when, when),
    )


def record_query(conn, ts, client, qname, qtype, outcome, detail=None,
                 source="pihole"):
    conn.execute(
        "INSERT INTO queries (ts, client, qname, qtype, outcome, detail, source)"
        " VALUES (?,?,?,?,?,?,?)",
        (ts, client, qname, qtype, outcome, detail, source),
    )
    touch_device(conn, client, when=ts)


def record_answer(conn, ts, qname, ip, qtype=None, kind=None, source="pihole"):
    """Record that `qname` resolved to `ip`, aggregating repeat answers.

    One row per (qname, ip): the first time it was seen, the last, and a hit
    count. Storing each reply line instead would be one row per cached answer
    per second, which is the same information at a thousand times the size.
    """
    conn.execute(
        """
        INSERT INTO answers (qname, ip, qtype, kind, first_seen, last_seen, hits)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(qname, ip) DO UPDATE SET
            last_seen = excluded.last_seen,
            kind      = COALESCE(excluded.kind, answers.kind),
            qtype     = COALESCE(excluded.qtype, answers.qtype),
            hits      = answers.hits + 1
        """,
        (qname, ip, qtype, kind, ts, ts),
    )
