"""Flakebox: self-hosted flaky-test triage for small teams.

Ingests JUnit XML, detects flakiness statistically, quarantines the
flakes, and tells you when they're stable enough to come back.

  flakebox record --junit reports/junit.xml --commit $SHA
  flakebox report                        # flaky / broken / stable tables
  flakebox quarantine --format pytest -o .   # skip flakes in CI
  flakebox unquarantine --test pkg.Class::test_x
  flakebox export --out history.json     # carry the DB (CI artifact)
  flakebox import history.json           # merge it into the local DB

--db works before or after the subcommand:
  flakebox --db ci.db report  ==  flakebox report --db ci.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from . import detect as detect_mod
from . import junit as junit_mod
from . import quarantine as quar_mod
from . import store as store_mod

VERSION = "0.3.0"


def default_db() -> str:
    """~/.flakebox/history.db, resolved at call time so HOME/USERPROFILE
    overrides (tests, CI) are honoured."""
    return os.path.join(os.path.expanduser("~"), ".flakebox", "history.db")


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
        raw = junit_mod.read(args.junit)
        results = junit_mod.parse_bytes(raw, args.junit)
    except FileNotFoundError:
        print(f"error: no such file: {args.junit}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"error: cannot parse {args.junit}: {e}", file=sys.stderr)
        return 2
    sha = junit_mod.report_sha256(raw)
    conn = _db(args.db)
    try:
        if not args.force:
            dup = store_mod.find_run(conn, args.commit, sha)
            if dup is not None:
                print(f"already recorded as run {dup} (same commit and "
                      f"report); nothing added. --force records it again.")
                return 0
        run_id = store_mod.record_run(conn, results, args.commit, args.branch,
                                      report_sha=sha)
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
        a = detect_mod.analyze(conn, args.min_runs, args.stable_runs,
                               retire_after=args.retire_after)
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
    if a["retired"]:
        print(f"## Retired (absent from the last {args.retire_after} runs - "
              f"deleted or renamed, not counted)")
        for s in a["retired"]:
            q = " - still quarantined, run: flakebox unquarantine --test " \
                f"\"{s['test_id']}\"" if s["quarantined"] else ""
            print(f"- `{s['test_id']}` (was {s['status']}; last seen run "
                  f"{s['last_run']}, commit {s.get('last_commit') or '-'}){q}")
        print()
    return 0


def cmd_quarantine(args) -> int:
    """Quarantine the current flaky set and emit the WHOLE quarantine set
    in the requested format. Idempotent: running it again adds nothing,
    resets nothing, and still prints the full set (so a CI job can run
    it every build)."""
    conn = _db(args.db)
    try:
        a = detect_mod.analyze(conn, args.min_runs, args.stable_runs,
                               retire_after=args.retire_after)
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
    flags = ("".join([" [quarantined]" if s["quarantined"] else "",
                      " [retired: absent from the newest runs]"
                      if s["retired"] else ""]))
    print(f"\n{args.test}: {s['status']} - {s['passes']} passed, "
          f"{s['failures']} failed over {s['runs']} executed runs "
          f"({s['skips']} skipped{rate}){flags}\n")
    for h in hist[:20]:
        print(f"  {h['outcome']:7} {h['commit'] or '-'}")
    return 0


def cmd_export(args) -> int:
    conn = _db(args.db)
    try:
        data = store_mod.export_file(conn, args.out)
    finally:
        conn.close()
    print(f"exported {len(data['runs'])} run(s), "
          f"{len(data['quarantine'])} quarantine entr"
          f"{'y' if len(data['quarantine']) == 1 else 'ies'} to {args.out}")
    return 0


def cmd_import(args) -> int:
    conn = _db(args.db)
    try:
        totals = {"runs_added": 0, "runs_skipped": 0, "quarantine_added": 0}
        for path in args.files:
            try:
                counts = store_mod.import_file(conn, path)
            except FileNotFoundError:
                print(f"error: no such file: {path}", file=sys.stderr)
                return 2
            except (ValueError, KeyError, TypeError) as e:
                print(f"error: cannot import {path}: {e}", file=sys.stderr)
                return 2
            for k in totals:
                totals[k] += counts[k]
    finally:
        conn.close()
    print(f"imported {totals['runs_added']} run(s), skipped "
          f"{totals['runs_skipped']} already recorded, "
          f"{totals['quarantine_added']} new quarantine entr"
          f"{'y' if totals['quarantine_added'] == 1 else 'ies'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flakebox", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    p.add_argument("--db", default=None, metavar="PATH",
                   help=f"SQLite DB path (default {default_db()}). "
                        f"Accepted before or after the subcommand.")
    # the same --db on every subcommand (argparse parents); SUPPRESS so a
    # subcommand that does not mention it leaves the top-level value alone
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, metavar="PATH",
                        help="SQLite DB path (same as the top-level --db)")
    detect = argparse.ArgumentParser(add_help=False)
    detect.add_argument("--min-runs", type=int, default=5,
                        help="executed runs (skips excluded) before a test "
                             "can be called flaky or failing (default 5)")
    detect.add_argument("--stable-runs", type=int, default=10,
                        help="consecutive passes that make a test stable "
                             "(default 10)")
    detect.add_argument("--retire-after", type=int,
                        default=detect_mod.RETIRE_AFTER, metavar="N",
                        help="a test absent from the newest N runs is "
                             "retired (deleted/renamed): listed apart, "
                             f"not counted (default {detect_mod.RETIRE_AFTER}; "
                             "0 disables)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", parents=[common],
                       help="Ingest a JUnit XML report.")
    r.add_argument("--junit", required=True)
    r.add_argument("--commit", default=None)
    r.add_argument("--branch", default=None)
    r.add_argument("--force", action="store_true",
                   help="record even if this exact report (same commit, "
                        "same bytes) was recorded before")

    rp = sub.add_parser("report", parents=[common, detect],
                        help="Flaky / broken / stable / retired tables.")
    rp.add_argument("--format", choices=["markdown", "json"],
                    default="markdown")

    q = sub.add_parser("quarantine", parents=[common, detect],
                       help="Quarantine the flaky set.")
    q.add_argument("--format", choices=["pytest", "json", "deselect"],
                   default="pytest")
    q.add_argument("-o", "--out", default=".",
                   help="Output dir for --format pytest.")
    q.add_argument("--mode", choices=list(quar_mod.MODES), default="skip",
                   help="pytest plugin behaviour for quarantined tests: "
                        "skip them (default) or run them as non-strict "
                        "xfail so they keep recording results")

    u = sub.add_parser("unquarantine", parents=[common], help="Release a test.")
    u.add_argument("--test", required=True)

    h = sub.add_parser("history", parents=[common], help="Per-test run history.")
    h.add_argument("--test", required=True)
    h.add_argument("--limit", type=int, default=50)

    e = sub.add_parser("export", parents=[common],
                       help="Write the whole DB (runs, results, quarantine) "
                            "as one JSON file - a CI artifact that "
                            "`flakebox import` merges locally.")
    e.add_argument("--out", required=True, metavar="FILE")

    i = sub.add_parser("import", parents=[common],
                       help="Merge one or more `flakebox export` files into "
                            "the DB; runs already present (same commit and "
                            "report) are skipped.")
    i.add_argument("files", nargs="+", metavar="FILE")
    return p


def main(argv=None) -> int:
    _harden_streams()
    args = build_parser().parse_args(argv)
    cmds = {"record": cmd_record, "report": cmd_report,
            "quarantine": cmd_quarantine, "unquarantine": cmd_unquarantine,
            "history": cmd_history, "export": cmd_export,
            "import": cmd_import}
    try:
        return cmds[args.cmd](args)
    except sqlite3.DatabaseError as e:
        print(f"error: cannot use database {args.db or default_db()}: {e}",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
