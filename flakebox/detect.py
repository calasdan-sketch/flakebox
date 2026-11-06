"""Flake detection: statistical, explainable, no ML.

Definitions (skipped runs never count as evidence either way):

- executed runs = runs that passed, failed (``failed`` and ``error``) or
            failed then passed on the runner's retry (``passed_on_rerun``,
            which counts as both a pass and a failure)
- PROVEN  = flaky with proof: it passed and failed at the same commit,
            or failed and passed inside one run. Needs no ``min_runs``.
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

"Newest" is by the run's ``created_at`` (the row id only breaks a tie), so
importing an older export after newer local runs changes no verdict.

classify() is the one place these words are decided; report, history
and the JSON output all go through it.
"""

from __future__ import annotations

import re

from . import store as store_mod

FLAKY, FAILING, PASSING, UNKNOWN = "flaky", "failing", "passing", "unknown"
RECOVERED = "recovered"
FAIL_OUTCOMES = ("failed", "error")
RERUN = "passed_on_rerun"  # failed then passed inside one run (see junit.py)
RETIRE_AFTER = 3  # absent from this many newest runs -> retired
STABLE_RUNS = 10  # consecutive newest passes that make a test stable
FAILURE_MODES = 3  # most common failure signatures kept per test


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


_VOLATILE = re.compile(r"0x[0-9a-fA-F]+|\d+(?:[.,]\d+)*")
_SPACE = re.compile(r"\s+")


def failure_signature(message) -> str:
    """A failure message with the parts that change from run to run
    (numbers, durations, ports, ids, 0x addresses) replaced by `N` and
    whitespace collapsed, first line only, at most 160 characters - so
    `timeout after 30.2s` and `timeout after 31.7s` are one failure mode.
    An empty message is `(no message)`."""
    # only the head of a message can matter (and an imported one can be
    # any size): look at the first 4 KiB
    first = next((ln for ln in (message or "")[:4096].splitlines()
                  if ln.strip()), "")
    sig = _SPACE.sub(" ", _VOLATILE.sub("N", first)).strip()
    return sig[:160] if sig else "(no message)"


def stats(outcomes_newest_first: list[str], stable_runs: int = 0,
          commits: list | None = None,
          messages: list | None = None) -> dict:
    """Pass/fail/skip counts, fail rate over executed runs, the current
    pass streak (consecutive newest passes; a skip neither extends nor
    breaks it), the status from classify(), and the proof of flakiness:

    - same_run_flips: runs in which the test failed and then passed on
      the runner's own retry (outcome `passed_on_rerun`). That run counts
      once as executed, and as both a pass and a failure, so its status
      is flaky; it breaks a pass streak.
    - same_commit_flips: commits (from `commits`, aligned with the
      outcomes; None entries ignored) at which the test both passed and
      failed - the same code gave both answers (Google's definition of
      a flaky result).
    - failure_modes: the most common failure_signature()s of the failing
      runs (from `messages`, aligned with the outcomes), most frequent
      first.
    """
    passes = fails = skips = executed = reruns = 0
    for o in outcomes_newest_first:
        if o == "passed":
            passes += 1
        elif o in FAIL_OUTCOMES:
            fails += 1
        elif o == RERUN:
            passes += 1
            fails += 1
            reruns += 1
        elif o == "skipped":
            skips += 1
            continue
        else:
            continue  # an outcome this version does not know: no evidence
        executed += 1
    streak = 0
    for o in outcomes_newest_first:
        if o == "passed":
            streak += 1
        elif o == "skipped":
            continue
        else:
            break
    by_commit: dict = {}
    for i, o in enumerate(outcomes_newest_first):
        commit = commits[i] if commits and i < len(commits) else None
        if commit is None or commit == "":
            continue
        seen = by_commit.setdefault(commit, set())
        if o in ("passed", RERUN):
            seen.add("pass")
        if o in FAIL_OUTCOMES or o == RERUN:
            seen.add("fail")
    commit_flips = sum(1 for seen in by_commit.values() if len(seen) == 2)
    modes: dict[str, int] = {}
    if messages:
        for i, o in enumerate(outcomes_newest_first):
            if o in FAIL_OUTCOMES or o == RERUN:
                sig = failure_signature(messages[i] if i < len(messages)
                                        else "")
                modes[sig] = modes.get(sig, 0) + 1
    ranked = sorted(modes.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "runs": executed,          # executed runs (skips excluded)
        "recorded": len(outcomes_newest_first),
        "passes": passes,
        "failures": fails,
        "skips": skips,
        "flake_rate": (fails / executed) if executed else 0.0,
        "pass_streak": streak,
        "status": classify(passes, fails, streak, stable_runs),
        "same_run_flips": reruns,
        "same_commit_flips": commit_flips,
        "proven": bool(reruns or commit_flips),
        "failure_modes": [{"message": m, "count": c}
                          for m, c in ranked[:FAILURE_MODES]],
    }


def summarize(conn, test_id: str, window: int = 50,
              retire_after: int = RETIRE_AFTER,
              stable_runs: int = STABLE_RUNS,
              use_commits: bool = True) -> dict:
    """One test's stats with the same status word `report` would use
    (`flakebox history`)."""
    hist = store_mod.test_history(conn, test_id, limit=window)  # newest first
    s = stats([h["outcome"] for h in hist], stable_runs,
              commits=[h["commit"] for h in hist] if use_commits else None,
              messages=[h["message"] for h in hist])
    s["test_id"] = test_id
    s["quarantined"] = store_mod.is_quarantined(conn, test_id)
    # retire_after 0 disables retirement, as it does for analyze(): with no
    # recent runs to look in, every test would otherwise be "absent"
    s["retired"] = (retire_after > 0 and bool(hist)
                    and test_id not in store_mod.active_test_ids(
                        conn, retire_after))
    return s


def analyze(conn, min_runs: int = 5, stable_runs: int = STABLE_RUNS,
            window: int = 50, retire_after: int = RETIRE_AFTER,
            use_commits: bool = True) -> dict:
    """Return {flaky: [...], broken: [...], stable: [...], retired: [...]}
    ranked lists.

    Single batched query (the most recent `window` runs per test, via
    ROW_NUMBER over (created_at, id) - see store.NEWEST_FIRST), then the
    counts from stats() in Python - no per-test N+1 history fetches.
    Iteration order is first appearance in the results table, which is the
    order of the unsorted `stable` list. Tests absent from the newest
    `retire_after` runs go to `retired` (with where they were last seen)
    and nowhere else.

    A flaky test with proof (stats(): it passed and failed at the same
    commit, or failed and passed on a retry inside one run) is flaky with
    fewer than `min_runs` executed runs too: the same code gave both
    answers, so more runs cannot make it less flaky. `use_commits=False`
    ignores same-commit evidence (local runs on an uncommitted worktree
    reuse one commit for different code). Proven flakes rank first.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT test_id, outcome, run_id, created_at, first_seen,"
        "       commit_sha, message FROM ("
        "  SELECT r.test_id AS test_id, r.outcome AS outcome,"
        "         r.run_id AS run_id, runs.created_at AS created_at,"
        "         runs.commit_sha AS commit_sha, r.message AS message,"
        "         ROW_NUMBER() OVER (PARTITION BY r.test_id"
        f"                            ORDER BY {store_mod.NEWEST_FIRST}"
        "                           ) AS rn,"
        "         MIN(r.rowid) OVER (PARTITION BY r.test_id) AS first_seen"
        "  FROM results r JOIN runs ON r.run_id = runs.id"
        ") WHERE rn <= ? ORDER BY test_id, rn",
        (window,))
    # outcomes per test, newest-run first (rn 1..window); the first row of
    # a test is its newest run
    by_test: dict[str, tuple] = {}
    for (test_id, outcome, run_id, at, first_seen, commit,
         message) in cur.fetchall():
        if test_id in by_test:
            entry = by_test[test_id]
            entry[0].append(outcome)
            entry[4].append(commit)
            entry[5].append(message)
        else:
            by_test[test_id] = ([outcome], first_seen, run_id, at,
                                [commit], [message])
    qcur = conn.cursor()
    qcur.execute("SELECT test_id FROM quarantine")
    quarantined_ids = {row[0] for row in qcur.fetchall()}
    recent = set(store_mod.recent_run_ids(conn, retire_after))
    flaky, broken, stable, retired = [], [], [], []
    last_seen_at: dict[str, int | None] = {}
    for test_id, (outcomes, _first_seen, last_run, last_at, commits,
                  messages) in sorted(by_test.items(),
                                      key=lambda kv: kv[1][1]):
        s = stats(outcomes, stable_runs,
                  commits=commits if use_commits else None,
                  messages=messages)
        s["test_id"] = test_id
        s["quarantined"] = test_id in quarantined_ids
        if recent and last_run not in recent:  # none of its runs is recent
            s["retired"] = True
            s["last_run"] = last_run
            last_seen_at[test_id] = last_at
            retired.append(s)
            continue
        s["retired"] = False
        if s["status"] == FLAKY and s["proven"]:
            flaky.append(s)  # proof needs no minimum number of runs
            continue
        if s["runs"] < min_runs:
            continue  # not enough executed runs to say anything
        if s["status"] == FLAKY:  # classify() already set RECOVERED aside
            flaky.append(s)
        elif s["status"] == FAILING:
            broken.append(s)
        if s["quarantined"] and s["pass_streak"] >= stable_runs:
            stable.append(s)
    flaky.sort(key=lambda s: (not s["proven"], -s["flake_rate"],
                              -s["failures"]))
    broken.sort(key=lambda s: -s["failures"])
    never = float("-inf")  # a run without a timestamp is the oldest
    retired.sort(key=lambda s: (
        -(never if last_seen_at[s["test_id"]] is None
          else last_seen_at[s["test_id"]]), -s["last_run"], s["test_id"]))
    if retired:
        commits = dict(conn.execute(
            "SELECT id, commit_sha FROM runs WHERE id IN (%s)"
            % ",".join("?" * len(retired)),
            [s["last_run"] for s in retired]).fetchall())
        for s in retired:
            s["last_commit"] = commits.get(s["last_run"])
    return {"flaky": flaky, "broken": broken, "stable": stable,
            "retired": retired}
