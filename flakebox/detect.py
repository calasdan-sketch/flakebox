"""Flake detection: statistical, explainable, no ML.

Definitions (skipped runs never count as evidence either way):

- executed runs = passes + failures (``failed`` and ``error`` both fail)
- FLAKY   = at least one pass AND at least one failure across the window,
            over at least ``min_runs`` executed runs, and not currently on
            a pass streak of ``stable_runs``.
- RECOVERED = would be flaky, but the newest ``stable_runs`` executed
            runs all passed: it has recovered and is left alone (not
            re-quarantined); ``report`` and ``history`` both say so.
- FAILING = executed at least ``min_runs`` times and never passed.
            Broken, not flaky - a different problem, don't quarantine it.
- PASSING = executed at least ``min_runs`` times and never failed.
- STABLE  = a quarantined test whose newest ``stable_runs`` executed runs
            all passed - the report tells you to un-quarantine it.
- RETIRED = a test with history that appears in none of the newest
            ``retire_after`` runs: deleted or renamed. Listed on its own
            and otherwise left out, so a test removed from the suite
            does not stay "consistently failing" for fifty more builds.

classify() is the one place these words are decided; report, history
and the JSON output all go through it.
"""

from __future__ import annotations

from . import store as store_mod

FLAKY, FAILING, PASSING, UNKNOWN = "flaky", "failing", "passing", "unknown"
RECOVERED = "recovered"
FAIL_OUTCOMES = ("failed", "error")
RETIRE_AFTER = 3  # absent from this many newest runs -> retired
STABLE_RUNS = 10  # consecutive newest passes that make a test stable


def classify(passes: int, failures: int, pass_streak: int = 0,
             stable_runs: int = 0) -> str:
    """The status word for a test's executed runs (no min-runs
    threshold). With `stable_runs` > 0, a pass-and-fail history whose
    newest `stable_runs` executed runs all passed is RECOVERED rather
    than FLAKY - the same call `report` makes, so `history` agrees."""
    if passes and failures:
        if stable_runs and pass_streak >= stable_runs:
            return RECOVERED
        return FLAKY
    if failures:
        return FAILING
    if passes:
        return PASSING
    return UNKNOWN


def stats(outcomes_newest_first: list[str], stable_runs: int = 0) -> dict:
    """Pass/fail/skip counts, fail rate over executed runs, the current
    pass streak (consecutive newest passes; a skip neither extends nor
    breaks it) and the status from classify()."""
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
        "status": classify(passes, fails, streak, stable_runs),
    }


def summarize(conn, test_id: str, window: int = 50,
              retire_after: int = RETIRE_AFTER,
              stable_runs: int = STABLE_RUNS) -> dict:
    """One test's stats with the same status word `report` would use
    (`flakebox history`)."""
    hist = store_mod.test_history(conn, test_id, limit=window)  # newest first
    s = stats([h["outcome"] for h in hist], stable_runs)
    s["test_id"] = test_id
    s["quarantined"] = store_mod.is_quarantined(conn, test_id)
    s["retired"] = bool(hist) and test_id not in store_mod.active_test_ids(
        conn, retire_after)
    return s


def analyze(conn, min_runs: int = 5, stable_runs: int = STABLE_RUNS,
            window: int = 50, retire_after: int = RETIRE_AFTER) -> dict:
    """Return {flaky: [...], broken: [...], stable: [...], retired: [...]}
    ranked lists.

    Single batched query (the most recent `window` runs per test, via
    ROW_NUMBER), then the counts from stats() in Python - no per-test
    N+1 history fetches. Iteration order is first appearance in the
    results table, which is the order of the unsorted `stable` list.
    Tests absent from the newest `retire_after` runs go to `retired`
    (with where they were last seen) and nowhere else.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT test_id, outcome, first_seen, last_run FROM ("
        "  SELECT r.test_id AS test_id, r.outcome AS outcome,"
        "         r.run_id AS run_id,"
        "         ROW_NUMBER() OVER (PARTITION BY r.test_id"
        "                            ORDER BY r.run_id DESC) AS rn,"
        "         MIN(r.rowid) OVER (PARTITION BY r.test_id) AS first_seen,"
        "         MAX(r.run_id) OVER (PARTITION BY r.test_id) AS last_run"
        "  FROM results r"
        ") WHERE rn <= ? ORDER BY test_id, run_id DESC",
        (window,))
    # outcomes per test, newest-run first (rn 1..window)
    by_test: dict[str, tuple[list[str], int, int]] = {}
    for test_id, outcome, first_seen, last_run in cur.fetchall():
        if test_id in by_test:
            by_test[test_id][0].append(outcome)
        else:
            by_test[test_id] = ([outcome], first_seen, last_run)
    qcur = conn.cursor()
    qcur.execute("SELECT test_id FROM quarantine")
    quarantined_ids = {row[0] for row in qcur.fetchall()}
    recent = store_mod.recent_run_ids(conn, retire_after)
    horizon = min(recent) if recent else 0  # oldest run that still counts
    flaky, broken, stable, retired = [], [], [], []
    for test_id, (outcomes, _first_seen, last_run) in sorted(
            by_test.items(), key=lambda kv: kv[1][1]):
        s = stats(outcomes, stable_runs)
        s["test_id"] = test_id
        s["quarantined"] = test_id in quarantined_ids
        if last_run < horizon:
            s["retired"] = True
            s["last_run"] = last_run
            retired.append(s)
            continue
        s["retired"] = False
        if s["runs"] < min_runs:
            continue  # not enough executed runs to say anything
        if s["status"] == FLAKY:  # classify() already set RECOVERED aside
            flaky.append(s)
        elif s["status"] == FAILING:
            broken.append(s)
        if s["quarantined"] and s["pass_streak"] >= stable_runs:
            stable.append(s)
    flaky.sort(key=lambda s: (-s["flake_rate"], -s["failures"]))
    broken.sort(key=lambda s: -s["failures"])
    retired.sort(key=lambda s: (-s["last_run"], s["test_id"]))
    if retired:
        commits = dict(conn.execute(
            "SELECT id, commit_sha FROM runs WHERE id IN (%s)"
            % ",".join("?" * len(retired)),
            [s["last_run"] for s in retired]).fetchall())
        for s in retired:
            s["last_commit"] = commits.get(s["last_run"])
    return {"flaky": flaky, "broken": broken, "stable": stable,
            "retired": retired}
