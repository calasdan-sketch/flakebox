"""0.6.0: the statistics (regression vs flake, intervals, adaptive release),
retries in the JUnit file, quarantine governance, other runners, triage."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from flakebox import detect as detect_mod
from flakebox import junit as junit_mod
from flakebox import quarantine as quar_mod
from flakebox import rootcause
from flakebox import stat as stat_mod
from flakebox import store as store_mod
from flakebox import triage as triage_mod

from tests.test_flakebox import (  # noqa: F401  (module fixtures)
    child_env, mktemp, result, run_cli, setUpModule, tearDownModule)


def res(tid, outcome, message=""):
    r = result(tid, outcome)
    r["message"] = message
    return r


def history_db(outcomes, tid="t::x", messages=None, name="h.db",
               commits=True, quarantined=False):
    """A DB with one test whose outcomes are given OLDEST first
    ('p' pass, 'f' fail), one commit per run."""
    db = os.path.join(mktemp(), name)
    conn = store_mod.connect(db)
    for i, o in enumerate(outcomes):
        msg = messages[i] if messages else ""
        store_mod.record_run(
            conn, [res(tid, "failed" if o == "f" else "passed", msg)],
            commit_sha=f"c{i}" if commits else None,
            created_at=1_700_000_000 + i * 60)
    if quarantined:
        store_mod.quarantine_add(conn, tid, "auto: flaky")
    conn.close()
    return db


def analyze(db, **kw):
    conn = store_mod.connect(db)
    try:
        return detect_mod.analyze(conn, **kw)
    finally:
        conn.close()


class TestStat(unittest.TestCase):
    def test_wilson_matches_textbook_values(self):
        lo, hi = stat_mod.wilson(1, 4)
        self.assertAlmostEqual(lo, 0.0456, places=3)
        self.assertAlmostEqual(hi, 0.6994, places=3)
        lo, hi = stat_mod.wilson(0, 50)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.0713, places=3)
        self.assertEqual(stat_mod.wilson(0, 0), (0.0, 1.0))
        lo, hi = stat_mod.wilson(10, 10)
        self.assertAlmostEqual(hi, 1.0)
        self.assertGreater(lo, 0.7)

    def test_wilson_stays_inside_zero_one_and_is_ordered(self):
        for n in range(0, 40):
            for k in range(0, n + 1):
                lo, hi = stat_mod.wilson(k, n)
                self.assertTrue(0.0 <= lo <= hi <= 1.0, (k, n, lo, hi))
                if n:
                    self.assertTrue(lo - 1e-9 <= k / n <= hi + 1e-9, (k, n))

    def test_runs_to_catch(self):
        self.assertEqual(stat_mod.runs_to_catch(0.05), 59)
        self.assertEqual(stat_mod.runs_to_catch(0.5), 5)
        self.assertEqual(stat_mod.runs_to_catch(1.0), 1)
        self.assertIsNone(stat_mod.runs_to_catch(0))

    def test_change_point_finds_the_step(self):
        cp = stat_mod.change_point([0] * 40 + [1] * 4)
        self.assertEqual(cp["at"], 40)
        self.assertEqual((cp["before"], cp["after"]), (0.0, 1.0))

    def test_change_point_ignores_a_constant_rate(self):
        self.assertIsNone(stat_mod.change_point([0, 1] * 25))
        self.assertIsNone(stat_mod.change_point([0] * 30))
        self.assertIsNone(stat_mod.change_point([1] * 30))
        self.assertIsNone(stat_mod.change_point([0] * 40 + [1]))   # one blip
        self.assertIsNone(stat_mod.change_point([0, 1, 0]))        # too short

    def test_change_point_is_rare_on_stationary_flakes(self):
        import random
        rng = random.Random(3)
        hits = 0
        for _ in range(600):
            p = rng.choice([0.05, 0.1, 0.2, 0.35, 0.5])
            hits += stat_mod.change_point(
                [int(rng.random() < p) for _ in range(50)]) is not None
        self.assertLess(hits / 600, 0.03)


class TestRegressionIsNotAFlake(unittest.TestCase):
    OUT = "p" * 30 + "f" * 4

    def test_a_break_is_regressed_with_the_commits_to_bisect(self):
        a = analyze(history_db(self.OUT))
        self.assertEqual(a["flaky"], [])
        (s,) = a["regressed"]
        self.assertEqual(s["status"], "regressed")
        self.assertEqual(s["change"]["last_good_commit"], "c29")
        self.assertEqual(s["change"]["first_bad_commit"], "c30")
        self.assertEqual(s["change"]["runs_since"], 4)

    def test_the_legacy_model_calls_it_flaky_as_before(self):
        a = analyze(history_db(self.OUT), model="legacy")
        self.assertEqual(len(a["flaky"]), 1)
        self.assertEqual(a["regressed"], [])

    def test_a_real_flake_stays_flaky(self):
        a = analyze(history_db("pfpppfppppfpppfppppfpppfpppfp"))
        self.assertEqual(len(a["flaky"]), 1)
        self.assertEqual(a["regressed"], [])

    def test_one_failure_at_the_end_is_not_a_regression(self):
        a = analyze(history_db("p" * 30 + "f"))
        self.assertEqual(a["regressed"], [])

    def test_proof_beats_the_step(self):
        # the same commit passed and failed: flaky, whatever the tail says
        db = os.path.join(mktemp(), "p.db")
        conn = store_mod.connect(db)
        for i in range(30):
            store_mod.record_run(conn, [res("t::x", "passed")],
                                 commit_sha=f"c{i}", created_at=1000 + i)
        for i in range(4):
            store_mod.record_run(conn, [res("t::x", "failed")],
                                 commit_sha="bad", created_at=2000 + i)
        store_mod.record_run(conn, [res("t::x", "passed")],
                             commit_sha="bad", created_at=2100)
        conn.close()
        a = analyze(db)
        self.assertEqual(len(a["flaky"]), 1)
        self.assertTrue(a["flaky"][0]["proven"])

    def test_quarantine_never_hides_a_regression(self):
        db = history_db(self.OUT)
        code, out, _ = run_cli("--db", db, "quarantine", "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["quarantined"], [])
        code, out, _ = run_cli("--db", db, "report")
        self.assertIn("## Regressed", out)
        self.assertIn("last good commit c29", out)
        self.assertIn("first bad c30", out)
        self.assertNotIn("## Flaky", out)

    def test_history_names_the_regression(self):
        db = history_db(self.OUT)
        code, out, _ = run_cli("--db", db, "history", "--test", "t::x")
        self.assertIn("regressed", out)
        self.assertIn("REGRESSION, not a flake", out)

    def test_json_report_has_the_bucket_and_the_interval(self):
        code, out, _ = run_cli("--db", history_db(self.OUT), "report",
                               "--format", "json")
        data = json.loads(out)
        self.assertEqual(len(data["regressed"]), 1)
        s = data["regressed"][0]
        self.assertTrue(0 <= s["rate_low"] <= s["flake_rate"] <= s["rate_high"] <= 1)

    def test_a_flake_that_began_later_is_flaky_with_an_onset(self):
        out = "p" * 30 + "fpfppfpfpp" + "fpppfpfpff"
        a = analyze(history_db(out))
        (s,) = a["flaky"]
        self.assertEqual(s["change"]["kind"], "onset")
        self.assertEqual(s["change"]["runs_before"], 30)

    def test_report_shows_the_interval(self):
        code, out, _ = run_cli("--db", history_db("pfpfpfpfpf"), "report")
        self.assertRegex(out, r"\| 50% \(\d+-\d+%\) \|")


class TestAdaptiveRelease(unittest.TestCase):
    def test_ten_passes_do_not_release_a_ten_percent_flake(self):
        # 3 failures in 33 runs, then 10 passes: at that rate it takes ~30
        # passes to see a failure again, so 10 is not evidence it is fixed
        out = "ppppppppppfppppppppppfppppppppppf" + "p" * 10
        db = history_db(out, quarantined=True)
        a = analyze(db)
        self.assertEqual(a["stable"], [])
        legacy = analyze(db, model="legacy")
        self.assertEqual(len(legacy["stable"]), 1)

    def test_enough_passes_release_it(self):
        out = "ppppppppppfppppppppppfppppppppppf" + "p" * 40
        a = analyze(history_db(out, quarantined=True))
        self.assertEqual(len(a["stable"]), 1)
        self.assertGreater(a["stable"][0]["stable_needed"], 10)

    def test_a_half_flake_needs_only_the_floor(self):
        out = "pfpfpfpfpf" + "p" * 10
        a = analyze(history_db(out, quarantined=True))
        self.assertEqual(len(a["stable"]), 1)
        self.assertEqual(a["stable"][0]["stable_needed"], 10)

    def test_the_floor_is_the_stable_runs_option(self):
        out = "pfpfpfpfpf" + "p" * 6
        a = analyze(history_db(out, quarantined=True), stable_runs=5)
        self.assertEqual(len(a["stable"]), 1)

    def test_a_low_rate_flake_is_capped_at_the_window(self):
        out = "p" * 20 + "f" + "p" * 40
        a = analyze(history_db(out, quarantined=True), window=30)
        self.assertEqual(len(a["stable"]), 1)   # needs <= window passes


class TestRootCause(unittest.TestCase):
    CASES = [
        ("ConnectionError: connection refused on port 5432", "network"),
        ("socket.timeout: timed out", "network"),
        ("HTTP 503 Service Unavailable", "network"),
        ("OSError: [Errno 24] Too many open files", "resource"),
        ("MemoryError", "resource"),
        ("Timeout of 5000ms exceeded", "timing"),
        ("element not visible within 10s", "timing"),
        ("sqlite3.OperationalError: database is locked", "concurrency"),
        ("data race detected", "concurrency"),
        ("IntegrityError: UNIQUE constraint failed: users.email", "shared-state"),
        ("expected 2024-03-10 but was 2024-03-09 (timezone)", "time-or-random"),
        ("assert 404 == 200", "unknown"),
        ("", "unknown"),
        ("Traceback (most recent call last)", "unknown"),   # "race" in trace
    ]

    def test_families(self):
        for message, want in self.CASES:
            self.assertEqual(rootcause.family(message), want, message)

    def test_huge_message_is_fast(self):
        start = time.monotonic()
        rootcause.family("x" * 5_000_000)
        self.assertLess(time.monotonic() - start, 1)

    def test_failure_modes_carry_the_family(self):
        db = history_db("pfpfpfpfpf", messages=["connection refused"] * 10)
        (s,) = analyze(db)["flaky"]
        self.assertEqual(s["failure_modes"][0]["family"], "network")
        code, out, _ = run_cli("--db", db, "report")
        self.assertIn("[network]", out)


# What the real tools wrote (trimmed; captured with pytest 9 +
# pytest-rerunfailures 16.7, and gotestsum 1.12 --rerun-fails).
PYTEST_RERUN_GREEN = (
    '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
    '<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="2">'
    '<testcase classname="test_f" name="test_flaky" time="0.001" />'
    '<testcase classname="test_f" name="test_flaky" time="0.000" />'
    '<testcase classname="test_f" name="test_ok" time="0.000" />'
    '</testsuite></testsuites>').encode()
PYTEST_RERUN_RED = (
    '<testsuites name="pytest tests"><testsuite name="pytest" failures="1">'
    '<testcase classname="test_g" name="test_always" time="0.000" />'
    '<testcase classname="test_g" name="test_always" time="0.001" />'
    '<testcase classname="test_g" name="test_always" time="0.001">'
    '<failure message="assert False">trace</failure></testcase>'
    '</testsuite></testsuites>').encode()
GOTESTSUM_RERUN = (
    '<testsuites tests="3" failures="1"><testsuite tests="3" name="ex">'
    '<testcase classname="ex" name="TestFlaky" time="0.0">'
    '<failure message="Failed" type="">boom</failure></testcase>'
    '<testcase classname="ex" name="TestOK" time="0.0"></testcase>'
    '<testcase classname="ex" name="TestFlaky" time="0.0"></testcase>'
    '</testsuite></testsuites>').encode()


class TestRetriesInTheReport(unittest.TestCase):
    def outcomes(self, raw, **kw):
        return {r["test_id"]: r["outcome"]
                for r in junit_mod.parse_bytes(raw, **kw)}

    def test_pytest_rerunfailures_green_retry_is_a_rerun_pass(self):
        self.assertEqual(self.outcomes(PYTEST_RERUN_GREEN), {
            "test_f::test_flaky": "passed_on_rerun",
            "test_f::test_ok": "passed"})

    def test_pytest_rerunfailures_all_red_is_one_failure(self):
        # it used to be two passes and a failure: an always-red test that
        # looked flaky
        self.assertEqual(self.outcomes(PYTEST_RERUN_RED),
                         {"test_g::test_always": "failed"})
        (r,) = junit_mod.parse_bytes(PYTEST_RERUN_RED)
        self.assertEqual(r["message"], "assert False")

    def test_gotestsum_fail_then_pass_is_a_rerun_pass_with_the_message(self):
        got = junit_mod.parse_bytes(GOTESTSUM_RERUN)
        by = {r["test_id"]: r for r in got}
        self.assertEqual(by["ex::TestFlaky"]["outcome"], "passed_on_rerun")
        self.assertEqual(by["ex::TestFlaky"]["message"], "Failed")
        self.assertEqual(by["ex::TestOK"]["outcome"], "passed")
        self.assertEqual(len(got), 2)

    def test_duplicate_passes_outside_pytest_are_just_passes(self):
        raw = (b'<testsuites><testsuite name="x">'
               b'<testcase classname="a" name="t"/>'
               b'<testcase classname="a" name="t"/></testsuite></testsuites>')
        self.assertEqual(self.outcomes(raw), {"a::t": "passed"})
        self.assertEqual(
            self.outcomes(raw, duplicate_passes_are_reruns=True),
            {"a::t": "passed_on_rerun"})

    def test_skips_and_surefire_rerun_elements_are_kept(self):
        raw = (b'<testsuite name="x">'
               b'<testcase classname="a" name="s"><skipped/></testcase>'
               b'<testcase classname="a" name="s"><skipped/></testcase>'
               b'<testcase classname="a" name="r"><flakyFailure message="m"/>'
               b'</testcase></testsuite>')
        self.assertEqual(self.outcomes(raw), {"a::s": "skipped",
                                              "a::r": "passed_on_rerun"})

    def test_one_run_of_a_masked_flake_is_proof(self):
        tmp = mktemp()
        path = os.path.join(tmp, "r.xml")
        with open(path, "wb") as f:
            f.write(PYTEST_RERUN_GREEN)
        db = os.path.join(tmp, "d.db")
        self.assertEqual(run_cli("--db", db, "record", "--junit", path,
                                 "--commit", "c")[0], 0)
        a = analyze(db)
        self.assertEqual([s["test_id"] for s in a["flaky"]],
                         ["test_f::test_flaky"])
        self.assertTrue(a["flaky"][0]["proven"])


class TestRecordSeveralReports(unittest.TestCase):
    def write(self, tmp, name, body):
        p = os.path.join(tmp, name)
        with open(p, "wb") as f:
            f.write(body)
        return p

    def shard(self, name):
        return (f'<testsuite name="s"><testcase classname="a" '
                f'name="{name}"/></testsuite>').encode()

    def test_shards_are_one_run(self):
        tmp = mktemp()
        a = self.write(tmp, "a.xml", self.shard("t1"))
        b = self.write(tmp, "b.xml", self.shard("t2"))
        db = os.path.join(tmp, "d.db")
        code, out, _ = run_cli("--db", db, "record", "--junit", a, b,
                               "--commit", "c")
        self.assertEqual(code, 0)
        self.assertIn("recorded run 1: 2 tests", out)
        code, out, _ = run_cli("--db", db, "record", "--junit", b, a,
                               "--commit", "c")
        self.assertIn("already recorded", out)    # order does not matter

    def test_a_pattern_the_shell_left_alone_is_expanded(self):
        tmp = mktemp()
        self.write(tmp, "a.xml", self.shard("t1"))
        self.write(tmp, "b.xml", self.shard("t2"))
        db = os.path.join(tmp, "d.db")
        code, out, _ = run_cli("--db", db, "record", "--junit",
                               os.path.join(tmp, "*.xml"))
        self.assertIn("2 tests", out)

    def test_a_test_in_two_shards_that_failed_then_passed_is_a_rerun(self):
        tmp = mktemp()
        a = self.write(tmp, "a.xml", b'<testsuite name="s"><testcase '
                       b'classname="a" name="t"><failure message="m"/>'
                       b'</testcase></testsuite>')
        b = self.write(tmp, "b.xml", self.shard("t"))
        db = os.path.join(tmp, "d.db")
        code, out, _ = run_cli("--db", db, "record", "--junit", a, b)
        self.assertIn("1 passed only on a retry", out)

    def test_a_missing_file_among_several_is_an_error(self):
        tmp = mktemp()
        a = self.write(tmp, "a.xml", self.shard("t1"))
        code, _, err = run_cli("--db", os.path.join(tmp, "d.db"), "record",
                               "--junit", a, os.path.join(tmp, "nope.xml"))
        self.assertEqual(code, 2)
        self.assertIn("no such file", err)


class TestGovernance(unittest.TestCase):
    def flaky_db(self):
        return history_db("pfpfpfpfpf", tid="t::flaky")

    def test_new_quarantines_get_an_owner_and_an_expiry(self):
        db = self.flaky_db()
        now = int(time.time())
        code, out, _ = run_cli("--db", db, "quarantine", "--format", "json",
                               "--owner", "dana", "--expires-days", "14")
        data = json.loads(out)
        e = data["entries"]["t::flaky"]
        self.assertEqual(e["owner"], "dana")
        self.assertAlmostEqual(e["expires_at"], now + 14 * 86400, delta=60)

    def test_zero_days_never_expires(self):
        code, out, _ = run_cli("--db", self.flaky_db(), "quarantine",
                               "--format", "json", "--expires-days", "0")
        self.assertIsNone(json.loads(out)["entries"]["t::flaky"]["expires_at"])

    def test_bad_days_is_a_usage_error(self):
        with self.assertRaises(SystemExit):
            run_cli("--db", self.flaky_db(), "quarantine", "--expires-days",
                    "-3")

    def test_an_expired_entry_leaves_the_set_and_is_named(self):
        db = self.flaky_db()
        conn = store_mod.connect(db)
        store_mod.quarantine_add(conn, "t::flaky", "auto", owner="dana",
                                 expires_at=int(time.time()) - 5)
        conn.close()
        code, out, err = run_cli("--db", db, "quarantine", "--format",
                                 "json")
        data = json.loads(out)
        self.assertEqual(data["quarantined"], [])
        self.assertEqual(data["expired"], ["t::flaky"])
        self.assertIn("expired", err)
        # and it is not silently re-quarantined for another 30 days
        self.assertEqual(data["new"], [])
        a = analyze(db)
        self.assertFalse(a["flaky"][0]["quarantined"])
        self.assertTrue(a["flaky"][0]["quarantine_expired"])
        code, out, _ = run_cli("--db", db, "report")
        self.assertIn("## Quarantine expired", out)
        self.assertIn("owner dana", out)

    def test_renew_extends_it(self):
        db = self.flaky_db()
        conn = store_mod.connect(db)
        store_mod.quarantine_add(conn, "t::flaky", "auto",
                                 expires_at=int(time.time()) - 5)
        conn.close()
        code, out, _ = run_cli("--db", db, "renew", "--test", "t::flaky",
                               "--days", "7", "--owner", "lee")
        self.assertEqual(code, 0)
        self.assertIn("renewed", out)
        code, out, _ = run_cli("--db", db, "quarantine", "--format", "json")
        data = json.loads(out)
        self.assertEqual(data["quarantined"], ["t::flaky"])
        self.assertEqual(data["entries"]["t::flaky"]["owner"], "lee")
        code, _, err = run_cli("--db", db, "renew", "--test", "t::nope")
        self.assertIn("not in quarantine", err)

    def test_auto_release_frees_the_stable_ones(self):
        db = history_db("pfpfpfpfpf" + "p" * 12, tid="t::fixed",
                        quarantined=True)
        code, out, err = run_cli("--db", db, "quarantine", "--format",
                                 "json", "--auto-release")
        data = json.loads(out)
        self.assertEqual(data["quarantined"], [])
        self.assertIn("released (stable): t::fixed", err)

    def test_old_database_is_migrated_and_keeps_its_entries(self):
        db = os.path.join(mktemp(), "old.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE runs (id INTEGER PRIMARY KEY, commit_sha TEXT, "
            "branch TEXT, created_at INTEGER, report_sha TEXT, env TEXT);"
            "CREATE TABLE results (run_id INTEGER, test_id TEXT, outcome "
            "TEXT, duration_ms INTEGER, message TEXT);"
            "CREATE TABLE quarantine (test_id TEXT PRIMARY KEY, "
            "quarantined_at INTEGER, reason TEXT);"
            "INSERT INTO quarantine VALUES ('t::old', 1, 'auto');")
        conn.commit()
        conn.close()
        conn = store_mod.connect(db)
        (e,) = store_mod.quarantined(conn)
        conn.close()
        self.assertEqual((e["test_id"], e["owner"], e["expires_at"]),
                         ("t::old", None, None))   # never expires

    def test_export_and_import_carry_owner_and_expiry(self):
        tmp = mktemp()
        db = os.path.join(tmp, "a.db")
        conn = store_mod.connect(db)
        store_mod.record_run(conn, [res("t::x", "passed")], commit_sha="c")
        store_mod.quarantine_add(conn, "t::x", "auto", owner="dana",
                                 expires_at=2_000_000_000)
        conn.close()
        out = os.path.join(tmp, "e.json")
        self.assertEqual(run_cli("--db", db, "export", "--out", out)[0], 0)
        db2 = os.path.join(tmp, "b.db")
        self.assertEqual(run_cli("--db", db2, "import", out)[0], 0)
        conn = store_mod.connect(db2)
        (e,) = store_mod.quarantined(conn)
        conn.close()
        self.assertEqual((e["owner"], e["expires_at"]), ("dana", 2_000_000_000))

    def test_import_refuses_a_bad_expiry(self):
        tmp = mktemp()
        bad = os.path.join(tmp, "e.json")
        with open(bad, "w") as f:
            json.dump({"flakebox_export": 1, "runs": [], "quarantine": [
                {"test_id": "t::x", "expires_at": "soon"}]}, f)
        code, _, err = run_cli("--db", os.path.join(tmp, "d.db"), "import",
                               bad)
        self.assertEqual(code, 2)
        self.assertIn("expires_at", err)


class TestPluginIsNeverSilent(unittest.TestCase):
    TEST = ("def test_flaky():\n    assert False, 'flaky'\n\n\n"
            "def test_fine():\n    pass\n")

    def run_pytest(self, tmp):
        env = child_env(PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-p", "flakebox_quarantine", "test_q.py"], cwd=tmp, env=env,
            capture_output=True, text=True, timeout=120)

    def setup_dir(self, entry):
        tmp = mktemp()
        with open(os.path.join(tmp, "test_q.py"), "w") as f:
            f.write(self.TEST)
        quar_mod.write_pytest(tmp, ["test_q::test_flaky"],
                              entries={"test_q::test_flaky": entry})
        return tmp

    def test_the_summary_lists_the_skipped_test_with_owner_and_expiry(self):
        tmp = self.setup_dir({"owner": "dana",
                              "expires_at": int(time.time()) + 5 * 86400})
        proc = self.run_pytest(tmp)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("flakebox quarantine", proc.stdout)
        self.assertIn("were skipped because they are quarantined", proc.stdout)
        self.assertIn("test_q.py::test_flaky (owner dana, expires in 5 day(s))",
                      proc.stdout)

    def test_a_stale_plugin_stops_applying_after_the_expiry(self):
        tmp = self.setup_dir({"owner": "dana",
                              "expires_at": int(time.time()) - 60})
        proc = self.run_pytest(tmp)
        self.assertEqual(proc.returncode, 1, proc.stdout)   # it ran and failed
        self.assertIn("EXPIRED quarantine", proc.stdout)
        self.assertIn("1 failed", proc.stdout)

    def test_without_governance_the_file_is_what_it_was(self):
        tmp = mktemp()
        quar_mod.write_pytest(tmp, ["a::b"])
        with open(os.path.join(tmp, "flakebox_quarantine.json")) as f:
            self.assertEqual(sorted(json.load(f)), ["count", "mode", "tests"])


class TestOtherRunners(unittest.TestCase):
    def test_jest_name_from_jest_junit_ids(self):
        self.assertEqual(quar_mod.jest_name("cart adds::cart adds"),
                         "cart adds")
        self.assertEqual(quar_mod.jest_name("a::b::a::b"), "a::b")
        self.assertEqual(quar_mod.jest_name("Suite::name"), "name")

    def test_jest_pattern_runs_everything_else(self):
        (arg,), left = quar_mod.jest_args(
            ["cart flaky (a+b) [x]::cart flaky (a+b) [x]"])
        self.assertEqual(left, [])
        pattern = arg.split("=", 1)[1]
        # JavaScript and Python agree on this lookahead
        self.assertIsNone(re.search(pattern, "cart flaky (a+b) [x]"))
        self.assertTrue(re.search(pattern, "cart adds item"))
        self.assertTrue(re.search(pattern, "cart flaky (a+b) [x] more"))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_jest_pattern_in_javascript(self):
        (arg,), _ = quar_mod.jest_args(["a (b)::a (b)", "c.d::c.d"])
        pattern = arg.split("=", 1)[1]
        script = ("const p = new RegExp(process.argv[1]);"
                  "console.log(JSON.stringify(['a (b)', 'c.d', 'cxd', "
                  "'a (b) x'].map(n => p.test(n))))")
        out = subprocess.run(["node", "-e", script, pattern],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(out.stdout), [False, False, True, True])

    def test_go_skips_top_level_tests_and_leaves_subtests_running(self):
        args, left = quar_mod.go_args(["ex::TestB", "ex::TestA",
                                       "ex::TestC/case"])
        self.assertEqual(args, ["-skip", "^(?:TestA|TestB)$"])
        self.assertEqual(left, ["ex::TestC/case"])
        self.assertEqual(quar_mod.go_args([]), ([], []))

    @unittest.skipUnless(shutil.which("go"), "go is not installed")
    def test_go_skip_in_the_go_tool(self):
        tmp = mktemp()
        with open(os.path.join(tmp, "go.mod"), "w") as f:
            f.write("module ex\ngo 1.21\n")
        with open(os.path.join(tmp, "a_test.go"), "w") as f:
            f.write('package ex\nimport "testing"\n'
                    "func TestA(t *testing.T){ t.Fatal(\"x\") }\n"
                    "func TestB(t *testing.T){}\n")
        args, _ = quar_mod.go_args(["ex::TestA"])
        env = child_env(GOFLAGS="-mod=mod", GOTOOLCHAIN="local")
        proc = subprocess.run(["go", "test", "-count=1", "-v"] + args
                              + ["./..."], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=120)
        if "-skip" in proc.stderr and "flag provided but not defined" in proc.stderr:
            self.skipTest("this go is older than 1.20 (no -skip)")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("--- PASS: TestB", proc.stdout)
        self.assertNotIn("TestA", proc.stdout.replace("ok", ""))

    def test_maven_excludes_by_class_and_method(self):
        args, left = quar_mod.maven_args(
            ["com.acme.CartTest::checkout", "com.acme.CartTest::pay()",
             "com.acme.T::param[1]", "nopkg"])
        self.assertEqual(args, ["-Dtest=!com.acme.CartTest#checkout,"
                                "!com.acme.CartTest#pay"])
        self.assertEqual(sorted(left), ["com.acme.T::param[1]", "nopkg"])

    def test_the_command_prints_only_the_arguments_on_stdout(self):
        db = history_db("pfpfpfpfpf", tid="com.acme.CartTest::checkout")
        for fmt, want in (("maven", "-Dtest=!com.acme.CartTest#checkout"),
                          ("jest", "--testNamePattern="),
                          ("go", "-skip")):
            code, out, err = run_cli("--db", db, "quarantine", "--format",
                                     fmt)
            self.assertEqual(code, 0, fmt)
            self.assertEqual(len(out.splitlines()), 1, fmt)
            self.assertIn(want, out)
            self.assertIn("in quarantine", err)

    def test_a_hostile_id_is_one_shell_word(self):
        if os.name == "nt":
            self.skipTest("POSIX quoting")
        import shlex
        for fmt in ("jest", "maven"):
            line, _ = quar_mod.runner_line(
                fmt, ["a.B::x$(touch pwned)`id`;'\"\n"])
            self.assertLessEqual(len(shlex.split(line)), 1, fmt)
            if fmt == "jest":
                self.assertEqual(len(shlex.split(line)), 1)


class TestTriage(unittest.TestCase):
    def report(self, *cases):
        body = "".join(
            f'<testcase classname="{c}" name="{n}">'
            + (f'<failure message="{m}"/>' if o == "f" else "")
            + "</testcase>" for c, n, o, m in cases)
        return f"<testsuite name='s'>{body}</testsuite>".encode()

    def build(self):
        db = os.path.join(mktemp(), "h.db")
        conn = store_mod.connect(db)
        for i in range(12):
            rs = [res("a::flaky", "failed" if i % 3 == 0 else "passed",
                      "connection refused"),
                  res("a::solid", "passed"),
                  res("a::broke", "passed" if i < 8 else "failed", "boom")]
            store_mod.record_run(conn, rs, commit_sha=f"c{i}",
                                 created_at=1_700_000_000 + i * 3600)
        store_mod.quarantine_add(conn, "a::flaky", "auto", owner="dana",
                                 expires_at=int(time.time()) + 86400 * 9)
        conn.close()
        return db

    def judge(self, db, cases):
        conn = store_mod.connect(db)
        try:
            from flakebox import junit
            results = junit.parse_bytes(self.report(*cases))
            return triage_mod.triage(conn, results)
        finally:
            conn.close()

    def test_known_flake_new_failure_and_broken(self):
        t = self.judge(self.build(), [
            ("a", "flaky", "f", "connection refused"),
            ("a", "solid", "f", "assert 1 == 2"),
            ("a", "broke", "f", "boom"),
            ("a", "unseen", "f", "new"),
            ("a", "fine", "p", "")])
        by = {f["test_id"]: f for f in t["failures"]}
        self.assertEqual(by["a::flaky"]["verdict"], "known-flake")
        self.assertEqual(by["a::flaky"]["owner"], "dana")
        self.assertIsNotNone(by["a::flaky"]["tracking_since"])
        self.assertEqual(by["a::solid"]["verdict"], "new-failure")
        self.assertEqual(by["a::broke"]["verdict"], "broken")
        self.assertEqual(by["a::unseen"]["verdict"], "new-failure")
        self.assertFalse(t["all_known"])

    def test_all_known_says_rerun_is_reasonable_and_exits_zero(self):
        db = self.build()
        tmp = mktemp()
        path = os.path.join(tmp, "r.xml")
        with open(path, "wb") as f:
            f.write(self.report(("a", "flaky", "f", "connection refused")))
        code, out, _ = run_cli("--db", db, "triage", "--junit", path,
                               "--exit-code")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("<!-- flakebox:triage -->"))
        self.assertIn("Every failure is a known flake", out)
        self.assertIn("owner dana", out)
        self.assertIn("tracking since", out)
        self.assertIn("usually network", out)

    def test_exit_code_one_when_a_failure_is_not_a_known_flake(self):
        db = self.build()
        path = os.path.join(mktemp(), "r.xml")
        with open(path, "wb") as f:
            f.write(self.report(("a", "solid", "f", "x")))
        code, out, _ = run_cli("--db", db, "triage", "--junit", path,
                               "--exit-code")
        self.assertEqual(code, 1)
        self.assertIn("NEW failure, probably real", out)
        # without the flag it only reports
        self.assertEqual(run_cli("--db", db, "triage", "--junit", path)[0], 0)

    def test_triage_never_writes_the_database(self):
        db = self.build()
        before = open(db, "rb").read()
        path = os.path.join(mktemp(), "r.xml")
        with open(path, "wb") as f:
            f.write(self.report(("a", "flaky", "f", "x")))
        run_cli("--db", db, "triage", "--junit", path)
        conn = store_mod.connect(db)
        self.assertEqual(store_mod.run_count(conn), 12)
        conn.close()
        self.assertTrue(before)

    def test_no_database_yet_is_every_failure_is_new_not_an_error(self):
        path = os.path.join(mktemp(), "r.xml")
        with open(path, "wb") as f:
            f.write(self.report(("a", "x", "f", "m")))
        db = os.path.join(mktemp(), "never.db")
        code, out, _ = run_cli("--db", db, "triage", "--junit", path)
        self.assertEqual(code, 0)
        self.assertIn("never seen before", out)
        self.assertFalse(os.path.exists(db))

    def test_a_foreign_database_is_still_refused(self):
        tmp = mktemp()
        db = os.path.join(tmp, "x.db")
        sqlite3.connect(db).execute("CREATE TABLE z (a)").connection.commit()
        path = os.path.join(tmp, "r.xml")
        with open(path, "wb") as f:
            f.write(self.report(("a", "x", "f", "m")))
        code, _, err = run_cli("--db", db, "triage", "--junit", path)
        self.assertEqual(code, 2)
        self.assertIn("not a flakebox database", err)

    def test_no_failures(self):
        path = os.path.join(mktemp(), "r.xml")
        with open(path, "wb") as f:
            f.write(self.report(("a", "x", "p", "")))
        code, out, _ = run_cli("--db", self.build(), "triage", "--junit", path)
        self.assertIn("no failures in this report", out)

    def test_hostile_ids_and_messages_cannot_break_the_comment(self):
        db = self.build()
        path = os.path.join(mktemp(), "r.xml")
        evil = "x|`‮@everyone [a](http://e)"
        with open(path, "wb") as f:
            f.write(self.report(("a", evil, "f", "m [31m\u202e")))
        code, out, _ = run_cli("--db", db, "triage", "--junit", path)
        self.assertEqual(code, 0)
        self.assertNotIn("‮", out)
        rows = [ln for ln in out.splitlines() if ln.startswith("| ") and
                "verdict" not in ln]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].count("|") - rows[0].count("\\|"), 4)

    def test_json_format(self):
        path = os.path.join(mktemp(), "r.xml")
        with open(path, "wb") as f:
            f.write(self.report(("a", "flaky", "f", "x")))
        code, out, _ = run_cli("--db", self.build(), "triage", "--junit",
                               path, "--format", "json")
        data = json.loads(out)
        self.assertEqual(data["failures"][0]["verdict"], "known-flake")

    def test_a_huge_report_is_fast(self):
        cases = [("a", f"t{i}", "f", "m") for i in range(2000)]
        path = os.path.join(mktemp(), "r.xml")
        with open(path, "wb") as f:
            f.write(self.report(*cases))
        start = time.monotonic()
        code, out, _ = run_cli("--db", self.build(), "triage", "--junit", path)
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - start, 20)
        self.assertIn("and 1950 more", out)


class TestBenchmarkFloors(unittest.TestCase):
    def test_the_committed_floors_hold(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from benchmarks import accuracy
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = accuracy.main(["--check"])
        self.assertEqual(rc, 0, buf.getvalue())


class TestActionHasTriage(unittest.TestCase):
    def test_inputs_and_step(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "action.yml"), encoding="utf-8") as f:
            text = f.read()
        for needle in ("  triage:", "  triage-output:",
                       "name: Triage failures (optional)",
                       "if: inputs.triage == 'true'"):
            self.assertIn(needle, text)
        # triage reads the history before this run is recorded
        self.assertLess(text.index("Triage failures"),
                        text.index("name: Record results"))


if __name__ == "__main__":
    tempfile  # noqa: B018
    unittest.main()
