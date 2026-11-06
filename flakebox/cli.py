"""Flakebox: self-hosted flaky-test triage for small teams.

Ingests JUnit XML, detects flakiness statistically, quarantines the
flakes, and tells you when they're stable enough to come back.

  flakebox record --junit reports/junit.xml --commit="$SHA"
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
import errno
import json
import os
import sqlite3
import sys

from . import detect as detect_mod
from . import junit as junit_mod
from . import quarantine as quar_mod
from . import safe
from . import store as store_mod

VERSION = "0.5.0"


def default_db() -> str:
    """~/.flakebox/history.db, resolved at call time so HOME/USERPROFILE
    overrides (tests, CI) are honoured."""
    return os.path.join(os.path.expanduser("~"), ".flakebox", "history.db")


def _harden_streams() -> None:
    """Never crash on a console that cannot encode the report's emoji or
    a test id (e.g. cp1252 on Windows): replace instead of raising."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        # Both handlers raise UnicodeEncodeError on a character the code
        # page lacks: "strict" (the default) and "surrogateescape" (what a
        # piped stdout reports on Windows 11 / Python 3.11, with cp1252).
        if reconfigure is None or getattr(stream, "errors", None) not in (
                "strict", "surrogateescape"):
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


def _db(path: str | None):
    return store_mod.connect(path or default_db())


class _NoDatabase(Exception):
    """A command that reads history was pointed at a file that is not there."""


def _existing_db(path: str | None):
    """Open a DB that must already exist. `report`, `quarantine`,
    `history`, `export` and `unquarantine` used to create an empty one at
    a mistyped --db and answer "No flaky tests detected" - a gate that
    passes because it looked in the wrong place. Now that is an error."""
    path = path or default_db()
    if path != ":memory:":
        if not os.path.exists(path):
            raise _NoDatabase(path)
        try:
            # a zero-byte file or another program's SQLite database would
            # be given flakebox's schema and reported as "no runs"
            store_mod.check_existing(path)
        except store_mod.NotFlakeboxDB as e:
            raise _NotFlakebox(path, str(e)) from None
    return store_mod.connect(path)


class _NotFlakebox(Exception):
    """An existing file that a read command will not treat as history."""


def _short(text: str) -> str:
    return text if len(text) <= 30 else text[:27] + "..."


def _int64(text: str) -> int:
    """argparse `type` for an integer that reaches the database. SQLite
    stores 64 bits; binding a bigger Python int is an OverflowError, which
    used to end in a traceback."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid int value: {_short(text)!r}") from None
    low, high = -store_mod._INT64, store_mod._INT64 - 1
    if not low <= value <= high:
        raise argparse.ArgumentTypeError(
            f"{_short(text)} is out of range (allowed: {low} to {high})")
    return value


def _utf8(text: str) -> str:
    """argparse `type` for text that is stored in the database. A byte of
    argv that is not UTF-8 reaches Python as a lone surrogate, which SQLite
    cannot store (a UnicodeEncodeError, and a traceback)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise argparse.ArgumentTypeError(
            "must be valid UTF-8 text (it is stored in the database)") from None
    return text


def _lf_only(stream) -> None:
    """Write "\\n" as "\\n" on `stream`. Text-mode stdout turns it into
    "\\r\\n" on Windows, and Git-Bash's `$(...)` strips only the "\\n", so
    the last word of the deselect line kept a carriage return."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(newline="\n")
        except (OSError, ValueError):
            pass


def _is_cwd(path: str) -> bool:
    """Is `path` the current directory (however it is spelled)?"""
    try:
        return os.path.samefile(path, os.getcwd())
    except OSError:
        return False


def cmd_record(args) -> int:
    try:
        raw = junit_mod.read(args.junit)
        results = junit_mod.parse_bytes(raw, args.junit)
    except FileNotFoundError:
        print(f"error: no such file: {safe.text(args.junit)}",
              file=sys.stderr)
        return 2
    except Exception as e:
        print(f"error: cannot parse {safe.text(args.junit)}: "
              f"{safe.text(e)}", file=sys.stderr)
        return 2
    sha = junit_mod.report_sha256(raw)
    if not results and not args.allow_empty:
        # Nearly always a test step that broke. Recorded, a run with no
        # tests makes every test look absent, and three of them in a row
        # retire the whole suite. Exit 0 all the same: a build that really
        # ran no tests (docs only) is not a failure.
        junit_mod._warn(f"{args.junit} has no test cases, so it was not "
                        f"recorded (a test step that broke looks like this; "
                        f"--allow-empty records it anyway)")
        return 0
    conn = _db(args.db)
    try:
        if not args.force:
            dup = store_mod.find_run(conn, args.commit, sha, args.env)
            if dup is not None:
                print(f"already recorded as run {dup} (same commit and "
                      f"report); nothing added. --force records it again.")
                return 0
        run_id = store_mod.record_run(conn, results, args.commit, args.branch,
                                      report_sha=sha, env=args.env)
    finally:
        conn.close()
    n_fail = sum(1 for r in results if r["outcome"] in ("failed", "error"))
    n_rerun = sum(1 for r in results
                  if r["outcome"] == junit_mod.PASSED_ON_RERUN)
    rerun = (f", {n_rerun} passed only on a retry (flaky)" if n_rerun else "")
    print(f"recorded run {run_id}: {len(results)} tests, "
          f"{n_fail} failed/error{rerun}")
    return 0


def _pct(f: float) -> str:
    return f"{f:.0%}"


def _evidence(s: dict) -> str:
    """Why a flaky test is certainly flaky, in a few words ("-" when the
    verdict rests on the pass/fail counts alone)."""
    parts = []
    if s.get("same_commit_flips"):
        n = s["same_commit_flips"]
        parts.append(f"same commit x{n}" if n > 1 else "same commit")
    if s.get("same_run_flips"):
        n = s["same_run_flips"]
        parts.append(f"passed on retry x{n}" if n > 1 else "passed on retry")
    return ", ".join(parts) or "-"


def _top_failure(s: dict, width: int = 60) -> str:
    modes = s.get("failure_modes") or []
    if not modes:
        return "-"
    msg = modes[0]["message"]
    if len(msg) > width:
        msg = msg[:width - 3] + "..."
    more = f" (+{len(modes) - 1} more)" if len(modes) > 1 else ""
    return safe.md_cell(msg) + more


def _unquarantine_cmd(test_id: str, fallback: str) -> str:
    """The command that releases `test_id`, for a markdown code span. When
    the id cannot be pasted safely (shell_arg() refuses it: a Windows
    shell character, a control character), or would be changed by the
    code span (`|`, a backtick), the `fallback` - a command that needs no
    id - is suggested instead."""
    quoted = safe.shell_arg(test_id)
    if quoted is None or "|" in test_id or "`" in test_id:
        return fallback
    return f"flakebox unquarantine --test {quoted}"


def cmd_report(args) -> int:
    conn = _existing_db(args.db)
    try:
        a = detect_mod.analyze(conn, args.min_runs, args.stable_runs,
                               retire_after=args.retire_after,
                               use_commits=not args.ignore_commits)
        n_runs = store_mod.run_count(conn)
    finally:
        conn.close()
    if args.format == "json":
        print(json.dumps(a, indent=2))
        return 0
    print(f"\n# Flakebox report ({safe.text(args.db or default_db())})\n")
    if not n_runs:
        print("No runs recorded yet: nothing to analyze "
              "(flakebox record --junit FILE adds one).\n")
        return 0
    if a["flaky"]:
        print("## Flaky (pass AND fail - quarantine candidates)")
        print("| test | flake rate | fails/passes | runs | proof | "
              "top failure |")
        print("|---|---|---|---|---|---|")
        for s in a["flaky"]:
            q = " \U0001f512" if s["quarantined"] else ""
            print(f"| {safe.md_code(s['test_id'])}{q} | "
                  f"{_pct(s['flake_rate'])} | "
                  f"{s['failures']}/{s['passes']} | {s['runs']} | "
                  f"{_evidence(s)} | {_top_failure(s)} |")
        print()
        if any(s["proven"] for s in a["flaky"]):
            print("_proof: the test passed in one run and failed in "
                  "another run of the same commit and environment, or "
                  "failed and then passed on the runner's retry in one "
                  "run - flaky whatever the run count._\n")
    else:
        print("No flaky tests detected. \U0001f389\n")
    if a["broken"]:
        print("## Consistently failing (broken, not flaky - fix, don't quarantine)")
        for s in a["broken"]:
            top = _top_failure(s)
            why = f"; {top}" if top != "-" else ""
            print(f"- {safe.md_code(s['test_id'])} ({s['failures']} "
                  f"failures, 0 passes{why})")
        print()
    if a["stable"]:
        print("## Stable - ready to un-quarantine")
        for s in a["stable"]:
            print(f"- {safe.md_code(s['test_id'])} ({s['pass_streak']} "
                  f"consecutive passes)")
            hint = _unquarantine_cmd(
                s["test_id"], f"flakebox unquarantine --all-stable "
                f"--stable-runs {args.stable_runs}")
            print(f"  run: {safe.md_code(hint)}")
        print()
    if a["retired"]:
        print(f"## Retired (absent from the last {args.retire_after} runs - "
              f"deleted or renamed, not counted)")
        for s in a["retired"]:
            q = (" - still quarantined, run: "
                 + safe.md_code(_unquarantine_cmd(
                     s["test_id"], f"flakebox unquarantine --all-retired "
                     f"--retire-after {args.retire_after}"))
                 if s["quarantined"] else "")
            print(f"- {safe.md_code(s['test_id'])} (was {s['status']}; last "
                  f"seen run {s['last_run']}, commit "
                  f"{safe.md_cell(s.get('last_commit') or '-')}){q}")
        print()
    return 0


def cmd_quarantine(args) -> int:
    """Quarantine the current flaky set and emit the WHOLE quarantine set
    in the requested format. Idempotent: running it again adds nothing,
    resets nothing, and still prints the full set (so a CI job can run
    it every build)."""
    conn = _existing_db(args.db)
    try:
        a = detect_mod.analyze(conn, args.min_runs, args.stable_runs,
                               retire_after=args.retire_after,
                               use_commits=not args.ignore_commits)
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
        try:
            written = quar_mod.write_pytest(out_dir, full, mode=args.mode)
        except OSError as e:    # -o is a file, no permission, no such drive
            print(f"error: cannot write the pytest plugin to "
                  f"{safe.text(out_dir)}: {e.strerror or e}", file=sys.stderr)
            return 2
        print(summary)
        for w in written:
            print(f"  wrote {safe.text(w)}")
        # `-p` imports the plugin at start-up. `python -m pytest` puts the
        # current directory on sys.path, the bare `pytest` command does not.
        print("run pytest with: python -m pytest -p flakebox_quarantine")
        if not _is_cwd(out_dir):
            print(f"  ({safe.text(out_dir)} is not the current directory: "
                  f"put it on "
                  f"PYTHONPATH first, or python cannot import the plugin)")
    elif args.format == "deselect":
        # stdout is meant for `eval "pytest $(flakebox quarantine --format
        # deselect)"`: only the (shell-quoted) args go there, the summary
        # goes to stderr
        print(summary, file=sys.stderr)
        _lf_only(sys.stdout)    # no CR at the end of what `$(...)` captures
        print(quar_mod.deselect_line(full))
    else:
        print(json.dumps({"quarantined": full, "new": new}, indent=2))
    return 0


def cmd_unquarantine(args) -> int:
    conn = _existing_db(args.db)
    try:
        if args.test is None:   # --all-stable / --all-retired
            a = detect_mod.analyze(conn, args.min_runs, args.stable_runs,
                                   retire_after=args.retire_after,
                                   use_commits=not args.ignore_commits)
            group = a["stable"] if args.all_stable else [
                s for s in a["retired"] if s["quarantined"]]
            released = [s["test_id"] for s in group
                        if store_mod.quarantine_remove(conn, s["test_id"])]
        else:
            removed = store_mod.quarantine_remove(conn, args.test)
    finally:
        conn.close()
    if args.test is None:
        which = "stable" if args.all_stable else "retired"
        print(f"un-quarantined {len(released)} {which} test(s)")
        for tid in released:
            print(f"  {safe.text(tid)}")
        if released:
            print("regenerate the pytest plugin: "
                  "flakebox quarantine --format pytest")
        return 0
    if not removed:
        print(f"{safe.text(args.test)} is not in quarantine; nothing "
              f"changed (flakebox quarantine --format json lists the set)",
              file=sys.stderr)
        return 0
    print(f"un-quarantined {safe.text(args.test)}")
    print("regenerate the pytest plugin: flakebox quarantine --format pytest")
    return 0


def cmd_history(args) -> int:
    conn = _existing_db(args.db)
    try:
        hist = store_mod.test_history(conn, args.test, limit=args.limit)
        if not hist:
            print(f"no history for {safe.text(args.test)}")
            return 0
        # the same thresholds as `report`, so the two never disagree on
        # what a test is (a recovered test is "recovered", not "flaky")
        s = detect_mod.summarize(conn, args.test,
                                 retire_after=args.retire_after,
                                 stable_runs=args.stable_runs,
                                 use_commits=not args.ignore_commits)
    finally:
        conn.close()
    status = s["status"]
    rate = (f", flake rate {_pct(s['flake_rate'])}"
            if status in (detect_mod.FLAKY, detect_mod.RECOVERED) else "")
    if status == detect_mod.RECOVERED:
        status += f" ({s['pass_streak']} consecutive passes)"
    if s["proven"] and s["status"] == detect_mod.FLAKY:
        status += f" (proof: {_evidence(s)})"
    elif s["runs"] < args.min_runs:
        status += (f" (only {s['runs']} executed runs; report needs "
                   f"{args.min_runs})")
    flags = ("".join([" [quarantined]" if s["quarantined"] else "",
                      " [retired: absent from the newest runs]"
                      if s["retired"] else ""]))
    print(f"\n{safe.text(args.test)}: {status} - {s['passes']} passed, "
          f"{s['failures']} failed over {s['runs']} executed runs "
          f"({s['skips']} skipped{rate}){flags}\n")
    if s["failure_modes"]:
        print("  failure modes (numbers shown as N):")
        for m in s["failure_modes"]:
            print(f"    {m['count']:>3}x {safe.text(m['message'])}")
        print()
    for h in hist[:20]:
        # the outcome is escaped too: an imported DB is someone else's data
        line = f"  {safe.text(h['outcome']):7} {safe.text(h['commit'] or '-')}"
        if h.get("env"):
            line += f" [{safe.text(h['env'])}]"
        if h["outcome"] not in ("passed", "skipped") and h.get("message"):
            msg = next((ln.strip() for ln in h["message"][:4096].splitlines()
                        if ln.strip()), "")
            if len(msg) > 80:
                msg = msg[:77] + "..."
            line += f"  {safe.text(msg)}"
        print(line.rstrip())
    return 0


def cmd_export(args) -> int:
    conn = _existing_db(args.db)
    try:
        try:
            data = store_mod.export_file(conn, args.out)
        except OSError as e:    # a directory, no permission, no such folder
            print(f"error: cannot export to {safe.text(args.out)}: "
                  f"{e.strerror or e}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    print(f"exported {len(data['runs'])} run(s), "
          f"{len(data['quarantine'])} quarantine entr"
          f"{'y' if len(data['quarantine']) == 1 else 'ies'} to "
          f"{safe.text(args.out)}")
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
            except OSError as e:    # a directory, no permission, ...
                print(f"error: cannot import {path}: {e.strerror or e}",
                      file=sys.stderr)
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
    detect.add_argument("--stable-runs", type=int,
                        default=detect_mod.STABLE_RUNS,
                        help="consecutive passes that make a test stable "
                             f"(default {detect_mod.STABLE_RUNS})")
    detect.add_argument("--retire-after", type=_int64,
                        default=detect_mod.RETIRE_AFTER, metavar="N",
                        help="a test absent from the newest N runs is "
                             "retired (deleted/renamed): listed apart, "
                             f"not counted (default {detect_mod.RETIRE_AFTER}; "
                             "0 disables)")
    detect.add_argument("--ignore-commits", action="store_true",
                        help="do not treat a pass and a failure at the same "
                             "commit as proof of flakiness (use it for a DB "
                             "of local runs on an uncommitted worktree)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", parents=[common],
                       help="Ingest a JUnit XML report.")
    r.add_argument("--junit", required=True)
    r.add_argument("--commit", type=_utf8, default=None)
    r.add_argument("--branch", type=_utf8, default=None)
    r.add_argument("--env", type=_utf8, default=None, metavar="NAME",
                   help="the environment this run is from (e.g. "
                        "linux-py3.12). Same-commit proof only compares runs "
                        "of one environment, so a test that passes on Linux "
                        "and fails on Windows at one commit is not called "
                        "flaky for that. Set it on every matrix leg.")
    r.add_argument("--force", action="store_true",
                   help="record even if this exact report (same commit, "
                        "same bytes) was recorded before")
    r.add_argument("--allow-empty", action="store_true",
                   help="record a report that has no test cases. By default "
                        "it is not recorded (a warning on stderr, exit 0): "
                        "it is nearly always a test step that broke, and "
                        "three of them in a row retire every test")

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

    u = sub.add_parser("unquarantine", parents=[common, detect],
                       help="Release a test, or every stable / retired one.")
    which = u.add_mutually_exclusive_group(required=True)
    which.add_argument("--test", type=_utf8, default=None)
    which.add_argument("--all-stable", action="store_true",
                       help="release every quarantined test the report "
                            "lists as stable (same --stable-runs etc.)")
    which.add_argument("--all-retired", action="store_true",
                       help="release every quarantined test the report "
                            "lists as retired (same --retire-after)")

    h = sub.add_parser("history", parents=[common, detect],
                       help="Per-test run history (same thresholds as "
                            "report, so the status word agrees).")
    h.add_argument("--test", required=True, type=_utf8)
    h.add_argument("--limit", type=_int64, default=50)

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


# Tests patch this to exercise the Windows branch on any OS.
_WINDOWS = os.name == "nt"


def _pipe_gone(exc: BaseException) -> bool:
    """Did the reader of our stdout go away?

    POSIX raises BrokenPipeError (EPIPE). Windows reports the same thing as
    a plain OSError with errno EINVAL and no file name (measured on Windows
    11 / Python 3.11: errno 22, winerror None), raised by the FLUSH rather
    than by the print, and it fails a second time at interpreter exit
    (status 120) unless fd 1 is pointed at the null device first."""
    if isinstance(exc, BrokenPipeError):
        return True
    if not isinstance(exc, OSError):
        return False
    if exc.errno == errno.EPIPE:
        return True
    return (_WINDOWS and exc.errno == errno.EINVAL
            and getattr(exc, "filename", None) is None)


def _broken_pipe() -> int:
    """stdout's reader went away (`flakebox report | head`): leave
    quietly. stdout is pointed at the null device first so the
    interpreter's exit-time flush cannot raise the same error again."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass
    return 0


def main(argv=None) -> int:
    _harden_streams()
    args = build_parser().parse_args(argv)
    cmds = {"record": cmd_record, "report": cmd_report,
            "quarantine": cmd_quarantine, "unquarantine": cmd_unquarantine,
            "history": cmd_history, "export": cmd_export,
            "import": cmd_import}
    try:
        rc = cmds[args.cmd](args)
        if sys.stdout is not None:
            sys.stdout.flush()  # a closed pipe surfaces here, not at interpreter exit
        return rc
    except _NotFlakebox as e:
        print(f"error: {safe.text(e.args[0])} is not a flakebox database "
              f"({safe.text(e.args[1])}); check --db", file=sys.stderr)
        return 2
    except _NoDatabase as e:
        print(f"error: no flakebox database at {safe.text(e.args[0])} "
              f"(nothing recorded there: check --db, or run flakebox record "
              f"/ flakebox import first)", file=sys.stderr)
        return 2
    except sqlite3.DatabaseError as e:
        print(f"error: cannot use database "
              f"{safe.text(args.db or default_db())}: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        if _pipe_gone(e):
            return _broken_pipe()
        raise


if __name__ == "__main__":
    sys.exit(main())
