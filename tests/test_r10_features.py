"""0.5.0: known flakes vs new failures (summary), the flake budget, quarantine
expiry and retriage, the cost line, and jest / vitest / gotestsum reports
(real files written by those tools, in tests/fixtures)."""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from flakebox import cli as cli_mod
from flakebox import cost as cost_mod
from flakebox import detect as detect_mod
from flakebox import junit as junit_mod
from flakebox import quarantine as quar_mod
from flakebox import store as store_mod
from flakebox import summary as summary_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "tests", "fixtures")
DAY = 86400


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli_mod.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def row(test_id, outcome, ms=1000, message=""):
    return {"test_id": test_id, "outcome": outcome, "duration_ms": ms,
            "message": message}


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fb10")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "h.db")

    def record(self, results, sha=None, at=None):
        conn = store_mod.connect(self.db)
        try:
            return store_mod.record_run(conn, results, commit_sha=sha,
                                        created_at=at)
        finally:
            conn.close()

    def history(self, n=6):
        """test_flaky fails 3 of n runs, test_broken always fails,
        test_ok always passes."""
        for i in range(n):
            self.record([row("t::test_ok", "passed"),
                         row("t::test_flaky", "failed" if i % 2 else "passed"),
                         row("t::test_broken", "failed")], sha=f"c{i}",
                        at=1_000_000 + i)

    def junit(self, *cases, name="r.xml"):
        p = os.path.join(self.tmp, name)
        body = "".join(
            f'<testcase classname="t" name="{n}" time="1">{b}</testcase>'
            for n, b in cases)
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"<testsuite>{body}</testsuite>")
        return p


FAIL = '<failure message="boom"/>'


class TestRealReports(Tmp):
    def parse(self, name):
        return {r["test_id"]: r for r in junit_mod.parse(
            os.path.join(FIX, name))}

    def test_jest_outcomes(self):
        r = self.parse("jest-junit.xml")
        self.assertEqual(
            {k.split("::")[0]: v["outcome"] for k, v in r.items()},
            {"math add adds numbers": "passed",
             "math add breaks on purpose": "failed",
             "math not yet": "skipped",
             "math flaky then ok": "passed"})

    def test_jest_failure_message_comes_from_the_text(self):
        r = self.parse("jest-junit.xml")
        msg = next(v["message"] for k, v in r.items() if "breaks" in k)
        self.assertEqual(msg, "Error: expect(received).toBe(expected) "
                              "// Object.is equality")

    def test_jest_durations(self):
        r = self.parse("jest-junit.xml")
        self.assertEqual(r["math add adds numbers::math add adds numbers"]
                         ["duration_ms"], 2)

    def test_vitest_outcomes_and_ids(self):
        r = self.parse("vitest-junit.xml")
        self.assertEqual({k: v["outcome"] for k, v in r.items()}, {
            "src/v.test.js::suite > passes": "passed",
            "src/v.test.js::suite > fails": "failed",
            "src/v.test.js::suite > skipped": "skipped",
            "src/v.test.js::suite > flaky then ok": "passed"})

    def test_vitest_message_and_fractional_time(self):
        r = self.parse("vitest-junit.xml")
        f = r["src/v.test.js::suite > fails"]
        self.assertEqual(f["message"],
                         "expected 1 to be 2 // Object.is equality")
        self.assertEqual(f["duration_ms"], 6)

    def test_gotestsum_rerun_fails_is_a_retry_pass(self):
        r = self.parse("gotestsum-rerun-fails.xml")
        self.assertEqual(r["example.com/fx::TestFlaky"]["outcome"], "retried")
        self.assertEqual(r["example.com/fx::TestFlaky"]["message"],
                         "fx_test.go:22: first attempt")

    def test_gotestsum_outcomes(self):
        r = self.parse("gotestsum-rerun-fails.xml")
        self.assertEqual({k.split("::")[1]: v["outcome"] for k, v in r.items()},
                         {"TestFail": "failed", "TestSub/bad/case": "failed",
                          "TestSub": "failed", "TestFlaky": "retried",
                          "TestSkip": "skipped", "TestPass": "passed",
                          "TestSub/ok_case": "passed"})

    def test_gotestsum_failure_that_failed_every_rerun_is_one_row(self):
        rows = junit_mod.parse(os.path.join(FIX, "gotestsum-rerun-fails.xml"))
        ids = [r["test_id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(rows), 7)

    def test_gotestsum_message_skips_the_go_bookkeeping_lines(self):
        r = self.parse("gotestsum-rerun-fails.xml")
        self.assertEqual(r["example.com/fx::TestFail"]["message"],
                         "fx_test.go:10: boom")
        # a parent test has only bookkeeping lines: the attribute is kept
        self.assertEqual(r["example.com/fx::TestSub"]["message"], "Failed")

    def test_every_real_report_records_through_the_cli(self):
        for name, n in (("jest-junit.xml", 4), ("vitest-junit.xml", 4),
                        ("gotestsum-rerun-fails.xml", 7)):
            code, out, _ = cli("record", "--db", self.db, "--junit",
                               os.path.join(FIX, name), "--commit", name)
            self.assertEqual(code, 0, name)
            self.assertIn(f"{n} tests", out)

    def test_go_retry_shows_in_the_report_after_one_run(self):
        cli("record", "--db", self.db, "--junit",
            os.path.join(FIX, "gotestsum-rerun-fails.xml"), "--commit", "a")
        code, out, _ = cli("report", "--db", self.db)
        self.assertIn("example.com/fx::TestFlaky", out)
        self.assertIn("Passed on retry", out)

    def test_failure_message_unit(self):
        import xml.etree.ElementTree as ET
        f = lambda x: junit_mod.failure_message(ET.fromstring(x))
        self.assertEqual(f('<failure message="real"/>'), "real")
        self.assertEqual(f('<failure message="Failed">\n=== RUN x\n--- FAIL: x\n'
                           '  a_test.go:3: why\n</failure>'), "a_test.go:3: why")
        self.assertEqual(f("<failure>\n\n  Error: nope\nmore</failure>"),
                         "Error: nope")
        self.assertEqual(f("<failure/>"), "")
        self.assertEqual(len(f("<failure>" + "x" * 900 + "</failure>")), 500)

    def test_fixtures_ship_with_a_readme_naming_the_versions(self):
        with open(os.path.join(FIX, "README.txt"), encoding="utf-8") as f:
            text = f.read()
        for needle in ("jest 29.7.0", "vitest 2.1.8", "gotestsum 1.12.0"):
            self.assertIn(needle, text)


class TestSummaryClassify(Tmp):
    def analysis(self):
        self.history()
        conn = store_mod.connect(self.db)
        try:
            return detect_mod.analyze(conn, 5, 10), [
                q["test_id"] for q in store_mod.quarantined(conn)]
        finally:
            conn.close()

    def test_known_new_and_broken(self):
        a, q = self.analysis()
        c = summary_mod.classify([
            row("t::test_flaky", "failed"), row("t::test_brand_new", "failed"),
            row("t::test_broken", "error"), row("t::test_ok", "passed")], a, q)
        self.assertEqual([r["test_id"] for r in c["known"]], ["t::test_flaky"])
        self.assertEqual([r["test_id"] for r in c["new"]], ["t::test_brand_new"])
        self.assertEqual([r["test_id"] for r in c["broken"]], ["t::test_broken"])
        self.assertEqual((c["failed"], c["tests"]), (3, 4))

    def test_a_quarantined_test_is_a_known_flake(self):
        a, _ = self.analysis()
        c = summary_mod.classify([row("t::elsewhere", "failed")], a,
                                 ["t::elsewhere"])
        self.assertEqual(len(c["known"]), 1)
        self.assertTrue(c["known"][0]["quarantined"])

    def test_passed_and_skipped_are_not_failures(self):
        a, q = self.analysis()
        c = summary_mod.classify([row("t::a", "passed"), row("t::b", "skipped")],
                                 a, q)
        self.assertEqual((c["failed"], c["known"], c["new"]), (0, [], []))

    def test_a_retry_pass_is_listed_apart(self):
        a, q = self.analysis()
        c = summary_mod.classify([row("t::x", "retried")], a, q)
        self.assertEqual(len(c["retried"]), 1)
        self.assertEqual(c["failed"], 0)

    def test_headlines(self):
        h = lambda k, n, b=0: summary_mod.headline(
            {"known": [0] * k, "new": [0] * n, "broken": [0] * b})
        table = [
            ((0, 0), "No test failed in this report."),
            ((1, 0), "This failure is a known flake."),
            ((3, 0), "These 3 failures are known flakes."),
            ((0, 1), "This failure is new; it is not a known flake."),
            ((0, 2), "These 2 failures are new; none is a known flake."),
            ((3, 1), "3 failures are known flakes; this one is new."),
            ((1, 1), "1 failure is a known flake; this one is new."),
            ((2, 2), "2 failures are known flakes; 2 are new."),
            ((0, 0, 1), "1 failure was already failing."),
            ((0, 0, 3), "3 failures were already failing."),
            ((2, 1, 1), "2 failures are known flakes; 1 is new; "
                        "1 was already failing."),
        ]
        for args, want in table:
            self.assertEqual(h(*args), want, args)

    def test_advice_follows_the_facts(self):
        a = summary_mod.advice
        self.assertIn("look at it", a({"known": [1], "new": [1], "broken": []}))
        self.assertIn("re-run is reasonable",
                      a({"known": [1], "new": [], "broken": []}))
        self.assertIn("broken, not flaky",
                      a({"known": [], "new": [], "broken": [1]}))
        self.assertEqual(a({"known": [], "new": [], "broken": []}), "")


class TestSummaryCommand(Tmp):
    def test_the_documented_headline_end_to_end(self):
        self.history()
        # three known flakes cannot be had from one flaky test: build them
        for i in range(6):
            self.record([row(f"t::f{k}", "failed" if i % 2 else "passed")
                         for k in range(3)] + [row("t::test_ok", "passed")],
                        sha=f"d{i}", at=2_000_000 + i)
        rep = self.junit(("f0", FAIL), ("f1", FAIL), ("f2", FAIL),
                         ("fresh", FAIL), ("test_ok", ""))
        code, out, err = cli("summary", "--db", self.db, "--junit", rep)
        self.assertEqual(code, 0, err)
        self.assertIn("## Flakebox: 3 failures are known flakes; this one is "
                      "new.", out)
        self.assertTrue(out.startswith(summary_mod.MARKER + "\n"))
        self.assertIn("### New failures\n- `t::fresh`: `boom`", out)

    def test_order_matters_run_it_before_record(self):
        for i in range(6):
            self.record([row("t::x", "passed")], sha=f"s{i}", at=100 + i)
        rep = self.junit(("x", FAIL))
        _, before, _ = cli("summary", "--db", self.db, "--junit", rep,
                           "--min-runs", "1")
        self.assertIn("is new", before)
        cli("record", "--db", self.db, "--junit", rep, "--commit", "now")
        _, after, _ = cli("summary", "--db", self.db, "--junit", rep,
                          "--min-runs", "1")
        self.assertIn("known flake", after)

    def test_no_failures(self):
        self.history()
        rep = self.junit(("test_ok", ""))
        code, out, _ = cli("summary", "--db", self.db, "--junit", rep)
        self.assertEqual(code, 0)
        self.assertIn("No test failed in this report.", out)
        self.assertIn("0 of 1 test(s) failed", out)

    def test_an_empty_history_makes_every_failure_new(self):
        rep = self.junit(("a", FAIL))
        code, out, _ = cli("summary", "--db", self.db, "--junit", rep)
        self.assertEqual(code, 0)
        self.assertIn("This failure is new", out)

    def test_missing_and_broken_reports_exit_2(self):
        code, _, err = cli("summary", "--db", self.db, "--junit",
                           os.path.join(self.tmp, "nope.xml"))
        self.assertEqual((code, "no such file" in err), (2, True))
        bad = os.path.join(self.tmp, "bad.xml")
        with open(bad, "w") as f:
            f.write("<testsuite>")
        code, _, err = cli("summary", "--db", self.db, "--junit", bad)
        self.assertEqual((code, "cannot parse" in err), (2, True))

    def test_the_output_is_deterministic(self):
        self.history()
        rep = self.junit(("test_flaky", FAIL), ("n", FAIL))
        a = cli("summary", "--db", self.db, "--junit", rep)
        b = cli("summary", "--db", self.db, "--junit", rep)
        self.assertEqual(a, b)

    def test_hostile_names_and_messages_stay_in_code_spans(self):
        evil = "a`b``c@octocat <img src=x> [x](http://e.com)\n## Flakebox: ok"
        c = {"known": [], "broken": [], "retried": [], "failed": 1, "tests": 1,
             "new": [dict(row(evil, "failed", message=evil), flake_rate=None,
                          quarantined=False, known=False)]}
        text = summary_mod.to_markdown(c)
        self.assertEqual(len(re.findall(r"(?m)^## ", text)), 1)
        outside = re.sub(r"(`+)[^`\n]*?\1", "", text)
        self.assertNotIn("@octocat", outside.replace("@​", ""))
        self.assertNotIn("<img", outside)
        self.assertNotIn("](", outside)
        self.assertNotRegex(text, r"(?m)^## Flakebox: ok")

    def test_a_long_message_is_cut_in_plain_ascii(self):
        c = {"known": [], "broken": [], "retried": [], "failed": 1, "tests": 1,
             "new": [dict(row("t::x", "failed", message="m" * 500),
                          flake_rate=None, quarantined=False)]}
        text = summary_mod.to_markdown(c)
        self.assertIn("m" * 117 + "...", text)
        text.encode("ascii")

    def test_md_code_fences(self):
        self.assertEqual(summary_mod.md_code("a"), "`a`")
        self.assertEqual(summary_mod.md_code("a`b"), "``a`b``")
        self.assertEqual(summary_mod.md_code("`x`"), "`` `x` ``")
        self.assertEqual(summary_mod.md_code("x\n\ny"), "`x y`")

    def test_a_long_list_is_cut(self):
        rows = [dict(row(f"t::n{i}", "failed"), flake_rate=None,
                     quarantined=False) for i in range(40)]
        c = {"known": [], "broken": [], "retried": [], "new": rows,
             "failed": 40, "tests": 40}
        text = summary_mod.to_markdown(c)
        self.assertIn("- and 15 more", text)
        self.assertEqual(text.count("`t::n"), summary_mod.MAX_LISTED)

    def test_summary_output_has_unix_newlines(self):
        out = subprocess.run(
            [sys.executable, "-m", "flakebox", "summary", "--db", self.db,
             "--junit", self.junit(("a", FAIL))],
            cwd=self.tmp, env=dict(os.environ, PYTHONPATH=REPO),
            capture_output=True).stdout
        self.assertNotIn(b"\r", out)


class TestBudget(Tmp):
    def test_totals(self):
        self.history()
        conn = store_mod.connect(self.db)
        a = detect_mod.analyze(conn, 5, 10)
        conn.close()
        self.assertEqual(a["totals"], {"active": 3, "flaky": 1,
                                       "flaky_percent": 33.33})

    def test_totals_with_no_tests(self):
        self.assertEqual(detect_mod.totals(0, 0),
                         {"active": 0, "flaky": 0, "flaky_percent": 0.0})

    def test_retired_tests_are_not_counted(self):
        self.history()
        for i in range(3):
            self.record([row("t::test_ok", "passed")], sha=f"z{i}",
                        at=5_000_000 + i)
        conn = store_mod.connect(self.db)
        a = detect_mod.analyze(conn, 5, 10)
        conn.close()
        self.assertEqual(a["totals"]["active"], 1)

    def test_over_the_limit_exits_1_and_says_why(self):
        self.history()
        code, out, err = cli("report", "--db", self.db,
                             "--max-flaky-percent", "10")
        self.assertEqual(code, 1)
        self.assertIn("flake budget exceeded: 1 of 3 test(s) are flaky "
                      "(33.33%); the limit is 10%", err)
        self.assertIn("EXCEEDED", out)

    def test_exactly_at_the_limit_passes(self):
        self.history()
        code, out, err = cli("report", "--db", self.db,
                             "--max-flaky-percent", "33.33333333333333333")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("- ok", out)

    def test_under_the_limit_passes(self):
        self.history()
        self.assertEqual(cli("report", "--db", self.db,
                             "--max-flaky-percent", "50")[0], 0)

    def test_zero_percent_fails_on_any_flake(self):
        self.history()
        self.assertEqual(cli("report", "--db", self.db,
                             "--max-flaky-percent", "0")[0], 1)

    def test_off_by_default(self):
        self.history()
        code, out, err = cli("report", "--db", self.db)
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("Flake budget", out)

    def test_json_carries_the_budget_and_totals(self):
        self.history()
        code, out, _ = cli("report", "--db", self.db, "--format", "json",
                           "--max-flaky-percent", "10")
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertTrue(data["budget"]["exceeded"])
        self.assertEqual(data["totals"]["flaky"], 1)

    def test_bad_values_are_refused(self):
        for bad in ("abc", "-1", "101", "nan", "inf"):
            with self.assertRaises(SystemExit) as cm, \
                    redirect_stderr(io.StringIO()):
                cli_mod.main(["report", "--db", self.db,
                              "--max-flaky-percent", bad])
            self.assertEqual(cm.exception.code, 2, bad)

    def test_summary_applies_the_same_gate(self):
        self.history()
        code, out, err = cli("summary", "--db", self.db, "--junit",
                             self.junit(("a", "")), "--max-flaky-percent", "10")
        self.assertEqual(code, 1)
        self.assertIn("Flake budget: 1 of 3 test(s) are flaky", out)
        self.assertIn("**exceeded**", out)


class TestQuarantineExpiry(Tmp):
    NOW = 10_000_000_000

    def entries(self):
        return [{"test_id": "old", "at": self.NOW - 40 * DAY, "reason": ""},
                {"test_id": "older", "at": self.NOW - 90 * DAY, "reason": ""},
                {"test_id": "new", "at": self.NOW - 2 * DAY, "reason": ""},
                {"test_id": "undated", "at": None, "reason": ""}]

    def test_expired_lists_the_old_ones_oldest_first(self):
        got = quar_mod.expired(self.entries(), 30, now=self.NOW)
        self.assertEqual(got, [{"test_id": "older", "days": 90},
                               {"test_id": "old", "days": 40}])

    def test_off_when_days_is_none(self):
        self.assertEqual(quar_mod.expired(self.entries(), None, self.NOW), [])

    def test_the_boundary_is_strict(self):
        e = [{"test_id": "x", "at": self.NOW - 30 * DAY}]
        self.assertEqual(quar_mod.expired(e, 30, self.NOW), [])
        e = [{"test_id": "x", "at": self.NOW - 30 * DAY - 1}]
        self.assertEqual(len(quar_mod.expired(e, 30, self.NOW)), 1)

    def old_quarantine(self, days=40):
        self.history()
        conn = store_mod.connect(self.db)
        store_mod.quarantine_add(conn, "t::test_flaky", "auto: flaky",
                                 at=int(__import__("time").time()) - days * DAY)
        conn.close()

    def test_report_warns_and_exits_0_by_default(self):
        self.old_quarantine()
        code, out, err = cli("report", "--db", self.db, "--quarantine-days", "30")
        self.assertEqual(code, 0)
        self.assertIn("warning: 1 quarantined test(s) are older than 30", err)
        self.assertIn("## Quarantine older than 30 day(s)", out)
        self.assertIn("flakebox retriage --test \"t::test_flaky\"", out)

    def test_fail_mode_exits_1(self):
        self.old_quarantine()
        code, _, err = cli("report", "--db", self.db, "--quarantine-days", "30",
                           "--quarantine-expiry", "fail")
        self.assertEqual(code, 1)
        self.assertIn("error: 1 quarantined test(s)", err)

    def test_nothing_expired_is_silent(self):
        self.old_quarantine(days=3)
        code, out, err = cli("report", "--db", self.db, "--quarantine-days", "30",
                             "--quarantine-expiry", "fail")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("Quarantine older", out)

    def test_off_by_default(self):
        self.old_quarantine(days=4000)
        code, out, err = cli("report", "--db", self.db)
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("Quarantine older", out)

    def test_retriage_restarts_the_clock(self):
        self.old_quarantine()
        code, out, _ = cli("retriage", "--db", self.db, "--test", "t::test_flaky")
        self.assertEqual(code, 0)
        self.assertIn("stays quarantined", out)
        code, _, err = cli("report", "--db", self.db, "--quarantine-days", "30",
                           "--quarantine-expiry", "fail")
        self.assertEqual((code, err), (0, ""))

    def test_retriage_of_a_test_that_is_not_quarantined(self):
        self.history()
        code, _, err = cli("retriage", "--db", self.db, "--test", "t::nope")
        self.assertEqual(code, 2)
        self.assertIn("is not quarantined", err)

    def test_unquarantine_ends_the_warning(self):
        self.old_quarantine()
        cli("unquarantine", "--db", self.db, "--test", "t::test_flaky")
        code, _, err = cli("report", "--db", self.db, "--quarantine-days", "30",
                           "--quarantine-expiry", "fail")
        self.assertEqual((code, err), (0, ""))

    def test_quarantine_command_keeps_stdout_clean_and_still_fails(self):
        self.old_quarantine()
        code, out, err = cli("quarantine", "--db", self.db, "--format", "json",
                             "--quarantine-days", "30",
                             "--quarantine-expiry", "fail")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["quarantined"], ["t::test_flaky"])
        self.assertIn("older than 30 day(s)", err)

    def test_quarantine_command_does_not_restart_the_clock(self):
        self.old_quarantine()
        cli("quarantine", "--db", self.db, "--format", "json")
        code, _, _ = cli("report", "--db", self.db, "--quarantine-days", "30",
                         "--quarantine-expiry", "fail")
        self.assertEqual(code, 1)

    def test_summary_lists_the_expired(self):
        self.old_quarantine()
        code, out, _ = cli("summary", "--db", self.db, "--junit",
                           self.junit(("a", "")), "--quarantine-days", "30")
        self.assertEqual(code, 0)
        self.assertIn("### Quarantine older than 30 day(s)", out)
        self.assertIn("`t::test_flaky`, 40 day(s)", out)

    def test_bad_days_are_refused(self):
        for bad in ("0", "-3", "x", "1.5", "99999999"):
            with self.assertRaises(SystemExit) as cm, \
                    redirect_stderr(io.StringIO()):
                cli_mod.main(["report", "--db", self.db,
                              "--quarantine-days", bad])
            self.assertEqual(cm.exception.code, 2, bad)

    def test_touch_on_the_store(self):
        conn = store_mod.connect(self.db)
        store_mod.quarantine_add(conn, "a", at=5)
        self.assertTrue(store_mod.quarantine_touch(conn, "a", at=99))
        self.assertEqual(store_mod.quarantined(conn)[0]["at"], 99)
        self.assertFalse(store_mod.quarantine_touch(conn, "b"))
        conn.close()


class TestCost(Tmp):
    def test_estimate_from_the_reports_alone(self):
        # run 1 (c1): flaky fails 4s; others 6s.  run 2 (c1 again): a repeat.
        self.record([row("a", "passed", 6000), row("f", "failed", 4000)],
                    sha="c1", at=10)
        self.record([row("a", "passed", 6000), row("f", "passed", 1000)],
                    sha="c1", at=20)
        self.record([row("a", "passed", 6000), row("f", "retried", 3000),
                     row("s", "skipped", 99999)], sha="c2", at=30)
        conn = store_mod.connect(self.db)
        est = cost_mod.estimate(conn, {"f"})
        conn.close()
        self.assertEqual(est, {"runs": 3, "test_ms": 10000 + 7000 + 9000,
                               "flake_ms": 4000 + 3000, "runs_hit": 2,
                               "repeat_runs": 1, "repeat_ms": 7000})

    def test_a_run_without_a_commit_is_never_a_repeat(self):
        self.record([row("a", "passed")], sha=None, at=1)
        self.record([row("a", "passed")], sha=None, at=2)
        conn = store_mod.connect(self.db)
        self.assertEqual(cost_mod.estimate(conn, set())["repeat_runs"], 0)
        conn.close()

    def test_the_window_limits_the_runs(self):
        for i in range(7):
            self.record([row("a", "passed")], sha=f"s{i}", at=i)
        conn = store_mod.connect(self.db)
        self.assertEqual(cost_mod.estimate(conn, set(), window=3)["runs"], 3)
        conn.close()

    def test_nothing_recorded(self):
        conn = store_mod.connect(self.db)
        self.assertIsNone(cost_mod.estimate(conn, set()))
        conn.close()
        self.assertEqual(cost_mod.line(None), "")

    def test_minutes(self):
        m = cost_mod.minutes
        self.assertEqual((m(0), m(10), m(90000), m(600000)),
                         ("0 min", "<0.1 min", "1.5 min", "10.0 min"))

    def test_the_line_says_what_it_is_and_is_not(self):
        line = cost_mod.line({"runs": 50, "test_ms": 600000, "flake_ms": 30000,
                              "runs_hit": 4, "repeat_runs": 2,
                              "repeat_ms": 120000})
        self.assertEqual(
            line, "Cost, from your own reports: flaky tests failed in 4 of the "
                  "last 50 run(s), 0.5 min of 10.0 min of reported test time; "
                  "2 run(s) repeated a commit that had already run (2.0 min "
                  "of test time). Test time only: setup and queueing are not "
                  "in a JUnit report.")

    def test_reports_without_times_make_no_minutes_up(self):
        line = cost_mod.line({"runs": 2, "test_ms": 0, "flake_ms": 0,
                              "runs_hit": 1, "repeat_runs": 0, "repeat_ms": 0})
        self.assertIn("carry no test times", line)
        self.assertNotIn("min of", line)

    def test_report_and_json_show_it(self):
        self.history()
        _, out, _ = cli("report", "--db", self.db)
        self.assertIn("Cost, from your own reports:", out)
        _, js, _ = cli("report", "--db", self.db, "--format", "json")
        self.assertEqual(json.loads(js)["cost"]["runs"], 6)

    def test_no_invented_numbers_in_the_code(self):
        with open(os.path.join(REPO, "flakebox", "cost.py"),
                  encoding="utf-8") as f:
            code = f.read()
        for word in ("USD", "dollar", "per minute", "$"):
            self.assertNotIn(word, re.sub(r'""".*?"""', "", code, flags=re.S),
                             word)

    def test_the_unsourced_claim_is_gone(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertNotRegex(readme, r"10\s*[-–]\s*25\s*%")
        self.assertNotIn("CI compute", readme)


class TestActionSteps(Tmp):
    """The new Action steps, run under bash with the real package."""

    def steps(self):
        sys.path.insert(0, os.path.join(REPO, "tests"))
        try:
            from test_flakebox import action_steps
        finally:
            sys.path.pop(0)
        with open(os.path.join(REPO, "action.yml"), encoding="utf-8") as f:
            return action_steps(f.read())

    def step(self, prefix):
        return next(s for s in self.steps() if s["name"].startswith(prefix))

    def run_step(self, prefix, inputs, junit=None):
        s = self.step(prefix)
        ctx = {"github.action_path": REPO, "inputs.junit": junit or "",
               "inputs.min-runs": "5", "inputs.summary": "true",
               "inputs.comment-output": "", "inputs.max-flaky-percent": "",
               "inputs.quarantine-days": "", "inputs.quarantine-expiry": "warn"}
        ctx.update(inputs)
        sub = lambda t: re.sub(r"\$\{\{\s*([^}]*?)\s*\}\}",
                               lambda m: ctx[m.group(1)], t)
        summary = os.path.join(self.tmp, "summary.md")
        env = dict(os.environ, HOME=self.tmp, GITHUB_STEP_SUMMARY=summary)
        env.update({k: sub(v) for k, v in s["env"].items()})
        p = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o",
                            "pipefail", "-c", sub(s["run"])], cwd=self.tmp,
                           env=env, capture_output=True, text=True, timeout=60)
        text = ""
        if os.path.exists(summary):
            with open(summary, encoding="utf-8") as f:
                text = f.read()
        return p, text

    def test_the_summary_step_comes_before_record(self):
        names = [s["name"] for s in self.steps()]
        self.assertLess(
            next(i for i, n in enumerate(names) if n.startswith("Summarize")),
            names.index("Record results"))

    def test_the_new_inputs_are_documented(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        for name in ("summary", "comment-output", "max-flaky-percent",
                     "quarantine-days", "quarantine-expiry"):
            self.assertIn(f"`{name}`", readme)
            with open(os.path.join(REPO, "action.yml"),
                      encoding="utf-8") as f:
                self.assertIn(f"  {name}:", f.read())

    @unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "bash")
    def test_summary_step_writes_summary_and_comment_file(self):
        self.history()
        shutil.copy(self.db, os.path.join(self.tmp, ".flakebox_h"))
        os.makedirs(os.path.join(self.tmp, ".flakebox"))
        shutil.move(os.path.join(self.tmp, ".flakebox_h"),
                    os.path.join(self.tmp, ".flakebox", "history.db"))
        rep = self.junit(("test_flaky", FAIL), ("fresh", FAIL))
        out = os.path.join(self.tmp, "sub", "comment.md")
        p, text = self.run_step("Summarize", {"inputs.comment-output": out},
                                junit=rep)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("1 failure is a known flake; this one is new.", text)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)

    @unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "bash")
    def test_summary_false_still_writes_the_comment_file_only(self):
        rep = self.junit(("a", FAIL))
        out = os.path.join(self.tmp, "c.md")
        p, text = self.run_step("Summarize", {"inputs.summary": "false",
                                              "inputs.comment-output": out},
                                junit=rep)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(text, "")
        self.assertTrue(os.path.exists(out))

    @unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "bash")
    def test_a_leading_dash_comment_path_is_one_word(self):
        rep = self.junit(("a", FAIL))
        p, _ = self.run_step("Summarize", {"inputs.comment-output": "-c.md"},
                             junit=rep)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "-c.md")))

    @unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "bash")
    def test_budget_step_fails_with_an_error_line(self):
        self.history()
        os.makedirs(os.path.join(self.tmp, ".flakebox"))
        shutil.copy(self.db, os.path.join(self.tmp, ".flakebox", "history.db"))
        p, _ = self.run_step("Check the flake budget",
                             {"inputs.max-flaky-percent": "5"})
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error::flakebox:", p.stdout)
        self.assertIn("flake budget exceeded", p.stderr)

    @unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "bash")
    def test_budget_step_passes_when_within_limits(self):
        self.history()
        os.makedirs(os.path.join(self.tmp, ".flakebox"))
        shutil.copy(self.db, os.path.join(self.tmp, ".flakebox", "history.db"))
        p, _ = self.run_step("Check the flake budget",
                             {"inputs.max-flaky-percent": "90",
                              "inputs.quarantine-days": "30",
                              "inputs.quarantine-expiry": "fail"})
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_budget_step_only_runs_when_asked(self):
        cond = self.step("Check the flake budget")
        with open(os.path.join(REPO, "action.yml"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("if: inputs.max-flaky-percent != '' || "
                      "inputs.quarantine-days != ''", text)
        self.assertIsNotNone(cond)


class TestConnectDoesNotLeak(Tmp):
    def test_a_file_that_is_not_a_database_leaves_no_open_handle(self):
        import sqlite3
        bad = os.path.join(self.tmp, "notadb")
        with open(bad, "wb") as f:
            f.write(b"this is not a sqlite file" * 100)
        opened = []
        real = sqlite3.connect

        def spy(*a, **k):
            c = real(*a, **k)
            opened.append(c)
            return c
        sqlite3.connect = spy
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                store_mod.connect(bad)
        finally:
            sqlite3.connect = real
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):   # closed
            opened[0].execute("select 1")


class TestPackaging(unittest.TestCase):
    def test_version_everywhere(self):
        with open(os.path.join(REPO, "pyproject.toml"), encoding="utf-8") as f:
            self.assertIn('version = "0.5.0"', f.read())
        self.assertEqual(cli_mod.VERSION, "0.5.0")
        with open(os.path.join(REPO, "CHANGELOG.md"), encoding="utf-8") as f:
            self.assertTrue(f.read().split("\n## ")[1].startswith("0.5.0"))

    def test_fixtures_are_in_the_sdist_manifest(self):
        with open(os.path.join(REPO, "MANIFEST.in"), encoding="utf-8") as f:
            self.assertIn("recursive-include tests *.xml *.txt", f.read())


if __name__ == "__main__":
    unittest.main()
