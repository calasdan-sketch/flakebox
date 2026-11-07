"""Re-runnable benchmark: does flakebox call the right thing, and how fast?

    python benchmarks/accuracy.py            # prints a table, exit 0
    python benchmarks/accuracy.py --check    # exit 1 if a floor is missed

Simulated test histories with KNOWN ground truth (seeded: the same numbers
on every machine and every run):

  healthy     never fails
  flaky       fails with a fixed chance each run (2%..50%, log-uniform)
  late-flake  clean, then starts failing with a fixed chance (10%..50%)
  regression  passes, then at a random run starts failing (>= 95% of runs)
  always-red  fails every run

Each history is 60 runs, one commit per run, scored on what `analyze()`
says about the test at the end, with the old rule (model "legacy": a pass
and a failure in the window = flaky) and the new one (model "stat").
"Time to detect" replays every prefix of the history through the same
code and reports the first run at which the call is right and stays right.

A second part scores JUnit retry handling: pytest-rerunfailures writes the
failed attempts as bare <testcase> elements, so a flaky test whose retry
went green looks like a clean pass.

This is a simulation: it shows the decision rules separate the cases,
not how often real suites contain each (real flake rates, burstiness and
shared infrastructure failures differ). Numbers are not a claim about
any other tool.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flakebox import detect, junit, store  # noqa: E402

RUNS = 60
MIN_RUNS = 5
PER_KIND = 400
SEED = 20261107
KINDS = ("healthy", "flaky", "late-flake", "regression", "always-red")
# what the right call is for each kind
TRUTH = {"healthy": None, "flaky": "flaky", "late-flake": "flaky",
         "regression": "regressed", "always-red": "broken"}


def simulate(rng: random.Random, kind: str):
    """(outcomes oldest-first as 0 pass / 1 fail, onset run or 0)."""
    if kind == "healthy":
        return [0] * RUNS, 0
    if kind == "flaky":
        p = math.exp(rng.uniform(math.log(0.02), math.log(0.5)))
        return [int(rng.random() < p) for _ in range(RUNS)], 0
    if kind == "late-flake":
        p = rng.uniform(0.10, 0.50)
        onset = rng.randint(15, 40)
        return [int(i >= onset and rng.random() < p)
                for i in range(RUNS)], onset
    if kind == "regression":
        onset = rng.randint(15, 45)
        p = rng.uniform(0.95, 1.0)
        return [int(i >= onset and rng.random() < p)
                for i in range(RUNS)], onset
    return [1] * RUNS, 0   # always-red


def outcome_names(series):
    return ["failed" if v else "passed" for v in series]


def call(series, model, upto=None):
    """The bucket analyze()'s rules give a history (oldest first)."""
    prefix = series[:upto] if upto else series
    commits = [f"c{i}" for i in range(len(prefix))]
    names = outcome_names(prefix)[::-1]   # stats() wants newest first
    s = detect.stats(names, detect.STABLE_RUNS, commits=commits[::-1],
                     run_ids=list(range(len(prefix)))[::-1], model=model)
    return detect.bucket_of(s, MIN_RUNS)


def final_calls_via_analyze(histories, model):
    """The same final call through the real analyze() on a SQLite DB, as a
    cross-check that call() above is the shipped code path."""
    conn = store.connect(":memory:")
    for i in range(RUNS):
        rows = [{"test_id": f"t{j}", "outcome": "failed" if h[0][i]
                 else "passed", "duration_ms": 0, "message": ""}
                for j, h in enumerate(histories)]
        store.record_run(conn, rows, commit_sha=f"c{i}", created_at=1000 + i)
    a = detect.analyze(conn, min_runs=MIN_RUNS, window=RUNS, model=model)
    conn.close()
    out = {}
    for bucket in ("flaky", "regressed", "broken"):
        for s in a[bucket]:
            out[int(s["test_id"][1:])] = bucket
    return out


def time_to_detect(series, onset, truth, model):
    """Runs after `onset` until the call equals `truth` for good, or None
    if it never does within the history."""
    first = None
    for n in range(MIN_RUNS, RUNS + 1):
        c = call(series, model, upto=n)
        if c == truth:
            if first is None:
                first = n
        else:
            first = None
    return None if first is None else max(first - onset, 1)


def run_accuracy():
    rng = random.Random(SEED)
    data = []   # (kind, series, onset)
    for kind in KINDS:
        for _ in range(PER_KIND):
            series, onset = simulate(rng, kind)
            data.append((kind, series, onset))
    hist = [(d[1],) for d in data]
    results = {}
    for model in ("legacy", "stat"):
        via = final_calls_via_analyze(hist, model)
        table = {k: {} for k in KINDS}
        seen = [0, 0]   # flaky histories with >= 1 failure: total, called
        for j, (kind, series, onset) in enumerate(data):
            got = call(series, model)
            assert got == via.get(j), (model, kind, j, got, via.get(j))
            table[kind][got] = table[kind].get(got, 0) + 1
            if kind in ("flaky", "late-flake") and 0 < sum(series) < RUNS:
                seen[0] += 1
                seen[1] += got == "flaky"
        table["_detectable"] = seen
        results[model] = table
    ttd = {m: {"late-flake": [], "regression": [], "flaky": []}
           for m in ("legacy", "stat")}
    for kind, series, onset in data:
        if kind in ttd["stat"] and (kind != "flaky" or rng.random() < 0.25):
            for m in ("legacy", "stat"):
                ttd[m][kind].append(
                    time_to_detect(series, onset, TRUTH[kind], m))
    return results, ttd


def pct(n, d):
    return 100.0 * n / d if d else 0.0


def summarize(results):
    out = {}
    for model, table in results.items():
        flaky_true = sum(table["flaky"].values()) + \
            sum(table["late-flake"].values())
        flaky_hit = table["flaky"].get("flaky", 0) + \
            table["late-flake"].get("flaky", 0)
        called_flaky = sum(table[k].get("flaky", 0) for k in KINDS)
        reg = table["regression"]
        out[model] = {
            "flaky recall %": pct(flaky_hit, flaky_true),
            "flaky recall, histories with a failure %":
                pct(table["_detectable"][1], table["_detectable"][0]),
            "flaky precision %": pct(flaky_hit, called_flaky),
            "healthy called flaky %": pct(table["healthy"].get("flaky", 0),
                                          PER_KIND),
            "regressions quarantined as flaky %":
                pct(reg.get("flaky", 0), PER_KIND),
            "regressions called regressed %":
                pct(reg.get("regressed", 0), PER_KIND),
            "always-red called broken %":
                pct(table["always-red"].get("broken", 0), PER_KIND),
        }
    return out


# ---- part 2: when is it safe to release a quarantined test? --------------

def ready_to_release(series, model, upto=None):
    prefix = series[:upto] if upto else series
    names = outcome_names(prefix)[::-1]
    s = detect.stats(names, detect.STABLE_RUNS, model=model)
    return s["runs"] >= MIN_RUNS and s["pass_streak"] >= s["stable_needed"]


def run_release(n=PER_KIND):
    """Quarantined tests: still-flaky ones must not be released, fixed ones
    should be, soon. The fix lands at run 30 of 60; the flake rate before
    it is 10%..50% (a fixed test is clean afterwards)."""
    rng = random.Random(SEED + 2)
    out = {m: {"released": 0, "delay": [], "never": 0} for m in
           ("legacy", "stat")}
    still = []
    for _ in range(n):
        p = math.exp(rng.uniform(math.log(0.02), math.log(0.5)))
        still.append([int(rng.random() < p) for _ in range(RUNS)])
    fixed = []
    for _ in range(n):
        p = rng.uniform(0.10, 0.50)
        fixed.append([int(i < 30 and rng.random() < p) for i in range(RUNS)])
    for m in ("legacy", "stat"):
        out[m]["released"] = sum(ready_to_release(x, m) for x in still)
        for x in fixed:
            if not any(x[:30]):
                continue    # never failed before the fix: nothing to release
            hit = next((k for k in range(31, RUNS + 1)
                        if ready_to_release(x, m, k)), None)
            if hit is None:
                out[m]["never"] += 1
            else:
                out[m]["delay"].append(hit - 30)
        out[m]["n_fixed"] = sum(1 for x in fixed if any(x[:30]))
    return {m: {
        "still-flaky tests told 'release' %": pct(v["released"], n),
        "fixed tests released within 30 runs %":
            pct(len(v["delay"]), v["n_fixed"]),
        "fixed tests: median runs until release":
            statistics.median(v["delay"]) if v["delay"] else float("nan"),
    } for m, v in out.items()}


# ---- part 3: retries in the JUnit file ------------------------------------

def pytest_report(first_fail: bool, last_fail: bool, attempts: int):
    """A pytest-rerunfailures report for one test: every attempt but the
    last is a bare <testcase>; the last carries the verdict."""
    bare = '<testcase classname="t" name="test_x" time="0.01"/>' * (attempts - 1)
    last = ('<testcase classname="t" name="test_x" time="0.01"><failure '
            'message="boom"/></testcase>' if last_fail else
            '<testcase classname="t" name="test_x" time="0.01"/>')
    return ('<testsuites name="pytest tests"><testsuite name="pytest">'
            + bare + last + "</testsuite></testsuites>").encode()


def legacy_parse(raw):
    """flakebox < 0.6: one result per <testcase>, no collapsing."""
    entries, _ = junit.raw_entries(raw)
    return entries


def run_retries(n=2000):
    rng = random.Random(SEED + 1)
    flake_seen = {"legacy": 0, "stat": 0}
    red_called_flaky = {"legacy": 0, "stat": 0}
    flake_runs = 0
    for _ in range(n):
        # a flaky test (30% first-attempt failure) whose retry went green
        raw = pytest_report(True, False, attempts=rng.randint(2, 3))
        flake_runs += 1
        for m, res in (("legacy", legacy_parse(raw)),
                       ("stat", junit.parse_bytes(raw))):
            ok = any(r["outcome"] == junit.PASSED_ON_RERUN for r in res)
            flake_seen[m] += ok
        # a test that fails on every attempt, reruns=2
        raw = pytest_report(True, True, attempts=3)
        for m, res in (("legacy", legacy_parse(raw)),
                       ("stat", junit.parse_bytes(raw))):
            passes = sum(1 for r in res if r["outcome"] == "passed")
            fails = sum(1 for r in res if r["outcome"] == "failed")
            red_called_flaky[m] += bool(passes and fails)
    return {m: {"retried-green flakes seen in ONE run %":
                pct(flake_seen[m], flake_runs),
                "always-red tests misread as flaky %":
                pct(red_called_flaky[m], n)} for m in flake_seen}


def fmt(v):
    return "never" if v is None else f"{v:g}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if a committed floor is missed")
    args = ap.parse_args(argv)
    results, ttd = run_accuracy()
    summ = summarize(results)
    print(f"flake classification: {PER_KIND} simulated tests of each of "
          f"{len(KINDS)} kinds, {RUNS} runs each, seed {SEED}\n")
    print(f"{'metric':44} {'legacy':>8} {'stat':>8}")
    for k in summ["stat"]:
        print(f"{k:44} {summ['legacy'][k]:8.1f} {summ['stat'][k]:8.1f}")
    print("\nruns after the onset until the call is right and stays right "
          "(median; share of tests never right in brackets)")
    print(f"{'kind':44} {'legacy':>14} {'stat':>14}")
    med = {}
    for kind in ("late-flake", "regression"):
        cells = []
        for m in ("legacy", "stat"):
            vals = ttd[m][kind]
            ok = [v for v in vals if v is not None]
            never = pct(len(vals) - len(ok), len(vals))
            med[(m, kind)] = (statistics.median(ok) if ok else None, never)
            cells.append(f"{fmt(med[(m, kind)][0])} ({never:.0f}%)")
        print(f"{kind:44} {cells[0]:>14} {cells[1]:>14}")
    rel = run_release()
    print("\nquarantine release (the fix lands at run 30)")
    print(f"{'metric':44} {'legacy':>8} {'stat':>8}")
    for k in rel["stat"]:
        print(f"{k:44} {rel['legacy'][k]:8.1f} {rel['stat'][k]:8.1f}")
    rt = run_retries()
    print("\nretries in the JUnit file (pytest-rerunfailures shape)")
    print(f"{'metric':44} {'legacy':>8} {'stat':>8}")
    for k in rt["stat"]:
        print(f"{k:44} {rt['legacy'][k]:8.1f} {rt['stat'][k]:8.1f}")
    if args.check:
        s = summ["stat"]
        floors = [
            (s["flaky recall %"] >= 90, "flaky recall >= 90%"),
            (s["flaky precision %"] >= 95, "flaky precision >= 95%"),
            (s["healthy called flaky %"] == 0, "no healthy test called flaky"),
            (s["regressions quarantined as flaky %"] <= 2,
             "regressions called flaky <= 2%"),
            (s["regressions called regressed %"] >= 90,
             "regressions called regressed >= 90%"),
            (s["always-red called broken %"] == 100, "always-red = broken"),
            (rel["stat"]["still-flaky tests told 'release' %"] <= 20,
             "still-flaky tests released <= 20%"),
            (rt["stat"]["retried-green flakes seen in ONE run %"] == 100,
             "retried flakes seen in one run"),
        ]
        bad = [name for ok, name in floors if not ok]
        if bad:
            print("\nFAILED floors: " + "; ".join(bad))
            return 1
        print("\nall floors met")
    return 0


if __name__ == "__main__":
    sys.exit(main())
