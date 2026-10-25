"""What flakiness cost, from the reports you recorded and nothing else.

Every number comes from the JUnit files flakebox has ingested: each test
case's `time` attribute and which commit each run was for. There is no
industry average, no price per minute and no guess about your runners in
here; where the reports cannot say something, this module does not either.

Over the newest `window` recorded runs:

- test time: the sum of every executed test case's reported time;
- flake time: the time of the FAILED attempts of tests that are flaky now
  (a failed or errored result, or one that failed and passed on retry --
  for a retry the report keeps the longest attempt, so this is a floor);
- runs hit: runs in which a flaky test failed at least once;
- repeat runs: runs for a commit that already had a run -- reruns, as far
  as your history shows -- and the test time they took.

It is test time as the test runner reports it. CI setup, checkout, queueing
and anything the runner does not put in a `time` attribute are not in it.
"""

from __future__ import annotations

from . import store as store_mod

WINDOW = 50
FAIL_OUTCOMES = ("failed", "error", "retried")


def estimate(conn, flaky_ids, window: int = WINDOW) -> dict | None:
    """The figures above, or None when nothing is recorded."""
    run_ids = store_mod.recent_run_ids(conn, window)
    if not run_ids:
        return None
    marks = ",".join("?" * len(run_ids))
    flaky = set(flaky_ids)
    total_ms = flake_ms = 0
    hit: set[int] = set()
    run_ms: dict[int, int] = {}
    for run_id, test_id, outcome, ms in conn.execute(
            f"SELECT run_id, test_id, outcome, duration_ms FROM results "
            f"WHERE run_id IN ({marks})", run_ids):
        if outcome == "skipped":
            continue
        ms = ms or 0
        total_ms += ms
        run_ms[run_id] = run_ms.get(run_id, 0) + ms
        if test_id in flaky and outcome in FAIL_OUTCOMES:
            flake_ms += ms
            hit.add(run_id)
    repeat_runs = repeat_ms = 0
    seen: set[str] = set()
    # oldest first: the first run of a commit is the original, later ones
    # are repeats. A run without a commit is never a repeat.
    for run_id, sha in conn.execute(
            f"SELECT id, commit_sha FROM runs WHERE id IN ({marks}) "
            f"ORDER BY created_at, id", run_ids):
        if sha is None:
            continue
        if sha in seen:
            repeat_runs += 1
            repeat_ms += run_ms.get(run_id, 0)
        seen.add(sha)
    return {"runs": len(run_ids), "test_ms": total_ms, "flake_ms": flake_ms,
            "runs_hit": len(hit), "repeat_runs": repeat_runs,
            "repeat_ms": repeat_ms}


def minutes(ms: int) -> str:
    """Milliseconds as minutes, one decimal ('0.4 min'); under a second of
    test time says so instead of printing 0.0."""
    if ms <= 0:
        return "0 min"
    m = ms / 60000
    return "<0.1 min" if m < 0.05 else f"{m:.1f} min"


def line(est: dict | None) -> str:
    """One plain sentence for a report. Empty when there is nothing to say."""
    if not est:
        return ""
    parts = [f"flaky tests failed in {est['runs_hit']} of the last "
             f"{est['runs']} run(s)"]
    if est["test_ms"]:
        parts[0] += (f", {minutes(est['flake_ms'])} of "
                     f"{minutes(est['test_ms'])} of reported test time")
    else:
        parts[0] += " (the reports carry no test times, so no minutes)"
    if est["repeat_runs"]:
        parts.append(f"{est['repeat_runs']} run(s) repeated a commit that "
                     f"had already run ({minutes(est['repeat_ms'])} of test "
                     f"time)")
    return ("Cost, from your own reports: " + "; ".join(parts) +
            ". Test time only: setup and queueing are not in a JUnit "
            "report.")
