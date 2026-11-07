"""Triage: is the failure in THIS report a known flake?

Reads the history, never writes it. For each test that failed in the
report it answers one of:

- known-flake   flaky in the history (proven, or enough runs): the usual
                answer to "just re-run it", with the rate, owner and the
                date it was first seen failing
- broken        already failing before this run (a regression, or never
                passed): re-running will not help
- new-failure   no flaky history: it passed before (or was never seen), so
                this failure is probably real
- weak-history  failed once or twice before, too few runs to call a flake:
                not treated as a known flake
"""

from __future__ import annotations

from . import detect, store

KNOWN, BROKEN, NEW, WEAK = "known-flake", "broken", "new-failure", "weak-history"


def judge(conn, failure: dict, min_runs: int = 5, window: int = 50,
          stable_runs: int = detect.STABLE_RUNS, use_commits: bool = True,
          model: str = "stat", qmap: dict | None = None, **_ignored) -> dict:
    """One failed test of the report -> its verdict and the facts behind it
    (one history query; `qmap` is store.quarantined() keyed by id)."""
    tid = failure["test_id"]
    hist = store.test_history(conn, tid, limit=window)   # newest first
    s = detect.stats([h["outcome"] for h in hist], stable_runs,
                     commits=([(h["commit"], h["env"]) for h in hist]
                              if use_commits else None),
                     messages=[h["message"] for h in hist],
                     run_ids=[h["run_id"] for h in hist], model=model,
                     max_stable=window)
    if qmap is None:
        qmap = {q["test_id"]: q for q in store.quarantined(conn)}
    q = qmap.get(tid)
    fails = [h["at"] for h in hist
             if h["outcome"] in detect.FAIL_OUTCOMES and h["at"] is not None]
    modes = s["failure_modes"]
    base = {"test_id": tid, "message": failure.get("message", ""),
            "runs": s["runs"], "status": s["status"],
            "flake_rate": s["flake_rate"], "rate_low": s["rate_low"],
            "rate_high": s["rate_high"], "proven": s["proven"],
            "quarantined": bool(q) and not store.expired(q),
            "owner": q["owner"] if q else None,
            "quarantine_expires_at": q["expires_at"] if q else None,
            "tracking_since": min(fails) if fails else None,
            "change": s.get("change"),
            "usual_failure": modes[0]["message"] if modes else None,
            "family": modes[0].get("family") if modes else None}
    st = s["status"]
    if st in (detect.FLAKY, detect.RECOVERED) and (
            s["proven"] or s["runs"] >= min_runs):
        return {**base, "verdict": KNOWN}
    if st in (detect.REGRESSED, detect.FAILING):
        return {**base, "verdict": BROKEN}
    if st in (detect.FLAKY, detect.RECOVERED):
        return {**base, "verdict": WEAK}
    return {**base, "verdict": NEW}


def triage(conn, results: list[dict], min_runs: int = 5, window: int = 50,
           **kw) -> dict:
    """{failures: [...judged], retried: [ids that passed only on a
    retry], all_known: bool}. `conn` may be None: no history yet, so every
    failure is new."""
    failed = [r for r in results if r["outcome"] in detect.FAIL_OUTCOMES]
    retried = sorted(r["test_id"] for r in results
                     if r["outcome"] == "passed_on_rerun")
    out = []
    qmap = ({q["test_id"]: q for q in store.quarantined(conn)}
            if conn is not None else {})
    for f in failed:
        if conn is None:
            out.append({"test_id": f["test_id"], "message": f["message"],
                        "verdict": NEW, "runs": 0, "status": "unknown",
                        "flake_rate": 0.0, "rate_low": 0.0, "rate_high": 1.0,
                        "proven": False, "quarantined": False, "owner": None,
                        "quarantine_expires_at": None,
                        "tracking_since": None, "change": None,
                        "usual_failure": None, "family": None})
        else:
            out.append(judge(conn, f, min_runs=min_runs, window=window,
                             qmap=qmap, **kw))
    return {"failures": out, "retried": retried,
            "all_known": bool(out) and all(f["verdict"] == KNOWN
                                           for f in out)}
