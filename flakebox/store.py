"""SQLite store: runs, per-test results, quarantine set; portable JSON
export/import so history can travel between CI caches and laptops."""

from __future__ import annotations

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    commit_sha TEXT,
    branch TEXT,
    created_at INTEGER,
    report_sha TEXT
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

EXPORT_FORMAT = 1


def connect(db_path: str) -> sqlite3.Connection:
    """Open (creating/migrating as needed). A file that is not a SQLite
    database raises sqlite3.DatabaseError - the CLI turns that into a
    clean error exit."""
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    # concurrent writers on stock SQLite: wait instead of immediately
    # raising "database is locked", and use WAL so readers don't block
    # writers (no-op on :memory:, which has no journal file).
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """DBs recorded by flakebox < 0.3 have no report_sha column."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "report_sha" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN report_sha TEXT")
        conn.commit()


def find_run(conn: sqlite3.Connection, commit_sha: str | None,
             report_sha: str | None) -> int | None:
    """The id of a run already recorded from the same report (same
    commit, same report bytes), or None."""
    if report_sha is None:
        return None
    row = conn.execute(
        "SELECT id FROM runs WHERE commit_sha IS ? AND report_sha = ? "
        "ORDER BY id LIMIT 1", (commit_sha, report_sha)).fetchone()
    return row[0] if row else None


def record_run(conn: sqlite3.Connection, results: list[dict],
               commit_sha: str | None = None,
               branch: str | None = None,
               report_sha: str | None = None,
               created_at: int | None = None) -> int:
    cur = conn.cursor()
    cur.execute("INSERT INTO runs (commit_sha, branch, created_at, report_sha) "
                "VALUES (?, ?, ?, ?)",
                (commit_sha, branch,
                 int(time.time()) if created_at is None else int(created_at),
                 report_sha))
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


def recent_run_ids(conn: sqlite3.Connection, n: int) -> list[int]:
    """Ids of the newest `n` recorded runs (newest first)."""
    cur = conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT ?",
                       (max(int(n), 0),))
    return [row[0] for row in cur.fetchall()]


def active_test_ids(conn: sqlite3.Connection, retire_after: int) -> set[str]:
    """Tests that appeared in at least one of the newest `retire_after`
    runs. Anything else with history is retired: deleted or renamed, and
    must not be reported as failing forever."""
    ids = recent_run_ids(conn, retire_after)
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    cur = conn.execute(
        f"SELECT DISTINCT test_id FROM results WHERE run_id IN ({marks})", ids)
    return {row[0] for row in cur.fetchall()}


def last_seen(conn: sqlite3.Connection, test_id: str) -> dict | None:
    """Newest run that contains the test: {run_id, commit, at, outcome}."""
    row = conn.execute(
        "SELECT runs.id, runs.commit_sha, runs.created_at, r.outcome"
        " FROM results r JOIN runs ON r.run_id = runs.id"
        " WHERE r.test_id = ? ORDER BY runs.id DESC LIMIT 1",
        (test_id,)).fetchone()
    if row is None:
        return None
    return {"run_id": row[0], "commit": row[1], "at": row[2], "outcome": row[3]}


def quarantine_add(conn: sqlite3.Connection, test_id: str,
                   reason: str = "", at: int | None = None) -> bool:
    """Add a test to the quarantine set. Idempotent (set semantics): an
    already-quarantined test keeps its original timestamp and reason.
    Returns True if the test was newly quarantined."""
    cur = conn.execute("INSERT OR IGNORE INTO quarantine "
                       "(test_id, quarantined_at, reason) VALUES (?, ?, ?)",
                       (test_id, int(time.time()) if at is None else int(at),
                        reason))
    conn.commit()
    return cur.rowcount == 1


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


# ---------------------------------------------------------------------------
# export / import: the whole DB as one JSON document
# ---------------------------------------------------------------------------


def export_data(conn: sqlite3.Connection) -> dict:
    """Everything in the DB as plain JSON (runs with their results, and
    the quarantine set). Written by `flakebox export`; read back by
    `flakebox import`, which merges instead of overwriting."""
    runs = []
    cur = conn.execute("SELECT id, commit_sha, branch, created_at, report_sha "
                       "FROM runs ORDER BY id")
    for run_id, commit, branch, at, sha in cur.fetchall():
        rows = conn.execute(
            "SELECT test_id, outcome, duration_ms, message FROM results "
            "WHERE run_id = ? ORDER BY rowid", (run_id,)).fetchall()
        runs.append({"commit": commit, "branch": branch, "created_at": at,
                     "report_sha": sha,
                     "results": [list(r) for r in rows]})
    return {"flakebox_export": EXPORT_FORMAT, "runs": runs,
            "quarantine": quarantined(conn)}


def export_file(conn: sqlite3.Connection, path: str) -> dict:
    data = export_data(conn)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    return data


def import_data(conn: sqlite3.Connection, data: dict) -> dict:
    """Merge an export into this DB. A run is skipped when a run from the
    same report (commit + report_sha) is already here, so importing the
    same file twice, or importing into the DB it came from, adds nothing.
    Runs without a report_sha cannot be recognised and are always added.
    Quarantine entries merge as a set. Returns counts."""
    if not isinstance(data, dict) or data.get("flakebox_export") != EXPORT_FORMAT:
        raise ValueError("not a flakebox export "
                         f"(expected \"flakebox_export\": {EXPORT_FORMAT})")
    added = skipped = 0
    runs = data.get("runs") or []
    if not isinstance(runs, list):
        raise ValueError("export: \"runs\" must be a list")
    for run in sorted(runs, key=lambda r: (r.get("created_at") or 0)):
        sha = run.get("report_sha")
        if find_run(conn, run.get("commit"), sha) is not None:
            skipped += 1
            continue
        results = [{"test_id": t, "outcome": o, "duration_ms": d, "message": m}
                   for t, o, d, m in run.get("results") or []]
        record_run(conn, results, run.get("commit"), run.get("branch"),
                   report_sha=sha, created_at=run.get("created_at"))
        added += 1
    q_added = 0
    for q in data.get("quarantine") or []:
        if quarantine_add(conn, q["test_id"], q.get("reason") or "",
                          at=q.get("at")):
            q_added += 1
    return {"runs_added": added, "runs_skipped": skipped,
            "quarantine_added": q_added}


def import_file(conn: sqlite3.Connection, path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return import_data(conn, data)
