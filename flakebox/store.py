"""SQLite store: runs, per-test results, quarantine set."""

from __future__ import annotations

import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    commit_sha TEXT,
    branch TEXT,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS results (
    run_id INTEGER,
    test_id TEXT,
    outcome TEXT,
    duration_ms INTEGER,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_results_test ON results(test_id);
CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
CREATE TABLE IF NOT EXISTS quarantine (
    test_id TEXT PRIMARY KEY,
    quarantined_at INTEGER,
    reason TEXT
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    # concurrent writers on stock SQLite: wait instead of immediately
    # raising "database is locked", and use WAL so readers don't block
    # writers (no-op on :memory:, which has no journal file).
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def record_run(conn: sqlite3.Connection, results: list[dict],
               commit_sha: str | None = None,
               branch: str | None = None) -> int:
    cur = conn.cursor()
    cur.execute("INSERT INTO runs (commit_sha, branch, created_at) "
                "VALUES (?, ?, ?)",
                (commit_sha, branch, int(time.time())))
    run_id = cur.lastrowid
    cur.executemany(
        "INSERT INTO results (run_id, test_id, outcome, duration_ms, message)"
        " VALUES (?, ?, ?, ?, ?)",
        [(run_id, r["test_id"], r["outcome"], r["duration_ms"], r["message"])
         for r in results])
    conn.commit()
    return run_id


def test_history(conn: sqlite3.Connection, test_id: str,
                 limit: int = 50) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        "SELECT r.outcome, r.duration_ms, runs.commit_sha, runs.created_at"
        " FROM results r JOIN runs ON r.run_id = runs.id"
        " WHERE r.test_id = ? ORDER BY runs.id DESC LIMIT ?",
        (test_id, limit))
    return [{"outcome": o, "duration_ms": d, "commit": c, "at": t}
            for o, d, c, t in cur.fetchall()]


def all_test_ids(conn: sqlite3.Connection) -> list[str]:
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT test_id FROM results")
    return [row[0] for row in cur.fetchall()]


def quarantine_add(conn: sqlite3.Connection, test_id: str,
                   reason: str = "") -> None:
    conn.execute("INSERT OR REPLACE INTO quarantine "
                 "(test_id, quarantined_at, reason) VALUES (?, ?, ?)",
                 (test_id, int(time.time()), reason))
    conn.commit()


def quarantine_remove(conn: sqlite3.Connection, test_id: str) -> None:
    conn.execute("DELETE FROM quarantine WHERE test_id = ?", (test_id,))
    conn.commit()


def quarantined(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.cursor()
    cur.execute("SELECT test_id, quarantined_at, reason FROM quarantine")
    return [{"test_id": t, "at": a, "reason": r} for t, a, r in cur.fetchall()]


def is_quarantined(conn: sqlite3.Connection, test_id: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM quarantine WHERE test_id = ?", (test_id,))
    return cur.fetchone() is not None
