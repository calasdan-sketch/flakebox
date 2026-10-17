"""SQLite store: runs, per-test results, quarantine set; portable JSON
export/import so history can travel between CI caches and laptops."""

from __future__ import annotations

import hashlib
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
    """DBs recorded by flakebox < 0.3 have no report_sha column; runs
    recorded without a report hash (older flakebox, or the API) get a
    content fingerprint so export/import can recognise them."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "report_sha" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN report_sha TEXT")
        conn.commit()
    fingerprint_legacy_runs(conn)


LEGACY_PREFIX = "legacy:"


def legacy_fingerprint(commit_sha, created_at, results) -> str:
    """Stand-in for the report hash of a run recorded without one: a
    digest of commit + timestamp + the results, so the same run exported
    and imported again is recognised. `results` are
    [test_id, outcome, duration_ms, message] rows in recorded order.
    Prefixed so it can never collide with a real report hash."""
    body = json.dumps([commit_sha, created_at,
                       [list(r) for r in results]],
                      sort_keys=True, separators=(",", ":"))
    return LEGACY_PREFIX + hashlib.sha256(body.encode("utf-8")).hexdigest()


def fingerprint_legacy_runs(conn: sqlite3.Connection) -> int:
    """Give every run without a report_sha its fingerprint. Returns how
    many were stamped (normally 0: a quick NULL scan)."""
    rows = conn.execute("SELECT id, commit_sha, created_at FROM runs "
                        "WHERE report_sha IS NULL").fetchall()
    for run_id, commit, at in rows:
        results = conn.execute(
            "SELECT test_id, outcome, duration_ms, message FROM results "
            "WHERE run_id = ? ORDER BY rowid", (run_id,)).fetchall()
        conn.execute("UPDATE runs SET report_sha = ? WHERE id = ?",
                     (legacy_fingerprint(commit, at, results), run_id))
    if rows:
        conn.commit()
    return len(rows)


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
    at = int(time.time()) if created_at is None else int(created_at)
    rows = [(r["test_id"], r["outcome"], r["duration_ms"], r["message"])
            for r in results]
    if report_sha is None:  # no report bytes to hash: fingerprint the content
        report_sha = legacy_fingerprint(commit_sha, at, rows)
    cur = conn.cursor()
    cur.execute("INSERT INTO runs (commit_sha, branch, created_at, report_sha) "
                "VALUES (?, ?, ?, ?)", (commit_sha, branch, at, report_sha))
    run_id = cur.lastrowid
    cur.executemany(
        "INSERT INTO results (run_id, test_id, outcome, duration_ms, message)"
        " VALUES (?, ?, ?, ?, ?)", [(run_id,) + row for row in rows])
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


def _optional(value, types, where: str):
    if value is not None and not isinstance(value, types):
        raise ValueError(f"export: {where} has the wrong type "
                         f"({type(value).__name__})")
    return value


def validate_export(data) -> dict:
    """Check the shape of an export document before anything is written;
    a malformed file is a ValueError naming the first bad field, never a
    traceback halfway through an import. Returns the data."""
    if not isinstance(data, dict) or data.get("flakebox_export") != EXPORT_FORMAT:
        raise ValueError("not a flakebox export "
                         f"(expected \"flakebox_export\": {EXPORT_FORMAT})")
    runs = data.get("runs")
    if runs is None:
        runs = data["runs"] = []
    if not isinstance(runs, list):
        raise ValueError("export: \"runs\" must be a list")
    for i, run in enumerate(runs):
        if not isinstance(run, dict):
            raise ValueError(f"export: runs[{i}] must be an object")
        _optional(run.get("commit"), str, f"runs[{i}].commit")
        _optional(run.get("branch"), str, f"runs[{i}].branch")
        _optional(run.get("report_sha"), str, f"runs[{i}].report_sha")
        _optional(run.get("created_at"), (int, float), f"runs[{i}].created_at")
        if isinstance(run.get("created_at"), bool):
            raise ValueError(f"export: runs[{i}].created_at has the wrong type")
        results = run.get("results")
        if results is None:
            results = run["results"] = []
        if not isinstance(results, list):
            raise ValueError(f"export: runs[{i}].results must be a list")
        for j, row in enumerate(results):
            if (not isinstance(row, (list, tuple)) or len(row) != 4
                    or not isinstance(row[0], str) or not isinstance(row[1], str)):
                raise ValueError(f"export: runs[{i}].results[{j}] must be "
                                 f"[test_id, outcome, duration_ms, message]")
    quarantine = data.get("quarantine")
    if quarantine is None:
        quarantine = data["quarantine"] = []
    if not isinstance(quarantine, list):
        raise ValueError("export: \"quarantine\" must be a list")
    for i, q in enumerate(quarantine):
        if not isinstance(q, dict) or not isinstance(q.get("test_id"), str):
            raise ValueError(f"export: quarantine[{i}] must be an object "
                             f"with a test_id")
    return data


def import_data(conn: sqlite3.Connection, data: dict) -> dict:
    """Merge an export into this DB. A run is skipped when a run from the
    same report (commit + report_sha) is already here, so importing the
    same file twice, or importing into the DB it came from, adds nothing.
    A run exported without a report_sha (recorded by flakebox < 0.3) is
    recognised by its content fingerprint instead. Quarantine entries
    merge as a set. Returns counts."""
    validate_export(data)
    added = skipped = 0
    for run in sorted(data["runs"], key=lambda r: (r.get("created_at") or 0)):
        rows = [tuple(r) for r in run["results"]]
        at = run.get("created_at")
        sha = run.get("report_sha") or legacy_fingerprint(run.get("commit"),
                                                          at, rows)
        if find_run(conn, run.get("commit"), sha) is not None:
            skipped += 1
            continue
        results = [{"test_id": t, "outcome": o, "duration_ms": d, "message": m}
                   for t, o, d, m in rows]
        record_run(conn, results, run.get("commit"), run.get("branch"),
                   report_sha=sha, created_at=at)
        added += 1
    q_added = 0
    for q in data["quarantine"]:
        if quarantine_add(conn, q["test_id"], q.get("reason") or "",
                          at=q.get("at")):
            q_added += 1
    return {"runs_added": added, "runs_skipped": skipped,
            "quarantine_added": q_added}


def import_file(conn: sqlite3.Connection, path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return import_data(conn, data)
