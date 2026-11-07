"""Flake detection: statistical, explainable, no ML.

Definitions (skipped runs never count as evidence either way):

- executed runs = runs that passed, failed (``failed`` and ``error``) or
            failed then passed on the runner's retry (``passed_on_rerun``,
            which ended green: it counts as a pass, never as a failure in
            the flake rate, but makes the test flaky and breaks a streak)
- PROVEN  = flaky with proof: it passed in one run and failed in another
            run of the same commit in the same environment (``--env``), or
            failed and passed on a retry inside one run. Needs no
            ``min_runs``.
- FLAKY   = at least one pass AND at least one failure across the window
            (or a pass on retry),
            over at least ``min_runs`` executed runs, and not currently on
            a pass streak of ``stable_runs``.
- RECOVERED = would be flaky, but the newest ``stable_runs`` executed
            runs all passed: it has recovered and is left alone (not
            re-quarantined); ``report`` and ``history`` both say so.
- REGRESSED = passed and failed, but the failures are a step, not a
            scatter: the failure rate jumped at one run (a likelihood-
            ratio scan, stat.change_point) to at least 80% and has stayed
            there to the newest run. A real break - fix it, don't
            quarantine it. Names the last good and first bad commit.
            ``model="legacy"`` calls it FLAKY, as flakebox < 0.6 did.
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

from . import rootcause
from . import stat as stat_mod
from . import store as store_mod

FLAKY, FAILING, PASSING, UNKNOWN = "flaky", "failing", "passing", "unknown"
RECOVERED = "recovered"
# Passed for a long time, failing ever since: a regression, not a flake
# (model "stat" only).
REGRESSED = "regressed"
MODELS = ("stat", "legacy")
REGRESSED_AFTER = 0.8  # failure rate since the step that makes it a break
FAIL_OUTCOMES = ("failed", "error")
RERUN = "passed_on_rerun"  # failed then passed inside one run (see junit.py)
RETIRE_AFTER = 3  # absent from this many newest runs -> retired
STABLE_RUNS = 10  # consecutive newest passes that make a test stable
FAILURE_MODES = 3  # most common failure signatures kept per test


def classify(passes: int, failures: int, pass_streak: int = 0,
             stable_runs: int = 0, reruns: int = 0) -> str:
    """The status word for a test's executed runs (no min-runs
    threshold). With `stable_runs` > 0, a pass-and-fail history whose
    newest `stable_runs` executed runs all passed is RECOVERED rather
    than FLAKY - the same call `report` makes, so `history` agrees. A run
    that passed only on a retry (`reruns`) is a failure the runner hid, so
    it makes the history flaky even with no run that ended red."""
    if passes and (failures or reruns):
        if stable_runs and pass_streak >= stable_runs:
            return RECOVERED
        return FLAKY
    if failures:
        return FAILING
    if passes:
        return PASSING
    return UNKNOWN


# What failure_signature() folds into `N`: values that change from run to
# run, never the numbers that tell failures apart (`assert 404 == 200` and
# `assert 500 == 200` stay two modes; `user_1`, `test_py39` keep digits).
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_NUMBER = re.compile(
    r"(?:(?P<lead>:)|(?<![\w.]))(?P<int>\d+)(?P<frac>[.,]\d+)?"
    r"(?P<unit>\s?(?:ms|us|µs|ns|s|sec|secs|seconds|min|m|h|%))?(?!\w)")
_SPACE = re.compile(r"\s+")


def _fold_number(m: re.Match) -> str:
    """A number is volatile when it has decimals (`30.2`), a time or
    percent unit (`3000 ms`, `30s`), 4 or more digits (ids, sizes, ports),
    or follows a `:` (`localhost:5432`, `line:17`)."""
    volatile = (m.group("frac") or m.group("unit") or m.group("lead")
                or len(m.group("int")) >= 4)
    if not volatile:
        return m.group(0)
    unit = (m.group("unit") or "").strip()
    return (m.group("lead") or "") + "N" + (unit if unit != "%" else "%")


def failure_signature(message) -> str:
    """A failure message with the parts that change from run to run
    (durations, decimals, ports, numbers of 4+ digits, 0x addresses,
    UUIDs) replaced by `N` and whitespace collapsed, first line only, at
    most 160 characters - so `timeout after 30.2s` and `timeout after
    31.7s` are one failure mode while `HTTP 503` and `HTTP 401` stay
    two. An empty message is `(no message)`."""
    # only the head of a message can matter (and an imported one can be
    # any size): look at the first 4 KiB
    first = next((ln for ln in (message or "")[:4096].splitlines()
                  if ln.strip()), "")
    sig = _UUID.sub("N", first)
    sig = _HEX.sub("N", sig)
    sig = _NUMBER.sub(_fold_number, sig)
    sig = _SPACE.sub(" ", sig).strip()
    return sig[:160] if sig else "(no message)"


def _example(message) -> str:
    first = next((ln.strip() for ln in (message or "")[:4096].splitlines()
                  if ln.strip()), "")
    return first[:160]


def stats(outcomes_newest_first: list[str], stable_runs: int = 0,
          commits: list | None = None,
          messages: list | None = None,
          run_ids: list | None = None, model: str = "stat",
          max_stable: int = 50) -> dict:
    """Pass/fail/skip counts, fail rate over executed runs, the current
    pass streak (consecutive newest passes; a skip neither extends nor
    breaks it), the status from classify(), and the proof of flakiness:

    - same_run_flips: runs in which the test failed and then passed on
      the runner's own retry (outcome `passed_on_rerun`). Such a run
      counts as executed and as a pass; it is not a failure in the flake
      rate (the run ended green) but it makes the status flaky and breaks
      a pass streak.
    - same_commit_flips: (commit, environment) keys - `commits` holds one
      key per outcome, a commit string or a (commit, env) tuple; None and
      empty commits are ignored - at which the test passed in one run and
      failed in a DIFFERENT run (`run_ids`, aligned with the outcomes; two
      results of one run, i.e. a duplicate test id, never prove anything).
      Retry runs take no part: they are counted above.
    - failure_modes: the most common failure_signature()s of the failing
      runs (from `messages`, aligned with the outcomes), most frequent
      first, each with one raw `example` message (the newest).
    """
    passes = fails = skips = executed = reruns = 0
    for o in outcomes_newest_first:
        if o == "passed":
            passes += 1
        elif o in FAIL_OUTCOMES:
            fails += 1
        elif o == RERUN:
            passes += 1
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
    by_key: dict = {}   # key -> {"pass": {run ids}, "fail": {run ids}}
    for i, o in enumerate(outcomes_newest_first):
        key = commits[i] if commits and i < len(commits) else None
        commit = key[0] if isinstance(key, tuple) else key
        if commit is None or commit == "":
            continue
        if o == "passed":
            kind = "pass"
        elif o in FAIL_OUTCOMES:
            kind = "fail"
        else:
            continue
        run = run_ids[i] if run_ids and i < len(run_ids) else ("row", i)
        by_key.setdefault(key, {"pass": set(), "fail": set()})[kind].add(run)
    commit_flips = 0
    for seen in by_key.values():
        # a pass in one run and a failure in another: some pair of runs
        # differs (if both sets were the same single run, it is not proof)
        if seen["pass"] and seen["fail"] and \
                len(seen["pass"] | seen["fail"]) >= 2:
            commit_flips += 1
    modes: dict[str, list] = {}
    if messages:
        for i, o in enumerate(outcomes_newest_first):
            if o in FAIL_OUTCOMES or o == RERUN:
                raw = messages[i] if i < len(messages) else ""
                sig = failure_signature(raw)
                if sig in modes:
                    modes[sig][0] += 1
                else:
                    modes[sig] = [1, _example(raw)]
    ranked = sorted(modes.items(), key=lambda kv: (-kv[1][0], kv[0]))
    # How many straight passes show a flake is gone? Ten is arbitrary: a
    # test that failed 1 run in 10 passes ten in a row 35% of the time
    # while still flaky. The model "stat" asks for as many passes as it
    # would take to see the failure again with 95% confidence at the rate
    # it had before the streak (at least `stable_runs`, at most
    # `max_stable`, normally the window).
    stable_needed = stable_runs
    if model == "stat" and stable_runs and (fails or reruns):
        before = executed - streak
        if before > 0:
            need = stat_mod.runs_to_catch(min((fails + reruns) / before,
                                              0.999))
            stable_needed = max(stable_runs, min(need or 0, max_stable))
    status = classify(passes, fails, streak, stable_needed, reruns)
    low, high = stat_mod.wilson(fails, executed)
    change = None
    proven = bool(reruns or commit_flips)
    if model == "stat" and status == FLAKY and not proven:
        change = _step(outcomes_newest_first, commits, run_ids)
        if change and change["kind"] == "regression":
            status = REGRESSED
    return {
        "runs": executed,          # executed runs (skips excluded)
        "recorded": len(outcomes_newest_first),
        "passes": passes,
        "failures": fails,
        "skips": skips,
        "flake_rate": (fails / executed) if executed else 0.0,
        "pass_streak": streak,
        "stable_needed": stable_needed,
        "rate_low": low,           # 95% Wilson interval of flake_rate
        "rate_high": high,
        "status": status,
        "change": change,
        "same_run_flips": reruns,
        "same_commit_flips": commit_flips,
        "proven": proven,
        "failure_modes": [{"message": m, "count": c, "example": ex,
                           "family": rootcause.family(ex)}
                          for m, (c, ex) in ranked[:FAILURE_MODES]],
    }


def _step(outcomes_newest_first, commits, run_ids):
    """Did the failure rate step? None, or {kind, ...} describing the best
    single change point (stat.change_point) of the executed runs:

    - kind "regression": the rate since the step is >= REGRESSED_AFTER and
      higher than before (it passed, now it fails). `last_good_commit` and
      `first_bad_commit` bracket it for a bisect; `runs_since` counts the
      runs from the first bad one to the newest.
    - kind "onset": the rate rose to something below that (a new flake):
      `first_bad_commit` is the first failure after the step.
    - kind "improvement": the rate fell (partly fixed).
    """
    series, keys = [], []   # oldest first, executed runs only
    for i in range(len(outcomes_newest_first) - 1, -1, -1):
        o = outcomes_newest_first[i]
        if o in FAIL_OUTCOMES:
            series.append(1)
        elif o in ("passed", RERUN):
            series.append(0)
        else:
            continue
        key = commits[i] if commits and i < len(commits) else None
        commit = key[0] if isinstance(key, tuple) else key
        keys.append(commit or None)
    cp = stat_mod.change_point(series)
    if cp is None:
        return None
    at = cp["at"]
    first_bad = next((j for j in range(at, len(series)) if series[j]), None)
    last_good = next((j for j in range(first_bad - 1, -1, -1)
                      if not series[j]), None) if first_bad is not None \
        else None
    if cp["after"] > cp["before"]:
        kind = "regression" if cp["after"] >= REGRESSED_AFTER else "onset"
    else:
        kind = "improvement"
    return {"kind": kind, "before": cp["before"], "after": cp["after"],
            "lr": cp["lr"], "runs_before": at,
            "runs_since": len(series) - (first_bad if first_bad is not None
                                         else at),
            "first_bad_commit": keys[first_bad] if first_bad is not None
            else None,
            "last_good_commit": keys[last_good] if last_good is not None
            else None}


def bucket_of(s: dict, min_runs: int = 5):
    """The report bucket for a stats() dict: "flaky", "regressed",
    "broken", or None (nothing to say yet: too few executed runs, passing,
    recovered). The one place the thresholds meet the statuses, shared by
    analyze() and benchmarks/accuracy.py."""
    if s["status"] == FLAKY and s["proven"]:
        return "flaky"  # proof needs no minimum number of runs
    if s["runs"] < min_runs:
        return None  # not enough executed runs to say anything
    return {FLAKY: "flaky", REGRESSED: "regressed",
            FAILING: "broken"}.get(s["status"])


def summarize(conn, test_id: str, window: int = 50,
              retire_after: int = RETIRE_AFTER,
              stable_runs: int = STABLE_RUNS,
              use_commits: bool = True, model: str = "stat") -> dict:
    """One test's stats with the same status word `report` would use
    (`flakebox history`)."""
    hist = store_mod.test_history(conn, test_id, limit=window)  # newest first
    s = stats([h["outcome"] for h in hist], stable_runs,
              commits=([(h["commit"], h["env"]) for h in hist]
                       if use_commits else None),
              messages=[h["message"] for h in hist],
              run_ids=[h["run_id"] for h in hist], model=model,
              max_stable=window)
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
            use_commits: bool = True, model: str = "stat") -> dict:
    """Return {flaky, regressed, broken, stable, retired} ranked lists
    ranked lists.

    Single batched query (the most recent `window` runs per test, via
    ROW_NUMBER over (created_at, id) - see store.NEWEST_FIRST), then the
    counts from stats() in Python - no per-test N+1 history fetches.
    Iteration order is first appearance in the results table, which is the
    order of the unsorted `stable` list. Tests absent from the newest
    `retire_after` runs go to `retired` (with where they were last seen)
    and nowhere else.

    A flaky test with proof (stats(): it passed in one run and failed in
    another run of the same commit and environment, or failed and passed
    on a retry inside one run) is flaky with
    fewer than `min_runs` executed runs too: the same code gave both
    answers, so more runs cannot make it less flaky. `use_commits=False`
    ignores same-commit evidence (local runs on an uncommitted worktree
    reuse one commit for different code). Proven flakes rank first.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT test_id, outcome, run_id, created_at, first_seen,"
        "       commit_sha, message, env FROM ("
        "  SELECT r.test_id AS test_id, r.outcome AS outcome,"
        "         r.run_id AS run_id, runs.created_at AS created_at,"
        "         runs.commit_sha AS commit_sha, r.message AS message,"
        "         runs.env AS env,"
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
         message, env) in cur.fetchall():
        if test_id in by_test:
            entry = by_test[test_id]
            entry[0].append(outcome)
            entry[4].append((commit, env))
            entry[5].append(message)
            entry[6].append(run_id)
        else:
            by_test[test_id] = ([outcome], first_seen, run_id, at,
                                [(commit, env)], [message], [run_id])
    qcur = conn.cursor()
    qcur.execute("SELECT test_id, expires_at FROM quarantine")
    quarantined_ids, expired_ids = set(), set()
    for tid, expires in qcur.fetchall():
        # a quarantine whose expiry has passed no longer protects the test
        (expired_ids if store_mod.expired({"expires_at": expires})
         else quarantined_ids).add(tid)
    recent = set(store_mod.recent_run_ids(conn, retire_after))
    flaky, regressed, broken, stable, retired = [], [], [], [], []
    last_seen_at: dict[str, int | None] = {}
    for test_id, (outcomes, _first_seen, last_run, last_at, commits,
                  messages, run_ids) in sorted(by_test.items(),
                                               key=lambda kv: kv[1][1]):
        s = stats(outcomes, stable_runs,
                  commits=commits if use_commits else None,
                  messages=messages, run_ids=run_ids, model=model,
                  max_stable=window)
        s["test_id"] = test_id
        s["quarantined"] = test_id in quarantined_ids
        s["quarantine_expired"] = test_id in expired_ids
        if recent and last_run not in recent:  # none of its runs is recent
            s["retired"] = True
            s["last_run"] = last_run
            last_seen_at[test_id] = last_at
            retired.append(s)
            continue
        s["retired"] = False
        which = bucket_of(s, min_runs)
        if which == "flaky":
            flaky.append(s)
        elif which == "regressed":
            regressed.append(s)
        elif which == "broken":
            broken.append(s)
        if (which != "flaky" or not s["proven"]) and s["runs"] >= min_runs \
                and s["quarantined"] \
                and s["pass_streak"] >= s["stable_needed"]:
            stable.append(s)
    flaky.sort(key=lambda s: (not s["proven"], -s["flake_rate"],
                              -s["failures"]))
    regressed.sort(key=lambda s: -s["change"]["runs_since"])
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
    return {"flaky": flaky, "regressed": regressed, "broken": broken,
            "stable": stable, "retired": retired}
