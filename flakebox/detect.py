"""Flake detection: statistical, explainable, no ML.

Definitions (skipped runs never count as evidence either way):

- executed runs = passes + failures (``failed`` and ``error`` both fail)
- FLAKY   = at least one pass AND at least one failure across the window,
            over at least ``min_runs`` executed runs, and not currently on
            a pass streak of ``stable_runs`` (that is a recovered test).
- FAILING = executed at least ``min_runs`` times and never passed.
            Broken, not flaky - a different problem, don't quarantine it.
- PASSING = executed at least ``min_runs`` times and never failed.
- STABLE  = a quarantined test whose newest ``stable_runs`` executed runs
            all passed - the report tells you to un-quarantine it.
"""

from __future__ import annotations

from . import store as store_mod

FLAKY, FAILING, PASSING, UNKNOWN = "flaky", "failing", "passing", "unknown"
FAIL_OUTCOMES = ("failed", "error")


def classify(passes: int, failures: int) -> str:
    """The three-way call on executed runs alone (no threshold)."""
    if passes and failures:
        return FLAKY
    if failures:
        return FAILING
    if passes:
        return PASSING
    return UNKNOWN


def stats(outcomes_newest_first: list[str]) -> dict:
    """Pass/fail/skip counts, fail rate over executed runs, and the
    current pass streak (consecutive newest passes; a skip neither
    extends nor breaks it)."""
    passes = sum(1 for o in outcomes_newest_first if o == "passed")
    fails = sum(1 for o in outcomes_newest_first if o in FAIL_OUTCOMES)
    skips = sum(1 for o in outcomes_newest_first if o == "skipped")
    streak = 0
    for o in outcomes_newest_first:
        if o == "passed":
            streak += 1
        elif o == "skipped":
            continue
        else:
            break
    executed = passes + fails
    return {
        "runs": executed,          # executed runs (skips excluded)
        "recorded": len(outcomes_newest_first),
        "passes": passes,
        "failures": fails,
        "skips": skips,
        "flake_rate": (fails / executed) if executed else 0.0,
        "pass_streak": streak,
        "status": classify(passes, fails),
    }


def summarize(conn, test_id: str, window: int = 50) -> dict:
    hist = store_mod.test_history(conn, test_id, limit=window)  # newest first
    s = stats([h["outcome"] for h in hist])
    s["test_id"] = test_id
    s["quarantined"] = store_mod.is_quarantined(conn, test_id)
    return s


def analyze(conn, min_runs: int = 5, stable_runs: int = 10,
            window: int = 50) -> dict:
    """Return {flaky: [...], broken: [...], stable: [...]} ranked lists.

    Single batched query (the most recent `window` runs per test, via
    ROW_NUMBER), then the counts from stats() in Python - no per-test
    N+1 history fetches. Iteration order is first appearance in the
    results table, which is the order of the unsorted `stable` list.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT test_id, outcome, first_seen FROM ("
        "  SELECT r.test_id AS test_id, r.outcome AS outcome,"
        "         r.run_id AS run_id,"
        "         ROW_NUMBER() OVER (PARTITION BY r.test_id"
        "                            ORDER BY r.run_id DESC) AS rn,"
        "         MIN(r.rowid) OVER (PARTITION BY r.test_id) AS first_seen"
        "  FROM results r"
        ") WHERE rn <= ? ORDER BY test_id, run_id DESC",
        (window,))
    # outcomes per test, newest-run first (rn 1..window)
    by_test: dict[str, tuple[list[str], int]] = {}
    for test_id, outcome, first_seen in cur.fetchall():
        if test_id in by_test:
            by_test[test_id][0].append(outcome)
        else:
            by_test[test_id] = ([outcome], first_seen)
    qcur = conn.cursor()
    qcur.execute("SELECT test_id FROM quarantine")
    quarantined_ids = {row[0] for row in qcur.fetchall()}
    flaky, broken, stable = [], [], []
    for test_id, (outcomes, _first_seen) in sorted(
            by_test.items(), key=lambda kv: kv[1][1]):
        s = stats(outcomes)
        s["test_id"] = test_id
        s["quarantined"] = test_id in quarantined_ids
        if s["runs"] < min_runs:
            continue  # not enough executed runs to say anything
        recovered = s["pass_streak"] >= stable_runs
        if s["status"] == FLAKY and not recovered:
            flaky.append(s)
        elif s["status"] == FAILING:
            broken.append(s)
        if s["quarantined"] and recovered:
            stable.append(s)
    flaky.sort(key=lambda s: (-s["flake_rate"], -s["failures"]))
    broken.sort(key=lambda s: -s["failures"])
    return {"flaky": flaky, "broken": broken, "stable": stable}
