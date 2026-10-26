"""Round 10c: jest/vitest/go emitters, prune, WAL checkpoint, report cap,
HTML trend page."""

import os
import re
import shlex
import sqlite3
import unittest
from unittest import mock

from flakebox import cli as cli_mod
from flakebox import junit as junit_mod
from flakebox import quarantine as quar_mod
from flakebox import store as store_mod

from test_flakebox import make_db, mktemp, run_cli, write_junit


class TestEmitters(unittest.TestCase):
    IDS = ["Suite works fine::Suite works fine", "Suite (odd) [1]::Suite (odd) [1]"]

    def test_jest_pattern_is_a_negative_lookahead_on_the_full_name(self):
        with mock.patch.object(quar_mod.os, "name", "posix"):
            line = quar_mod.jest_line(self.IDS)
        flag, pat = shlex.split(line)
        self.assertEqual(flag, "--testNamePattern")
        rx = re.compile(pat)       # Python re agrees with JS on this subset
        self.assertIsNone(rx.search("Suite works fine"))
        self.assertIsNone(rx.search("Suite (odd) [1]"))
        self.assertIsNotNone(rx.search("Suite works fine too"))
        self.assertIsNotNone(rx.search("Other"))

    def test_vitest_ids_use_the_suite_and_title(self):
        ids = ["src/a.test.ts::Outer > inner test"]
        line = quar_mod.jest_line(ids, vitest=True)
        self.assertTrue(line.startswith("-t "))
        self.assertIn("Outer inner test", shlex.split(line)[1] if os.name != "nt" else line)

    def test_go_skips_top_level_tests_and_reports_subtests(self):
        ids = ["example.com/pkg::TestA", "example.com/pkg::TestB",
               "example.com/pkg::TestC/sub_one"]
        names, subs = quar_mod.go_names(ids)
        self.assertEqual((names, subs), (["TestA", "TestB"], 1))
        with mock.patch.object(quar_mod.os, "name", "posix"):
            self.assertEqual(shlex.split(quar_mod.go_line(ids)),
                             ["-skip", "^(TestA|TestB)$"])

    def test_nothing_quarantined_gives_no_option(self):
        self.assertEqual(quar_mod.jest_line([]), "")
        self.assertEqual(quar_mod.go_line([]), "")

    def test_cli_formats(self):
        tmp = mktemp()
        db = make_db(tmp)
        for fmt in ("jest", "vitest", "go"):
            code, out, err = run_cli("--db", db, "quarantine", "--format", fmt)
            self.assertEqual(code, 0)
            self.assertIn("quarantined", err)
            self.assertTrue(out.strip(), fmt)
            self.assertNotIn("quarantined", out)


class TestPruneAndWal(unittest.TestCase):
    def test_prune_keeps_the_newest_runs_and_the_quarantine(self):
        tmp = mktemp()
        db = make_db(tmp, runs=6)
        run_cli("--db", db, "quarantine", "--format", "json")
        code, out, _ = run_cli("--db", db, "prune", "--keep-runs", "2")
        self.assertEqual(code, 0)
        self.assertIn("pruned 4 run(s)", out)
        conn = store_mod.connect(db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM results WHERE run_id NOT IN "
                "(SELECT id FROM runs)").fetchone()[0], 0)
            self.assertEqual(len(store_mod.quarantined(conn)), 1)
        finally:
            conn.close()

    def test_keep_runs_must_be_positive(self):
        with self.assertRaises(SystemExit):
            run_cli("prune", "--keep-runs", "0")

    def test_close_folds_the_wal_into_the_database(self):
        tmp = mktemp()
        db = make_db(tmp)
        wal = db + "-wal"
        self.assertTrue(not os.path.exists(wal) or os.path.getsize(wal) == 0)
        # the main file alone holds every run
        copy = os.path.join(tmp, "copy.db")
        with open(db, "rb") as a, open(copy, "wb") as b:
            b.write(a.read())
        c = sqlite3.connect(copy)
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 6)
        finally:
            c.close()

    def test_memory_databases_still_work(self):
        conn = store_mod.connect(":memory:")
        conn.close()


class TestReportCap(unittest.TestCase):
    def test_a_report_over_the_cap_is_refused(self):
        tmp = mktemp()
        p = write_junit(tmp, True)
        with self.assertRaises(ValueError):
            junit_mod.read(p, 10)
        self.assertTrue(junit_mod.read(p, 10 ** 6))

    def test_cli_refuses_and_names_the_option(self):
        tmp = mktemp()
        p = write_junit(tmp, True)
        code, out, err = run_cli("--db", os.path.join(tmp, "x.db"), "record",
                                 "--junit", p, "--max-report-mb", "0.0001")
        self.assertEqual(code, 2)
        self.assertIn("--max-report-mb", err)

    def test_zero_means_no_limit(self):
        tmp = mktemp()
        p = write_junit(tmp, True)
        code, _, _ = run_cli("--db", os.path.join(tmp, "x.db"), "record",
                             "--junit", p, "--max-report-mb", "0")
        self.assertEqual(code, 0)


class TestHtml(unittest.TestCase):
    def test_page_is_static_and_escaped(self):
        tmp = mktemp()
        db = make_db(tmp)
        code, out, _ = run_cli("--db", db, "report", "--format", "html")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("<!doctype html>"))
        self.assertIn("test_flaky", out)
        self.assertNotIn("<script", out)
        self.assertNotIn("http", out)

    def test_hostile_test_ids_are_escaped(self):
        tmp = mktemp()
        db = os.path.join(tmp, "h.db")
        conn = store_mod.connect(db)
        bad = "x::<img src=x onerror=alert(1)>"
        for i in range(6):
            store_mod.record_run(conn, [
                {"test_id": bad, "outcome": "failed" if i % 2 else "passed",
                 "duration_ms": 1, "message": ""}], commit_sha=f"s{i}")
        conn.close()
        _, out, _ = run_cli("--db", db, "report", "--format", "html")
        self.assertNotIn("<img", out)
        self.assertIn("&lt;img", out)


if __name__ == "__main__":
    unittest.main()


class TestReadmeExamples(unittest.TestCase):
    def test_the_workflow_example_uses_the_real_marker(self):
        from flakebox import summary as summary_mod
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "README.md")
        if not os.path.exists(path):
            self.skipTest("not a source checkout")
        with open(path, encoding="utf-8") as f:
            self.assertIn(f'startswith("{summary_mod.MARKER}")', f.read())
