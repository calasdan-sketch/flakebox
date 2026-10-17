"""Flakebox: self-hosted flaky-test triage for small teams.

Ingests JUnit XML, detects flakiness statistically, quarantines the
flakes, and tells you when they're stable enough to come back.

  flakebox record --junit reports/junit.xml --commit $SHA
  flakebox report                        # flaky / broken / stable tables
  flakebox quarantine --format pytest -o .   # skip flakes in CI
  flakebox unquarantine --test pkg.Class::test_x
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import detect as detect_mod
from . import junit as junit_mod
from . import quarantine as quar_mod
from . import store as store_mod

VERSION = "0.2.0"


def default_db() -> str:
    """~/.flakebox/history.db, resolved at call time so HOME/USERPROFILE
    overrides (tests, CI) are honoured."""
    return os.path.join(os.path.expanduser("~"), ".flakebox", "history.db")


DEFAULT_DB = default_db()  # kept for callers that import the constant


def _harden_streams() -> None:
    """Never crash on a console that cannot encode the report's emoji or
    a test id (e.g. cp1252 on Windows): replace instead of raising."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None or getattr(stream, "errors", None) != "strict":
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


def _db(path: str | None):
    return store_mod.connect(path or default_db())


def cmd_record(args) -> int:
    try:
        results = junit_mod.parse(args.junit)
    except FileNotFoundError:
        print(f"error: no such file: {args.junit}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"error: cannot parse {args.junit}: {e}", file=sys.stderr)
        return 2
    conn = _db(args.db)
    try:
        run_id = store_mod.record_run(conn, results, args.commit, args.branch)
    finally:
        conn.close()
    n_fail = sum(1 for r in results if r["outcome"] in ("failed", "error"))
    print(f"recorded run {run_id}: {len(results)} tests, "
          f"{n_fail} failed/error")
    return 0


def _pct(f: float) -> str:
    return f"{f:.0%}"


def cmd_report(args) -> int:
    conn = _db(args.db)
    try:
        a = detect_mod.analyze(conn, args.min_runs, args.stable_runs)
    finally:
        conn.close()
    if args.format == "json":
        print(json.dumps(a, indent=2))
        return 0
    print(f"\n# Flakebox report ({args.db or default_db()})\n")
    if a["flaky"]:
        print("## Flaky (pass AND fail - quarantine candidates)")
        print("| test | flake rate | fails/passes | runs |")
        print("|---|---|---|---|")
        for s in a["flaky"]:
            q = " \U0001f512" if s["quarantined"] else ""
            print(f"| `{s['test_id']}` | {_pct(s['flake_rate'])} | "
                  f"{s['failures']}/{s['passes']} | {s['runs']} |{q}")
        print()
    else:
        print("No flaky tests detected. \U0001f389\n")
    if a["broken"]:
        print("## Consistently failing (broken, not flaky - fix, don't quarantine)")
        for s in a["broken"]:
            print(f"- `{s['test_id']}` ({s['failures']} failures, "
                  f"0 passes)")
        print()
    if a["stable"]:
        print("## Stable - ready to un-quarantine")
        for s in a["stable"]:
            print(f"- `{s['test_id']}` ({s['pass_streak']} consecutive passes)")
            print(f"  run: flakebox unquarantine --test \"{s['test_id']}\"")
        print()
    return 0


def cmd_quarantine(args) -> int:
    """Quarantine the current flaky set and emit the WHOLE quarantine set
    in the requested format. Idempotent: running it again adds nothing,
    resets nothing, and still prints the full set (so a CI job can run
    it every build)."""
    conn = _db(args.db)
    try:
        a = detect_mod.analyze(conn, args.min_runs, args.stable_runs)
        new = [s["test_id"] for s in a["flaky"]
               if store_mod.quarantine_add(conn, s["test_id"],
                                           reason="auto: flaky")]
        full = sorted(q["test_id"] for q in store_mod.quarantined(conn))
    finally:
        conn.close()
    summary = (f"quarantined {len(new)} new test(s); "
               f"{len(full)} in quarantine.")
    if args.format == "pytest":
        out_dir = args.out or "."
        written = quar_mod.write_pytest(out_dir, full, mode=args.mode)
        print(summary)
        for w in written:
            print(f"  wrote {w}")
        print("run pytest with: pytest -p flakebox_quarantine")
    elif args.format == "deselect":
        # stdout is meant for `pytest $(flakebox quarantine --format
        # deselect)`: only the args go there, the summary goes to stderr
        print(summary, file=sys.stderr)
        print(" ".join(quar_mod.deselect_args(full)))
    else:
        print(json.dumps({"quarantined": full, "new": new}, indent=2))
    return 0


def cmd_unquarantine(args) -> int:
    conn = _db(args.db)
    try:
        store_mod.quarantine_remove(conn, args.test)
    finally:
        conn.close()
    print(f"un-quarantined {args.test}")
    print("regenerate the pytest plugin: flakebox quarantine --format pytest")
    return 0


def cmd_history(args) -> int:
    conn = _db(args.db)
    try:
        hist = store_mod.test_history(conn, args.test, limit=args.limit)
        if not hist:
            print(f"no history for {args.test}")
            return 0
        s = detect_mod.summarize(conn, args.test)
    finally:
        conn.close()
    rate = (f", flake rate {_pct(s['flake_rate'])}"
            if s["status"] == detect_mod.FLAKY else "")
    print(f"\n{args.test}: {s['status']} - {s['passes']} passed, "
          f"{s['failures']} failed over {s['runs']} executed runs "
          f"({s['skips']} skipped{rate})"
          f"{' [quarantined]' if s['quarantined'] else ''}\n")
    for h in hist[:20]:
        print(f"  {h['outcome']:7} {h['commit'] or '-'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="flakebox", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    p.add_argument("--db", default=None,
                   help=f"SQLite DB path (default {default_db()}).")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="Ingest a JUnit XML report.")
    r.add_argument("--junit", required=True)
    r.add_argument("--commit", default=None)
    r.add_argument("--branch", default=None)

    rp = sub.add_parser("report", help="Flaky / broken / stable tables.")
    rp.add_argument("--format", choices=["markdown", "json"],
                    default="markdown")
    rp.add_argument("--min-runs", type=int, default=5,
                    help="executed runs (skips excluded) before a test "
                         "can be called flaky or failing (default 5)")
    rp.add_argument("--stable-runs", type=int, default=10,
                    help="consecutive passes that make a test stable "
                         "(default 10)")

    q = sub.add_parser("quarantine", help="Quarantine the flaky set.")
    q.add_argument("--format", choices=["pytest", "json", "deselect"],
                   default="pytest")
    q.add_argument("-o", "--out", default=".",
                   help="Output dir for --format pytest.")
    q.add_argument("--mode", choices=list(quar_mod.MODES), default="skip",
                   help="pytest plugin behaviour for quarantined tests: "
                        "skip them (default) or run them as non-strict "
                        "xfail so they keep recording results")
    q.add_argument("--min-runs", type=int, default=5)
    q.add_argument("--stable-runs", type=int, default=10)

    u = sub.add_parser("unquarantine", help="Release a test.")
    u.add_argument("--test", required=True)

    h = sub.add_parser("history", help="Per-test run history.")
    h.add_argument("--test", required=True)
    h.add_argument("--limit", type=int, default=50)
    return p


def main(argv=None) -> int:
    _harden_streams()
    args = build_parser().parse_args(argv)
    cmds = {"record": cmd_record, "report": cmd_report,
            "quarantine": cmd_quarantine, "unquarantine": cmd_unquarantine,
            "history": cmd_history}
    return cmds[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
