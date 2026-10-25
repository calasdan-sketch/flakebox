"""0.4.6: a test that fails and then passes inside ONE report (a retry
plugin) is a flake and is recorded as one ("retried"); a duplicate keeps the
longest duration; the Action no longer pip-installs."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from flakebox import cli as cli_mod
from flakebox import detect as detect_mod
from flakebox import junit as junit_mod
from flakebox import store as store_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def junit(*cases):
    return ("<testsuite>" + "".join(cases) + "</testsuite>").encode()


def tc(name="t", time="1", body="", cls="a.B"):
    return f'<testcase classname="{cls}" name="{name}" time="{time}">{body}</testcase>'


FAIL = '<failure message="boom"/>'
ERR = '<error message="err"/>'
SKIP = "<skipped/>"


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli_mod.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestRetryPass(unittest.TestCase):
    def outcome(self, *cases):
        return junit_mod.parse_bytes(junit(*cases))[0]

    def test_fail_then_pass_is_a_retry_pass(self):
        r = self.outcome(tc(body=FAIL), tc())
        self.assertEqual(r["outcome"], "retried")
        self.assertEqual(r["message"], "boom")

    def test_error_then_pass_is_a_retry_pass(self):
        self.assertEqual(self.outcome(tc(body=ERR), tc())["outcome"], "retried")

    def test_two_failures_then_a_pass_is_a_retry_pass(self):
        self.assertEqual(
            self.outcome(tc(body=FAIL), tc(body=FAIL), tc())["outcome"],
            "retried")

    def test_pass_then_fail_stays_failed(self):
        self.assertEqual(self.outcome(tc(), tc(body=FAIL))["outcome"], "failed")

    def test_fail_pass_fail_stays_failed(self):
        self.assertEqual(
            self.outcome(tc(body=FAIL), tc(), tc(body=FAIL))["outcome"],
            "failed")

    def test_fail_then_fail_is_failed_and_error_beats_failure(self):
        self.assertEqual(self.outcome(tc(body=FAIL), tc(body=ERR))["outcome"],
                         "error")

    def test_a_skipped_attempt_between_is_ignored(self):
        self.assertEqual(
            self.outcome(tc(body=FAIL), tc(body=SKIP), tc())["outcome"],
            "retried")

    def test_fail_then_skip_is_failed(self):
        self.assertEqual(self.outcome(tc(body=FAIL), tc(body=SKIP))["outcome"],
                         "failed")

    def test_pass_pass_is_passed(self):
        self.assertEqual(self.outcome(tc(), tc())["outcome"], "passed")

    def test_other_tests_in_the_report_are_untouched(self):
        res = junit_mod.parse_bytes(junit(
            tc("a", body=FAIL), tc("b"), tc("a"), tc("c", body=FAIL)))
        self.assertEqual({r["test_id"]: r["outcome"] for r in res},
                         {"a.B::a": "retried", "a.B::b": "passed",
                          "a.B::c": "failed"})

    def test_duration_is_the_longest_attempt_whatever_the_order(self):
        for cases in ((tc(time="5", body=FAIL), tc(time="1")),
                      (tc(time="1", body=FAIL), tc(time="5")),
                      (tc(time="1"), tc(time="5", body=FAIL), tc(time="2"))):
            self.assertEqual(self.outcome(*cases)["duration_ms"], 5000)

    def test_a_failed_duplicate_keeps_the_longest_duration_too(self):
        r = self.outcome(tc(time="9"), tc(time="2", body=FAIL))
        self.assertEqual((r["outcome"], r["duration_ms"]), ("failed", 9000))

    def test_is_retry_pass_unit(self):
        f = junit_mod.is_retry_pass
        self.assertTrue(f(["failed", "passed"]))
        self.assertFalse(f(["passed"]))
        self.assertFalse(f([]))
        self.assertFalse(f(["skipped", "passed"]))
        self.assertFalse(f(["passed", "failed"]))


class TestRetryInTheHistory(unittest.TestCase):
    def db_with(self, outcomes, n_other=0):
        conn = store_mod.connect(":memory:")
        for i, o in enumerate(outcomes):
            store_mod.record_run(conn, [
                {"test_id": "m::t", "outcome": o, "duration_ms": 1,
                 "message": ""}], commit_sha=f"c{i}", created_at=1000 + i)
        return conn

    def test_one_retry_pass_is_flaky_even_below_min_runs(self):
        conn = self.db_with(["retried"])
        a = detect_mod.analyze(conn, min_runs=5)
        self.assertEqual([s["test_id"] for s in a["flaky"]], ["m::t"])
        self.assertEqual(a["flaky"][0]["retries"], 1)
        self.assertEqual(a["flaky"][0]["failures"], 0)

    def test_flake_rate_counts_the_failed_attempt(self):
        s = detect_mod.stats(["retried", "passed", "passed", "passed"])
        self.assertAlmostEqual(s["flake_rate"], 0.25)
        self.assertEqual((s["runs"], s["passes"], s["failures"]), (4, 4, 0))

    def test_a_retry_pass_breaks_the_pass_streak(self):
        s = detect_mod.stats(["passed", "retried", "passed"])
        self.assertEqual(s["pass_streak"], 1)

    def test_a_long_pass_streak_after_it_recovers(self):
        s = detect_mod.stats(["passed"] * 10 + ["retried"], stable_runs=10)
        self.assertEqual(s["status"], "recovered")
        s = detect_mod.stats(["passed"] * 9 + ["retried"], stable_runs=10)
        self.assertEqual(s["status"], "flaky")

    def test_a_clean_history_is_not_flaky(self):
        a = detect_mod.analyze(self.db_with(["passed"] * 6), min_runs=5)
        self.assertEqual(a["flaky"], [])

    def test_history_command_reports_it_flaky(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        db = os.path.join(tmp, "h.db")
        rep = os.path.join(tmp, "r.xml")
        with open(rep, "wb") as f:
            f.write(junit(tc(body=FAIL), tc()))
        self.assertEqual(cli("--db", db, "record", "--junit", rep)[0], 0)
        code, out, _ = cli("--db", db, "history", "--test", "a.B::t")
        self.assertIn("flaky", out)
        self.assertIn("retried", out)

    def test_record_says_how_many_passed_on_retry(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        rep = os.path.join(tmp, "r.xml")
        with open(rep, "wb") as f:
            f.write(junit(tc(body=FAIL), tc(), tc("u")))
        code, out, _ = cli("--db", os.path.join(tmp, "h.db"), "record",
                           "--junit", rep)
        self.assertEqual(code, 0)
        self.assertIn("2 tests, 0 failed/error, 1 passed on retry", out)

    def test_report_shows_the_retry_pass(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        db = os.path.join(tmp, "h.db")
        rep = os.path.join(tmp, "r.xml")
        with open(rep, "wb") as f:
            f.write(junit(tc(body=FAIL), tc()))
        cli("--db", db, "record", "--junit", rep)
        code, out, _ = cli("--db", db, "report")
        self.assertIn("## Flaky", out)
        self.assertIn("Passed on retry", out)
        self.assertIn("`a.B::t`: 1 run(s)", out)
        code, out, _ = cli("--db", db, "report", "--format", "json")
        self.assertEqual(json.loads(out)["flaky"][0]["retries"], 1)

    def test_export_import_carries_the_outcome(self):
        conn = self.db_with(["retried"])
        data = store_mod.export_data(conn)
        other = store_mod.connect(":memory:")
        store_mod.import_data(other, data)
        self.assertEqual(
            other.execute("SELECT outcome FROM results").fetchall(),
            [("retried",)])


class TestActionWithoutPip(unittest.TestCase):
    def text(self):
        with open(os.path.join(REPO, "action.yml"), encoding="utf-8") as f:
            return f.read()

    def test_there_is_no_pip_call(self):
        runs = [ln for ln in self.text().splitlines()
                if not ln.lstrip().startswith("#")]
        self.assertFalse([ln for ln in runs if "pip" in ln], runs)

    def test_every_flakebox_step_sets_pythonpath_to_the_action(self):
        text = self.text()
        self.assertEqual(text.count("PYTHONPATH: ${{ github.action_path }}"),
                         5)  # check, record, report, quarantine, export

    def test_the_check_step_runs_from_a_foreign_directory(self):
        """What the runner does: cwd is the user's checkout, PYTHONPATH the
        action's copy; no installation anywhere."""
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        env = dict(os.environ, PYTHONPATH=REPO, HOME=work,
                   PIP_REQUIRE_VIRTUALENV="1")
        p = subprocess.run([sys.executable, "-m", "flakebox", "--version"],
                           cwd=work, env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("flakebox", p.stdout)

    @unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "bash")
    def test_the_check_step_script_gives_one_clear_line_when_python_fails(self):
        steps = [ln for ln in self.text().split("- name: ")
                 if ln.startswith("Check flakebox runs")]
        self.assertEqual(len(steps), 1)
        script = steps[0].split("run: |\n", 1)[1]
        script = "\n".join(ln[8:] for ln in script.splitlines())
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        bad = os.path.join(tmp, "python3")
        with open(bad, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(bad, 0o755)
        p = subprocess.run(["bash", "-e", "-c", script], cwd=tmp,
                           env={"PATH": tmp + os.pathsep + "/usr/bin:/bin"},
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error::flakebox needs Python 3.10", p.stdout)


class TestVersionIsBumpedEverywhere(unittest.TestCase):
    def test_all_places_agree(self):
        import re
        with open(os.path.join(REPO, "pyproject.toml"), encoding="utf-8") as f:
            v = re.search(r'^version = "([^"]+)"', f.read(), re.M).group(1)
        self.assertEqual(v, cli_mod.VERSION)
        with open(os.path.join(REPO, "CHANGELOG.md"), encoding="utf-8") as f:
            self.assertTrue(f.read().split("\n## ")[1].startswith(v))


if __name__ == "__main__":
    unittest.main()
