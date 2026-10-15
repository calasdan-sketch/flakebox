"""Flake detection: statistical, explainable, no ML.

A test is FLAKY when it has both passes and failures across enough runs.
A test that only ever fails is BROKEN (not flaky) — different problem.
A quarantined test with enough consecutive passes is STABLE (un-quarantine).
"""

from __future__ import annotations

from . import store as store_mod


def summarize(conn, test_id: str, window: int = 50) -> dict:
    hist = store_mod.test_history(conn, test_id, limit=window)
    # chronological for streak math
    hist = hist[::-1]
    passes = sum(1 for h in hist if h["outcome"] == "passed")
    fails = sum(1 for h in hist if h["outcome"] in ("failed", "error"))
    skips = sum(1 for h in hist if h["outcome"] == "skipped")
    streak = 0
    for h in reversed(hist):
        if h["outcome"] == "passed":
            streak += 1
        else:
            break
    total = passes + fails
    return {
        "test_id": test_id,
        "runs": len(hist),
        "passes": passes,
        "failures": fails,
        "skips": skips,
        "flake_rate": (fails / total) if total else 0.0,
        "pass_streak": streak,
        "quarantined": store_mod.is_quarantined(conn, test_id),
    }


def analyze(conn, min_runs: int = 5, stable_runs: int = 10,
            window: int = 50) -> dict:
    """Return {flaky: [...], broken: [...], stable: [...]} ranked lists.

    Single batched query (the most recent `window` runs per test, via
    ROW_NUMBER), then pass/fail/streak counts computed in Python — no
    per-test N+1 history fetches. Output semantics are identical to the
    old per-test logic, including iteration order (first-appearance in
    the results table) for the unsorted `stable` list.
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
    # outcomes per test, newest-run first (rn 1..window), mirroring the
    # old ORDER BY runs.id DESC LIMIT window history used for streaks
    by_test: dict[str, tuple[list[str], int]] = {}
    for test_id, outcome, first_seen in cur.fetchall():
        if test_id in by_test:
            by_test[test_id][0].append(outcome)
        else:
            by_test[test_id] = ([outcome], first_seen)
    # one fetch for the quarantine set (old code did one per test)
    qcur = conn.cursor()
    qcur.execute("SELECT test_id FROM quarantine")
    quarantined_ids = {row[0] for row in qcur.fetchall()}
    flaky, broken, stable = [], [], []
    # first-appearance order, like the old all_test_ids() iteration
    for test_id, (outcomes, _first_seen) in sorted(
            by_test.items(), key=lambda kv: kv[1][1]):
        passes = sum(1 for o in outcomes if o == "passed")
        fails = sum(1 for o in outcomes if o in ("failed", "error"))
        skips = sum(1 for o in outcomes if o == "skipped")
        streak = 0
        for o in outcomes:
            if o == "passed":
                streak += 1
            else:
                break
        total = passes + fails
        s = {
            "test_id": test_id,
            "runs": len(outcomes),
            "passes": passes,
            "failures": fails,
            "skips": skips,
            "flake_rate": (fails / total) if total else 0.0,
            "pass_streak": streak,
            "quarantined": test_id in quarantined_ids,
        }
        if s["runs"] < min_runs:
            continue
        if s["failures"] >= 1 and s["passes"] >= 1:
            flaky.append(s)
        elif s["passes"] == 0 and s["failures"] >= min_runs:
            broken.append(s)
        if s["quarantined"] and s["pass_streak"] >= stable_runs:
            stable.append(s)
    flaky.sort(key=lambda s: (-s["flake_rate"], -s["failures"]))
    broken.sort(key=lambda s: -s["failures"])
    return {"flaky": flaky, "broken": broken, "stable": stable}
