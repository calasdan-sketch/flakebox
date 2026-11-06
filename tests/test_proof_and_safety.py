"""0.5.0: proof of flakiness (same commit, passed on retry), failure modes,
safe printing of untrusted text, and read commands that refuse a missing DB.

Synthetic data only; no network. Reuses the isolation of test_flakebox
(HOME and friends point at a throwaway directory)."""

import json
import os
import shlex
import time
import unittest
from unittest import mock

from flakebox import cli as cli_mod
from flakebox import detect as detect_mod
from flakebox import junit as junit_mod
from flakebox import safe as safe_mod
from flakebox import store as store_mod

from tests.test_flakebox import (  # noqa: F401  (module fixtures)
    empty_db, equivalence_history, mktemp, reference_analyze, result,
    run_cli, setUpModule, tearDownModule)


def res(tid, outcome, message=""):
    r = result(tid, outcome)
    r["message"] = message
    return r


def db_with(runs, name="p.db"):
    """runs: [(commit, [result, ...])] or [(commit, env, [result, ...])]
    oldest first, one second apart."""
    db = os.path.join(mktemp(), name)
    conn = store_mod.connect(db)
    t0 = 1_700_000_000
    for i, run in enumerate(runs):
        commit, env, rs = run if len(run) == 3 else (run[0], None, run[1])
        store_mod.record_run(conn, rs, commit_sha=commit, created_at=t0 + i,
                             env=env)
    conn.close()
    return db


SUREFIRE_FLAKY = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="s" tests="4">
  <testcase classname="com.acme.ATest" name="flakyF" time="0.1">
    <flakyFailure message="expected: 1 but was: 2" type="AssertionError">
      <stackTrace>trace</stackTrace>
    </flakyFailure>
  </testcase>
  <testcase classname="com.acme.ATest" name="flakyE" time="0.1">
    <flakyError type="java.net.SocketTimeoutException">Read timed out after 3000 ms
more lines</flakyError>
  </testcase>
  <testcase classname="com.acme.ATest" name="allFail" time="0.1">
    <failure message="boom" type="AssertionError">first</failure>
    <rerunFailure message="boom again" type="AssertionError"/>
  </testcase>
  <testcase classname="com.acme.ATest" name="ok" time="0.1"/>
</testsuite>"""


class TestSurefireReruns(unittest.TestCase):
    def test_flaky_elements_are_passed_on_rerun(self):
        by_id = {r["test_id"]: r for r in
                 junit_mod.parse_bytes(SUREFIRE_FLAKY.encode(), "s.xml")}
        self.assertEqual(by_id["com.acme.ATest::flakyF"]["outcome"],
                         "passed_on_rerun")
        self.assertEqual(by_id["com.acme.ATest::flakyF"]["message"],
                         "expected: 1 but was: 2")
        # no message attribute: the first line of the element's text
        self.assertEqual(by_id["com.acme.ATest::flakyE"]["outcome"],
                         "passed_on_rerun")
        self.assertEqual(by_id["com.acme.ATest::flakyE"]["message"],
                         "Read timed out after 3000 ms")
        # failed in every rerun: still a plain failure
        self.assertEqual(by_id["com.acme.ATest::allFail"]["outcome"], "failed")
        self.assertEqual(by_id["com.acme.ATest::ok"]["outcome"], "passed")

    def test_one_rerun_flake_is_reported_as_flaky_at_once(self):
        tmp = mktemp()
        p = os.path.join(tmp, "s.xml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(SUREFIRE_FLAKY)
        db = os.path.join(tmp, "h.db")
        code, out, _ = run_cli("--db", db, "record", "--junit", p,
                               "--commit=c1")
        self.assertEqual(code, 0)
        self.assertIn("2 passed only on a retry (flaky)", out)
        code, out, _ = run_cli("--db", db, "report", "--format", "json")
        flaky = {s["test_id"]: s for s in json.loads(out)["flaky"]}
        self.assertEqual(sorted(flaky), ["com.acme.ATest::flakyE",
                                         "com.acme.ATest::flakyF"])
        s = flaky["com.acme.ATest::flakyF"]
        # one run that ended green: a pass, not a failure in the rate, and
        # one proof (a retry), not two
        self.assertEqual((s["runs"], s["passes"], s["failures"],
                          s["flake_rate"], s["same_run_flips"],
                          s["same_commit_flips"], s["proven"]),
                         (1, 1, 0, 0.0, 1, 0, True))
        # the never-passing one is not "failing" yet: one run < min-runs
        self.assertEqual(json.loads(out)["broken"], [])

    def test_rerun_outcome_survives_export_and_import(self):
        db = db_with([("c1", [res("t::x", "passed_on_rerun", "flap")])])
        tmp = mktemp()
        exp = os.path.join(tmp, "e.json")
        self.assertEqual(run_cli("--db", db, "export", "--out", exp)[0], 0)
        db2 = os.path.join(tmp, "b.db")
        self.assertEqual(run_cli("--db", db2, "import", exp)[0], 0)
        code, out, _ = run_cli("--db", db2, "report", "--format", "json")
        self.assertEqual(json.loads(out)["flaky"][0]["same_run_flips"], 1)


class TestSameCommitProof(unittest.TestCase):
    def test_pass_and_fail_at_one_commit_is_flaky_below_min_runs(self):
        db = db_with([("c1", [res("t::x", "failed", "boom")]),
                      ("c1", [res("t::x", "passed")])])
        conn = store_mod.connect(db)
        try:
            a = detect_mod.analyze(conn, min_runs=5)
            self.assertEqual([s["test_id"] for s in a["flaky"]], ["t::x"])
            s = a["flaky"][0]
            self.assertEqual((s["same_commit_flips"], s["proven"]), (1, True))
            # --ignore-commits: back to needing min-runs
            off = detect_mod.analyze(conn, min_runs=5, use_commits=False)
            self.assertEqual(off["flaky"], [])
        finally:
            conn.close()

    def test_different_commits_and_missing_commits_prove_nothing(self):
        db = db_with([("c1", [res("t::x", "failed")]),
                      ("c2", [res("t::x", "passed")]),
                      (None, [res("t::x", "failed")]),
                      (None, [res("t::x", "passed")])])
        conn = store_mod.connect(db)
        try:
            a = detect_mod.analyze(conn, min_runs=5)
            self.assertEqual(a["flaky"], [])
            s = detect_mod.summarize(conn, "t::x")
            self.assertEqual((s["same_commit_flips"], s["proven"]), (0, False))
        finally:
            conn.close()

    def test_a_recovered_test_stays_recovered_even_with_proof(self):
        runs = [("c1", [res("t::x", "failed")]), ("c1", [res("t::x", "passed")])]
        runs += [(f"n{i}", [res("t::x", "passed")]) for i in range(10)]
        conn = store_mod.connect(db_with(runs))
        try:
            a = detect_mod.analyze(conn, min_runs=5)
            self.assertEqual(a["flaky"], [])
            self.assertEqual(detect_mod.summarize(conn, "t::x")["status"],
                             "recovered")
        finally:
            conn.close()

    def test_proven_flakes_rank_first(self):
        runs = []
        for i in range(6):   # t::loud flakes 50% on distinct commits
            runs.append((f"d{i}", [res("t::loud", "failed" if i % 2 else
                                       "passed")]))
        runs.append(("c9", [res("t::quiet", "failed")]))
        runs.append(("c9", [res("t::quiet", "passed")]))
        for i in range(4):
            runs.append((f"e{i}", [res("t::quiet", "passed"),
                                   res("t::loud", "failed")]))
        conn = store_mod.connect(db_with(runs))
        try:
            ids = [s["test_id"] for s in detect_mod.analyze(conn)["flaky"]]
        finally:
            conn.close()
        self.assertEqual(ids, ["t::quiet", "t::loud"])

    def test_history_names_the_proof_and_ignore_commits_drops_it(self):
        db = db_with([("c1", [res("t::x", "failed", "boom")]),
                      ("c1", [res("t::x", "passed")])])
        _, out, _ = run_cli("--db", db, "history", "--test", "t::x")
        self.assertIn("flaky (proof: same commit)", out)
        _, out, _ = run_cli("--db", db, "history", "--test", "t::x",
                            "--ignore-commits")
        self.assertIn("only 2 executed runs", out)


class TestBatchedEqualsOracleWithProof(unittest.TestCase):
    """The shared fixture re-runs commits and has rerun flakes; the batched
    analyze() must still equal the independent oracle, both ways."""

    def test_equal_with_and_without_commits(self):
        db = os.path.join(mktemp(), "eq.db")
        conn = store_mod.connect(db)
        try:
            for commit, rs in equivalence_history(shared_commits=True):
                store_mod.record_run(conn, rs, commit_sha=commit)
            kw = dict(min_runs=5, stable_runs=5, window=10)
            a = detect_mod.analyze(conn, **kw)
            self.assertEqual(a, reference_analyze(conn, **kw))
            self.assertTrue(any(s["same_commit_flips"] for s in a["flaky"]))
            self.assertTrue(any(s["same_run_flips"] for s in a["flaky"]))
            self.assertTrue(any(s["failure_modes"] for s in a["flaky"]))
            off = detect_mod.analyze(conn, use_commits=False, **kw)
            self.assertEqual(off, reference_analyze(conn, use_commits=False,
                                                    **kw))
            self.assertFalse(any(s["same_commit_flips"] for s in off["flaky"]))
        finally:
            conn.close()


class TestFailureModes(unittest.TestCase):
    def test_signature_folds_numbers_addresses_and_space(self):
        sig = detect_mod.failure_signature
        self.assertEqual(sig("timeout after 30.2s"), sig("timeout after 31,7s"))
        self.assertEqual(sig("obj at 0x7f3a2b  leaked"), "obj at N leaked")
        self.assertEqual(sig("\n\n  first line\nsecond"), "first line")
        self.assertEqual(sig(""), "(no message)")
        self.assertEqual(sig(None), "(no message)")

    def test_huge_message_is_capped_and_fast(self):
        start = time.monotonic()
        sig = detect_mod.failure_signature("x1 " * 400_000)
        self.assertLess(time.monotonic() - start, 5)
        self.assertLessEqual(len(sig), 160)

    def test_modes_are_counted_and_ranked(self):
        runs = [(f"c{i}", [res("t::x", "failed",
                               f"timeout after {i}s" if i < 4 else "refused")])
                for i in range(6)]
        runs += [(f"p{i}", [res("t::x", "passed")]) for i in range(2)]
        conn = store_mod.connect(db_with(runs))
        try:
            s = detect_mod.analyze(conn)["flaky"][0]
        finally:
            conn.close()
        self.assertEqual(s["failure_modes"],
                         [{"message": "timeout after Ns", "count": 4,
                           "example": "timeout after 3s"},
                          {"message": "refused", "count": 2,
                           "example": "refused"}])

    def test_report_and_history_show_the_top_failure(self):
        runs = [(f"c{i}", [res("t::x", "failed" if i % 2 else "passed",
                               "Connection refused: port 5432")])
                for i in range(6)]
        db = db_with(runs)
        _, out, _ = run_cli("--db", db, "report")
        self.assertIn("Connection refused: port N", out)
        _, out, _ = run_cli("--db", db, "history", "--test", "t::x")
        self.assertIn("3x Connection refused: port N", out)
        self.assertIn("failed  c5  Connection refused: port 5432", out)


ESC = "\x1b"
RLO = "‮"
ZWSP = "​"


class TestSafeText(unittest.TestCase):
    def test_controls_bidi_and_zero_width_are_escaped(self):
        s = safe_mod.text(f"a{ESC}[2Jb{RLO}c{ZWSP}d\ne\x00\x7f\x85")
        for bad in (ESC, RLO, ZWSP, "\n", "\x00", "\x7f", "\x85"):
            self.assertNotIn(bad, s)
        self.assertEqual(s, "a\\x1b[2Jb\\u202ec\\u200bd\\x0ae\\x00\\x7f\\x85")
        self.assertEqual(safe_mod.text("plain é ü 日本"), "plain é ü 日本")

    def test_lone_surrogate_is_escaped(self):
        self.assertEqual(safe_mod.text("x\udcff"), "x\\udcff")
        safe_mod.text("x\udcff").encode("utf-8")

    def test_md_code_survives_pipes_and_backticks(self):
        self.assertEqual(safe_mod.md_code("t[a|b]"), "`t[a\\|b]`")
        self.assertEqual(safe_mod.md_code("a`b"), "`` a`b ``")
        self.assertEqual(safe_mod.md_code("``x"), "``` ``x ```")

    def test_md_cell_escapes_markup(self):
        self.assertEqual(safe_mod.md_cell("<img src=x>|*"),
                         "\\<img src=x\\>\\|\\*")

    @unittest.skipIf(os.name == "nt", "POSIX quoting")
    def test_shell_arg_round_trips_hostile_ids(self):
        for tid in ['t::x"$(touch pwned)"', "t::a b", "t::'q'", "t::`id`",
                    "t::$HOME;rm -rf ~"]:
            self.assertEqual(shlex.split(safe_mod.shell_arg(tid)), [tid])

    def test_report_escapes_hostile_ids_and_messages(self):
        tid = f"t::x{ESC}]0;owned{ESC}\\|{RLO}evil"
        runs = [(f"c{i}", [res(tid, "failed" if i % 2 else "passed",
                               f"msg{ESC}[31m red{RLO}")]) for i in range(6)]
        db = db_with(runs)
        for args in (["report"], ["history", "--test", tid]):
            code, out, err = run_cli("--db", db, *args)
            self.assertEqual(code, 0, err)
            for bad in (ESC, RLO):
                self.assertNotIn(bad, out, args)
        _, out, _ = run_cli("--db", db, "report")
        row = next(ln for ln in out.splitlines() if ln.startswith("| `t::"))
        # the id's pipe is escaped: the row still has exactly 7 cell borders
        self.assertEqual(row.replace("\\|", "").count("|"), 7, row)

    def test_suggested_unquarantine_command_is_shell_quoted(self):
        tid = 't::x"$(touch pwned)"'
        runs = [(f"c{i}", [res(tid, "passed"), res("t::y", "passed")])
                for i in range(12)]
        db = db_with(runs)
        conn = store_mod.connect(db)
        store_mod.quarantine_add(conn, tid)
        conn.close()
        _, out, _ = run_cli("--db", db, "report")
        line = next(ln for ln in out.splitlines() if "run: " in ln)
        cmd = line.split("run: ", 1)[1].strip("`")
        self.assertEqual(cmd, "flakebox unquarantine --test "
                              + safe_mod.shell_arg(tid))
        if os.name != "nt":
            self.assertEqual(shlex.split(cmd)[-1], tid)


class TestWarningsAndErrorsAreEscaped(unittest.TestCase):
    def test_bad_time_warning_escapes_the_test_id(self):
        # XML 1.0 forbids ESC even as &#27;, but bidi controls are legal
        xml = (f'<testsuite><testcase classname="c" name="x{RLO}{ZWSP}" '
               f'time="fast"/></testsuite>').encode()
        seen = []
        junit_mod.parse_bytes(xml, "r.xml", warn=seen.append)
        self.assertTrue(seen)   # the raw message goes to the warn hook ...
        import io
        from contextlib import redirect_stderr
        err = io.StringIO()
        with redirect_stderr(err):
            junit_mod._warn(seen[0])   # ... and is escaped when printed
        self.assertNotIn(RLO, err.getvalue())
        self.assertNotIn(ZWSP, err.getvalue())
        self.assertIn("\\u202e\\u200b", err.getvalue())

    def test_history_shows_the_first_non_blank_line(self):
        db = db_with([(f"c{i}", [res("t::x", "failed" if i % 2 else "passed",
                                     "\n\n  real reason\nmore")])
                      for i in range(6)])
        _, out, _ = run_cli("--db", db, "history", "--test", "t::x")
        self.assertIn("failed  c5  real reason", out)


class TestMissingDatabase(unittest.TestCase):
    READERS = [["report"], ["report", "--format", "json"],
               ["quarantine", "--format", "json"], ["history", "--test", "t::x"],
               ["unquarantine", "--test", "t::x"]]

    def test_read_commands_refuse_a_missing_db_and_create_nothing(self):
        tmp = mktemp()
        db = os.path.join(tmp, "typo.db")
        for args in self.READERS + [["export", "--out",
                                     os.path.join(tmp, "e.json")]]:
            code, out, err = run_cli("--db", db, *args)
            self.assertEqual(code, 2, args)
            self.assertEqual(out, "", args)
            self.assertTrue(err.startswith("error: no flakebox database at "),
                            err)
            self.assertEqual(len(err.splitlines()), 1)
            self.assertFalse(os.path.exists(db), args)

    def test_writers_still_create_it(self):
        tmp = mktemp()
        exp = os.path.join(tmp, "e.json")
        self.assertEqual(run_cli("--db", empty_db(os.path.join(tmp, "a.db")),
                                 "export", "--out", exp)[0], 0)
        db = os.path.join(tmp, "new.db")
        self.assertEqual(run_cli("--db", db, "import", exp)[0], 0)
        self.assertTrue(os.path.exists(db))

    def test_an_empty_db_says_nothing_was_recorded(self):
        db = empty_db(os.path.join(mktemp(), "e.db"))
        code, out, _ = run_cli("--db", db, "report")
        self.assertEqual(code, 0)
        self.assertIn("No runs recorded yet", out)
        self.assertNotIn("No flaky tests detected", out)

    def test_unquarantine_of_an_unknown_test_says_so(self):
        db = empty_db(os.path.join(mktemp(), "e.db"))
        code, out, err = run_cli("--db", db, "unquarantine", "--test", "t::x")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("is not in quarantine; nothing changed", err)


class TestPanelReview(unittest.TestCase):
    """Regression tests for the eight findings of the synapse-1 panel."""

    # 1. the Windows hint must never be pasteable code
    def test_windows_quoting_refuses_shell_characters(self):
        for tid in ["t::test[a&calc]", "t::test[%PATH%]", "t::$(id)",
                    't::x"y', "t::a!b", "t::`b`", "t::a\\", "t::a^b|c",
                    f"t::a{ESC}"]:
            self.assertIsNone(safe_mod.shell_arg(tid, windows=True), tid)
        self.assertEqual(safe_mod.shell_arg("t.m::test_x[1-2 a,b]",
                                            windows=True),
                         '"t.m::test_x[1-2 a,b]"')

    def test_windows_report_hint_falls_back_to_a_command_without_the_id(self):
        tid = "t::test[a&calc]"
        runs = [(f"c{i}", [res(tid, "passed")]) for i in range(12)]
        db = db_with(runs)
        conn = store_mod.connect(db)
        store_mod.quarantine_add(conn, tid)
        conn.close()
        with mock.patch.object(safe_mod, "_WINDOWS", True):
            _, out, _ = run_cli("--db", db, "report")
        hint = next(ln for ln in out.splitlines() if "run: " in ln)
        self.assertNotIn("calc", hint)
        self.assertIn("flakebox unquarantine --all-stable --stable-runs 10",
                      hint)
        code, out, _ = run_cli("--db", db, "unquarantine", "--all-stable")
        self.assertEqual(code, 0)
        self.assertIn("un-quarantined 1 stable test(s)", out)
        conn = store_mod.connect(db)
        self.assertFalse(store_mod.is_quarantined(conn, tid))
        conn.close()

    def test_all_retired_releases_only_retired_quarantined_tests(self):
        runs = [(f"c{i}", [res("t::gone", "failed" if i % 2 else "passed"),
                           res("t::live", "passed")]) for i in range(6)]
        runs += [(f"d{i}", [res("t::live", "passed")]) for i in range(3)]
        db = db_with(runs)
        conn = store_mod.connect(db)
        store_mod.quarantine_add(conn, "t::gone")
        store_mod.quarantine_add(conn, "t::live")
        conn.close()
        _, out, _ = run_cli("--db", db, "unquarantine", "--all-retired")
        self.assertIn("un-quarantined 1 retired test(s)", out)
        conn = store_mod.connect(db)
        self.assertEqual([q["test_id"] for q in store_mod.quarantined(conn)],
                         ["t::live"])
        conn.close()

    # 2. history escapes the outcome, and import refuses unknown outcomes
    def test_import_refuses_an_unknown_outcome(self):
        tmp = mktemp()
        p = os.path.join(tmp, "evil.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"flakebox_export": 1, "runs": [{
                "commit": "c", "results": [
                    ["t::a", f"{ESC}]0;pwned\x07{ESC}[2Jfailed", 1, ""]]}]}, f)
        code, out, err = run_cli("--db", os.path.join(tmp, "d.db"),
                                 "import", p)
        self.assertEqual(code, 2)
        self.assertIn("(outcome) is not one of", err)
        self.assertNotIn(ESC, err)

    def test_history_escapes_a_stored_outcome(self):
        db = db_with([("c1", [res("t::a", "passed")])])
        conn = store_mod.connect(db)
        conn.execute("UPDATE results SET outcome = ?",
                     (f"{ESC}[2Jfailed{RLO}",))
        conn.commit()
        conn.close()
        _, out, _ = run_cli("--db", db, "history", "--test", "t::a")
        self.assertNotIn(ESC, out)
        self.assertNotIn(RLO, out)
        self.assertIn("\\x1b[2Jfailed", out)

    # 3. same-commit proof: per environment, never within one run
    def test_a_platform_failure_is_not_proof_when_env_differs(self):
        db = db_with([("abc", "linux", [res("t::paths", "passed")]),
                      ("abc", "windows", [res("t::paths", "failed")])])
        conn = store_mod.connect(db)
        try:
            self.assertEqual(detect_mod.analyze(conn)["flaky"], [])
            s = detect_mod.summarize(conn, "t::paths")
            self.assertEqual(s["same_commit_flips"], 0)
        finally:
            conn.close()
        # the same environment twice is proof
        db = db_with([("abc", "linux", [res("t::paths", "passed")]),
                      ("abc", "linux", [res("t::paths", "failed")])])
        conn = store_mod.connect(db)
        try:
            self.assertEqual(len(detect_mod.analyze(conn)["flaky"]), 1)
        finally:
            conn.close()

    def test_duplicate_ids_in_one_run_prove_nothing(self):
        db = db_with([("abc", [res("Suite::renders", "passed"),
                               res("Suite::renders", "failed")])])
        conn = store_mod.connect(db)
        try:
            self.assertEqual(detect_mod.analyze(conn)["flaky"], [])
        finally:
            conn.close()

    def test_record_env_flag_and_matrix_import(self):
        tmp = mktemp()
        ok = os.path.join(tmp, "ok.xml")
        bad = os.path.join(tmp, "bad.xml")
        with open(ok, "w", encoding="utf-8") as f:
            f.write('<testsuite><testcase classname="t" name="p"/></testsuite>')
        with open(bad, "w", encoding="utf-8") as f:
            f.write('<testsuite><testcase classname="t" name="p">'
                    '<failure message="x"/></testcase></testsuite>')
        a, b, agg = (os.path.join(tmp, n) for n in ("a.db", "b.db", "m.db"))
        run_cli("--db", a, "record", "--junit", ok, "--commit=abc",
                "--env=linux")
        run_cli("--db", b, "record", "--junit", bad, "--commit=abc",
                "--env=windows")
        for db in (a, b):
            run_cli("--db", db, "export", "--out", db + ".json")
        run_cli("--db", agg, "import", a + ".json", b + ".json")
        code, out, _ = run_cli("--db", agg, "quarantine", "--format", "json")
        self.assertEqual(json.loads(out)["quarantined"], [])
        _, out, _ = run_cli("--db", agg, "history", "--test", "t::p")
        self.assertIn("[windows]", out)

    def test_same_report_in_two_environments_is_two_runs(self):
        tmp = mktemp()
        p = os.path.join(tmp, "r.xml")
        with open(p, "w", encoding="utf-8") as f:
            f.write('<testsuite><testcase classname="t" name="p"/></testsuite>')
        db = os.path.join(tmp, "h.db")
        run_cli("--db", db, "record", "--junit", p, "--commit=c", "--env=a")
        _, out, _ = run_cli("--db", db, "record", "--junit", p, "--commit=c",
                            "--env=b")
        self.assertIn("recorded run 2", out)
        _, out, _ = run_cli("--db", db, "record", "--junit", p, "--commit=c",
                            "--env=b")
        self.assertIn("already recorded as run 2", out)

    def test_action_passes_an_env_to_record(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "action.yml"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn('--env="${FB_ENV:-$FB_DB_KEY-$FB_RUNNER_OS}"', text)
        self.assertIn("  env:\n    description:", text)

    # 4. the normaliser keeps the numbers that tell failures apart
    def test_signature_keeps_status_codes_and_identifiers(self):
        sig = detect_mod.failure_signature
        self.assertNotEqual(sig("assert 404 == 200"), sig("assert 500 == 200"))
        self.assertNotEqual(sig("HTTP 503"), sig("HTTP 401"))
        self.assertNotEqual(sig("KeyError: user_1"), sig("KeyError: user_2"))
        self.assertNotEqual(sig("test_py39 broke"), sig("test_py312 broke"))
        self.assertEqual(sig("Read timed out after 3000 ms"),
                         sig("Read timed out after 2950 ms"))
        self.assertEqual(sig("refused: localhost:5432"),
                         sig("refused: localhost:6543"))
        self.assertEqual(sig("job 123e4567-e89b-12d3-a456-426614174000 lost"),
                         "job N lost")
        self.assertEqual(sig("request 1234567 failed"), "request N failed")

    # 5. one retry run is one proof, ends green, and is not a red run
    def test_a_single_retry_run_is_one_proof_and_zero_rate(self):
        conn = store_mod.connect(db_with(
            [("c1", [res("t::r", "passed_on_rerun", "flap")])]))
        try:
            s = detect_mod.analyze(conn)["flaky"][0]
        finally:
            conn.close()
        self.assertEqual((s["same_run_flips"], s["same_commit_flips"],
                          s["failures"], s["passes"], s["flake_rate"]),
                         (1, 0, 0, 1, 0.0))
        _, out, _ = run_cli("--db", db_with(
            [("c1", [res("t::r", "passed_on_rerun", "flap")])]), "report")
        row = next(ln for ln in out.splitlines() if ln.startswith("| `t::r"))
        self.assertIn("| passed on retry |", row)
        self.assertNotIn("same commit", row)

    # 6. an id with `|` gets a hint that works
    def test_pipe_id_hint_needs_no_id(self):
        tid = "t::test_x[a|b]"
        db = db_with([(f"c{i}", [res(tid, "passed")]) for i in range(12)])
        conn = store_mod.connect(db)
        store_mod.quarantine_add(conn, tid)
        conn.close()
        _, out, _ = run_cli("--db", db, "report")
        hint = next(ln for ln in out.splitlines() if "run: " in ln)
        self.assertNotIn("\\|", hint)
        self.assertIn("--all-stable", hint)

    # 7. a zero-byte or foreign SQLite file is refused, and left untouched
    def test_zero_byte_and_foreign_databases_are_refused(self):
        import sqlite3
        tmp = mktemp()
        zero = os.path.join(tmp, "zero.db")
        open(zero, "wb").close()
        foreign = os.path.join(tmp, "other.db")
        c = sqlite3.connect(foreign)
        c.execute("CREATE TABLE notes (x)")
        c.commit()
        c.close()
        for db, why in ((zero, "the file is empty"),
                        (foreign, "no flakebox tables")):
            before = open(db, "rb").read()
            for args in (["report"], ["history", "--test", "t::x"],
                         ["export", "--out", os.path.join(tmp, "e.json")],
                         ["quarantine", "--format", "json"],
                         ["unquarantine", "--test", "t::x"]):
                code, out, err = run_cli("--db", db, *args)
                self.assertEqual(code, 2, (db, args, out))
                self.assertIn("is not a flakebox database", err)
                self.assertIn(why, err)
            self.assertEqual(open(db, "rb").read(), before, db)
        # record still creates its tables in an empty file (a writer)
        code, _, _ = run_cli("--db", zero, "record", "--junit",
                             self._report(tmp), "--commit=c")
        self.assertEqual(code, 0)

    def _report(self, tmp):
        p = os.path.join(tmp, "r.xml")
        with open(p, "w", encoding="utf-8") as f:
            f.write('<testsuite><testcase classname="t" name="p"/></testsuite>')
        return p

    # 8. flakyError without a message falls back to the exception type
    def test_flaky_error_without_message_uses_the_type(self):
        xml = b"""<testsuite><testcase classname="c.T" name="t">
  <flakyError type="java.lang.NullPointerException">
    <stackTrace>java.lang.NullPointerException
	at c.T.t(T.java:5)</stackTrace>
  </flakyError></testcase></testsuite>"""
        (r,) = junit_mod.parse_bytes(xml, "s.xml")
        self.assertEqual((r["outcome"], r["message"]),
                         ("passed_on_rerun", "java.lang.NullPointerException"))


class TestVersion(unittest.TestCase):
    def test_version_matches_pyproject(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "pyproject.toml"), encoding="utf-8") as f:
            self.assertIn(f'version = "{cli_mod.VERSION}"', f.read())


if __name__ == "__main__":
    unittest.main()
