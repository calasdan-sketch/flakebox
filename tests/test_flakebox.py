"""Flakebox tests: synthetic JUnit XML, no network."""

import io
import json
import os
import random
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from flakebox import cli as cli_mod
from flakebox import detect as detect_mod
from flakebox import junit as junit_mod
from flakebox import quarantine as quar_mod
from flakebox import store as store_mod

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="4">
  <testcase classname="tests.test_a.TestA" name="test_stable" time="0.1"/>
  <testcase classname="tests.test_a.TestA" name="test_flaky" time="0.2">
    <failure message="assert False">boom</failure>
  </testcase>
  <testcase classname="tests.test_b" name="test_broken" time="0.1">
    <error message="setup failed">kaput</error>
  </testcase>
  <testcase classname="tests.test_b" name="test_skip" time="0.0">
    <skipped/>
  </testcase>
</testsuite>"""


def write_junit(tmp: str, fail_flaky: bool) -> str:
    xml = JUNIT if fail_flaky else JUNIT.replace(
        '<failure message="assert False">boom</failure>', "")
    p = os.path.join(tmp, "junit.xml")
    with open(p, "w") as f:
        f.write(xml)
    return p


def make_db(tmp: str, flaky_fails: int = 3, runs: int = 6) -> str:
    """DB where test_flaky fails `flaky_fails` of `runs` runs,
    test_broken always errors, test_stable always passes."""
    db = os.path.join(tmp, "history.db")
    conn = store_mod.connect(db)
    for i in range(runs):
        results = [
            {"test_id": "tests.test_a.TestA::test_stable",
             "outcome": "passed", "duration_ms": 100, "message": ""},
            {"test_id": "tests.test_a.TestA::test_flaky",
             "outcome": "failed" if i < flaky_fails else "passed",
             "duration_ms": 200, "message": ""},
            {"test_id": "tests.test_b::test_broken",
             "outcome": "error", "duration_ms": 100, "message": ""},
        ]
        store_mod.record_run(conn, results, commit_sha=f"sha{i}")
    conn.close()
    return db


class TestJunit(unittest.TestCase):
    def test_parse(self):
        tmp = tempfile.mkdtemp()
        p = write_junit(tmp, True)
        rs = junit_mod.parse(p)
        by_id = {r["test_id"]: r for r in rs}
        self.assertEqual(len(rs), 4)
        self.assertEqual(by_id["tests.test_a.TestA::test_stable"]["outcome"],
                         "passed")
        self.assertEqual(by_id["tests.test_a.TestA::test_flaky"]["outcome"],
                         "failed")
        self.assertEqual(by_id["tests.test_b::test_broken"]["outcome"],
                         "error")
        self.assertEqual(by_id["tests.test_b::test_skip"]["outcome"],
                         "skipped")

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            junit_mod.parse("/nope/x.xml")


class TestDetect(unittest.TestCase):
    def test_flaky_vs_broken(self):
        db = make_db(tempfile.mkdtemp())
        conn = store_mod.connect(db)
        a = detect_mod.analyze(conn, min_runs=5)
        flaky_ids = [s["test_id"] for s in a["flaky"]]
        broken_ids = [s["test_id"] for s in a["broken"]]
        self.assertIn("tests.test_a.TestA::test_flaky", flaky_ids)
        self.assertIn("tests.test_b::test_broken", broken_ids)
        self.assertNotIn("tests.test_a.TestA::test_stable", flaky_ids)
        s = next(x for x in a["flaky"]
                 if x["test_id"] == "tests.test_a.TestA::test_flaky")
        self.assertAlmostEqual(s["flake_rate"], 0.5)

    def test_min_runs_gates(self):
        db = make_db(tempfile.mkdtemp(), runs=3)
        conn = store_mod.connect(db)
        a = detect_mod.analyze(conn, min_runs=5)
        self.assertEqual(a["flaky"], [])

    def test_stable_unquarantine(self):
        tmp = tempfile.mkdtemp()
        db = make_db(tmp, flaky_fails=2, runs=12)
        conn = store_mod.connect(db)
        store_mod.quarantine_add(conn, "tests.test_a.TestA::test_flaky",
                                 "auto")
        a = detect_mod.analyze(conn, min_runs=5, stable_runs=10)
        stable_ids = [s["test_id"] for s in a["stable"]]
        self.assertIn("tests.test_a.TestA::test_flaky", stable_ids)


class TestQuarantineFiles(unittest.TestCase):
    def test_pytest_plugin(self):
        tmp = tempfile.mkdtemp()
        written = quar_mod.write_pytest(
            tmp, ["tests.test_a.TestA::test_flaky"])
        self.assertEqual(len(written), 2)
        with open(os.path.join(tmp, "flakebox_quarantine.json")) as f:
            data = json.load(f)
        self.assertEqual(data["tests"], ["tests.test_a.TestA::test_flaky"])
        with open(os.path.join(tmp, "flakebox_quarantine.py")) as f:
            src = f.read()
        self.assertIn("pytest_collection_modifyitems", src)
        # the plugin must be valid python
        compile(src, "flakebox_quarantine.py", "exec")

    def test_deselect_args(self):
        args = quar_mod.deselect_args(["b::t2", "a::t1"])
        self.assertEqual(args, ["--deselect", "a::t1", "--deselect", "b::t2"])

    def test_matches_id_formats(self):
        # exec the REAL generated plugin against a real JSON file
        tmp = tempfile.mkdtemp()
        quar_mod.write_pytest(tmp, ["test_demo.TestDemo::test_flaky"])
        plugin = os.path.join(tmp, "flakebox_quarantine.py")
        ns: dict = {"__file__": plugin}
        exec(compile(open(plugin).read(), plugin, "exec"), ns)
        m = ns["_matches"]
        # JUnit-style vs pytest nodeid
        self.assertTrue(m("test_demo.py::TestDemo::test_flaky",
                          "test_demo.TestDemo::test_flaky"))
        self.assertTrue(m("tests/test_a.py::TestA::test_x",
                          "tests.test_a.TestA::test_x"))
        self.assertFalse(m("test_demo.py::TestDemo::test_other",
                           "test_demo.TestDemo::test_flaky"))
        self.assertFalse(m("test_demo.py::TestDemo::test_flaky",
                           "test_demo.TestDemo::test_other"))


class TestCLI(unittest.TestCase):
    def test_record_report_roundtrip(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "h.db")
        for i in range(6):
            p = write_junit(tmp, fail_flaky=(i < 3))
            code = cli_mod.main(["--db", db, "record", "--junit", p,
                                 "--commit", f"sha{i}"])
            self.assertEqual(code, 0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_mod.main(["--db", db, "report"])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("test_flaky", out)
        self.assertIn("Flaky", out)

    def test_report_json(self):
        db = make_db(tempfile.mkdtemp())
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_mod.main(["--db", db, "report", "--format", "json"])
        data = json.loads(buf.getvalue())
        self.assertEqual(code, 0)
        self.assertIn("flaky", data)
        self.assertIn("broken", data)
        self.assertIn("stable", data)

    def test_quarantine_pytest(self):
        tmp = tempfile.mkdtemp()
        db = make_db(tmp)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_mod.main(["--db", db, "quarantine",
                                 "--format", "pytest", "-o", tmp])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(
            os.path.join(tmp, "flakebox_quarantine.py")))
        conn = store_mod.connect(db)
        self.assertTrue(store_mod.is_quarantined(
            conn, "tests.test_a.TestA::test_flaky"))

    def test_unquarantine(self):
        db = make_db(tempfile.mkdtemp())
        conn = store_mod.connect(db)
        store_mod.quarantine_add(conn, "tests.test_a.TestA::test_flaky")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_mod.main(
                ["--db", db, "unquarantine",
                 "--test", "tests.test_a.TestA::test_flaky"])
        self.assertEqual(code, 0)
        self.assertFalse(store_mod.is_quarantined(
            conn, "tests.test_a.TestA::test_flaky"))

    def test_history(self):
        db = make_db(tempfile.mkdtemp())
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_mod.main(["--db", db, "history",
                                 "--test", "tests.test_a.TestA::test_flaky"])
        self.assertEqual(code, 0)
        self.assertIn("flake rate 50%", buf.getvalue())

    def test_record_missing(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_mod.main(["--db", ":memory:", "record",
                                 "--junit", "/nope.xml"])
        self.assertEqual(code, 2)


class TestActionYml(unittest.TestCase):
    def test_valid_yaml(self):
        import yaml  # may not exist; fall back to a structural check
        path = os.path.join(os.path.dirname(__file__), "..", "action.yml")
        with open(path) as f:
            text = f.read()
        try:
            data = yaml.safe_load(text)
        except ImportError:
            self.assertIn("actions/cache/restore@v4", text)
            self.assertIn("GITHUB_STEP_SUMMARY", text)
            return
        self.assertEqual(data["runs"]["using"], "composite")
        self.assertIn("junit", data["inputs"])


# ---------------------------------------------------------------------------
# Regression tests for the v1 fix batch.
# ---------------------------------------------------------------------------

DOCTYPE_BOMB = """<?xml version="1.0"?>
<!DOCTYPE testsuite [
  <!ENTITY a "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
]>
<testsuite name="pytest" tests="1">
  <testcase classname="a" name="t" time="0.1">&b;</testcase>
</testsuite>"""

ENTITY_DECL = """<?xml version="1.0"?>
<testsuite name="pytest" tests="1">
  <!ENTITY lol "lollollollol">
  <testcase classname="a" name="t" time="0.1"/>
</testsuite>"""

LOWERCASE_DOCTYPE = """<?xml version="1.0"?>
<!doctype testsuite [<!entity x "y">]>
<testsuite name="pytest" tests="1">
  <testcase classname="a" name="t" time="0.1"/>
</testsuite>"""


def write_raw(tmp: str, name: str, content: str) -> str:
    p = os.path.join(tmp, name)
    with open(p, "w") as f:
        f.write(content)
    return p


class TestXmlBombGuard(unittest.TestCase):
    def test_rejects_doctype_bomb(self):
        p = write_raw(tempfile.mkdtemp(), "bomb.xml", DOCTYPE_BOMB)
        with self.assertRaisesRegex(ValueError, "DOCTYPE"):
            junit_mod.parse(p)

    def test_rejects_entity_declaration(self):
        p = write_raw(tempfile.mkdtemp(), "entity.xml", ENTITY_DECL)
        with self.assertRaisesRegex(ValueError, "ENTITY"):
            junit_mod.parse(p)

    def test_rejection_is_case_insensitive(self):
        p = write_raw(tempfile.mkdtemp(), "lower.xml", LOWERCASE_DOCTYPE)
        with self.assertRaises(ValueError):
            junit_mod.parse(p)

    def test_cli_record_rejects_bomb_with_exit_2(self):
        tmp = tempfile.mkdtemp()
        p = write_raw(tmp, "bomb.xml", DOCTYPE_BOMB)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_mod.main(["--db", os.path.join(tmp, "h.db"),
                                 "record", "--junit", p])
        self.assertEqual(code, 2)
        self.assertIn("DOCTYPE", err.getvalue())

    def test_legit_xml_still_parses(self):
        tmp = tempfile.mkdtemp()
        p = write_junit(tmp, True)
        rs = junit_mod.parse(p)
        self.assertEqual(len(rs), 4)


class TestPackaging(unittest.TestCase):
    def test_pyproject_metadata(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib unavailable (needs Python 3.11+)")
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "pyproject.toml"), "rb") as f:
            data = tomllib.load(f)
        proj = data["project"]
        self.assertEqual(proj["name"], "flakebox")
        self.assertEqual(proj["version"], cli_mod.VERSION)
        self.assertEqual(proj["scripts"]["flakebox"], "flakebox.cli:main")
        self.assertTrue(os.path.exists(os.path.join(root, proj["readme"])))

    def test_entry_point_resolves(self):
        import importlib
        module_name, attr = "flakebox.cli", "main"
        target = getattr(importlib.import_module(module_name), attr)
        self.assertTrue(callable(target))
        # the exact [project.scripts] target, spelled out
        self.assertIs(target, cli_mod.main)


class TestActionCacheKeys(unittest.TestCase):
    def _text(self) -> str:
        path = os.path.join(os.path.dirname(__file__), "..", "action.yml")
        with open(path) as f:
            return f.read()

    def test_keys_are_per_job(self):
        text = self._text()
        # both restore and save cache keys must scope by job
        self.assertIn("${{ github.job }}", text)
        restore = text.index("Restore flakebox history")
        save = text.index("Save flakebox history")
        self.assertIn("github.job", text[restore:save])
        self.assertIn("github.job", text[save:])

    def test_cache_race_is_documented_honestly(self):
        text = self._text().lower()
        # documents the limitation and the aggregator pattern
        self.assertIn("aggregator", text)
        # must NOT invent a merge command: say plainly none exists
        self.assertIn("no merge command", text)
        # structural bits the action needs still present
        self.assertIn("GITHUB_STEP_SUMMARY", self._text())


# ---------------------------------------------------------------------------
# Regression tests for the Round 2 fix batch.
# ---------------------------------------------------------------------------


def exec_plugin_matches(tmp: str):
    """Load _matches from the REAL generated plugin in `tmp`."""
    quar_mod.write_pytest(tmp, ["placeholder::x"])
    plugin = os.path.join(tmp, "flakebox_quarantine.py")
    ns: dict = {"__file__": plugin}
    exec(compile(open(plugin).read(), plugin, "exec"), ns)
    return ns["_matches"]


class TestQuarantineMatchesClassless(unittest.TestCase):
    """FIX A: classless node IDs (test_mod.py::test_x) must match."""

    def test_classless_matches_both_directions(self):
        m = exec_plugin_matches(tempfile.mkdtemp())
        self.assertTrue(m("test_mod.py::test_x", "test_mod::test_x"))
        self.assertTrue(m("test_mod::test_x", "test_mod.py::test_x"))

    def test_classless_matches_dotted_junit_variant(self):
        m = exec_plugin_matches(tempfile.mkdtemp())
        self.assertTrue(m("tests/test_mod.py::test_x",
                          "pkg.test_mod::test_x"))
        self.assertTrue(m("pkg.test_mod::test_x",
                          "tests/test_mod.py::test_x"))

    def test_class_based_still_matches(self):
        m = exec_plugin_matches(tempfile.mkdtemp())
        self.assertTrue(m("test_demo.py::TestDemo::test_flaky",
                          "test_demo.TestDemo::test_flaky"))
        self.assertTrue(m("tests/test_a.py::TestA::test_x",
                          "tests.test_a.TestA::test_x"))

    def test_non_matching_names_still_miss(self):
        m = exec_plugin_matches(tempfile.mkdtemp())
        self.assertFalse(m("test_mod.py::test_x", "test_mod::test_y"))
        self.assertFalse(m("test_mod.py::test_x", "other_mod::test_x"))
        self.assertFalse(m("test_mod.py::TestA::test_x",
                           "test_mod.TestB::test_x"))
        self.assertFalse(m("test_x", "test_x"))  # too short: needs 2 segs


class TestStorePragmas(unittest.TestCase):
    """FIX B: concurrent-writer pragmas are set on every connect."""

    def test_busy_timeout_and_wal(self):
        db = os.path.join(tempfile.mkdtemp(), "h.db")
        conn = store_mod.connect(db)
        try:
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(timeout, 5000)
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(journal, "wal")
        finally:
            conn.close()

    def test_pragmas_survive_reconnect(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "h.db")
        store_mod.connect(db).close()
        conn = store_mod.connect(db)
        try:
            self.assertEqual(
                conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(
                conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()


def reference_analyze(conn, min_runs=5, stable_runs=10, window=50):
    """The pre-FIX-C per-test (N+1) logic, kept here as a test oracle."""
    flaky, broken, stable = [], [], []
    for tid in store_mod.all_test_ids(conn):
        hist = store_mod.test_history(conn, tid, limit=window)[::-1]
        passes = sum(1 for h in hist if h["outcome"] == "passed")
        fails = sum(1 for h in hist if h["outcome"] in ("failed", "error"))
        skips = sum(1 for h in hist if h["outcome"] == "skipped")
        streak = 0
        for h in reversed(hist):
            if h["outcome"] == "passed":
                streak += 1
            else:
                break
        total = passes + fails
        s = {"test_id": tid, "runs": len(hist), "passes": passes,
             "failures": fails, "skips": skips,
             "flake_rate": (fails / total) if total else 0.0,
             "pass_streak": streak,
             "quarantined": store_mod.is_quarantined(conn, tid)}
        if s["runs"] < min_runs:
            continue
        if s["failures"] >= 1 and s["passes"] >= 1:
            flaky.append(s)
        elif s["passes"] == 0 and s["failures"] >= min_runs:
            broken.append(s)
        if s["quarantined"] and s["pass_streak"] >= stable_runs:
            stable.append(s)
    flaky.sort(key=lambda s: (-s["flake_rate"], -s["failures"]))
    broken.sort(key=lambda s: -s["failures"])
    return {"flaky": flaky, "broken": broken, "stable": stable}


def make_equivalence_db(tmp: str) -> str:
    """~3.6k rows of seeded-random history incl. quarantined, flaky,
    broken, skip-heavy, and late-joiner (< min_runs) tests."""
    rng = random.Random(20261015)
    db = os.path.join(tmp, "eq.db")
    conn = store_mod.connect(db)
    n_tests, n_runs = 150, 24
    kinds = []
    for _ in range(n_tests):
        r = rng.random()
        kinds.append("pass" if r < 0.4 else "fail" if r < 0.6 else
                     "flaky" if r < 0.9 else "skippy")
    for run in range(n_runs):
        results = []
        for i, kind in enumerate(kinds):
            tid = f"mod{i:03d}::test_{i:03d}"
            if i >= 145 and run < n_runs - 3:
                continue  # late joiners: only the last 3 runs
            if kind == "pass":
                outcome = "passed"
            elif kind == "fail":
                outcome = rng.choice(["failed", "error"])
            elif kind == "flaky":
                outcome = rng.choices(
                    ["passed", "failed", "error", "skipped"],
                    weights=[55, 25, 5, 15])[0]
            else:
                outcome = rng.choice(["passed", "skipped"])
            results.append({"test_id": tid, "outcome": outcome,
                            "duration_ms": 10, "message": ""})
        store_mod.record_run(conn, results, commit_sha=f"sha{run}")
    # quarantine a spread across kinds
    for i in (3, 37, 66, 99, 121, 130, 140):
        store_mod.quarantine_add(conn, f"mod{i:03d}::test_{i:03d}", "auto")
    conn.close()
    return db


class TestAnalyzeBatchEquivalence(unittest.TestCase):
    """FIX C: batched analyze() must equal the old N+1 logic exactly."""

    KW = dict(min_runs=5, stable_runs=5, window=10)  # window < runs on purpose

    def test_identical_results_on_synthetic_dataset(self):
        db = make_equivalence_db(tempfile.mkdtemp())
        conn = store_mod.connect(db)
        try:
            new = detect_mod.analyze(conn, **self.KW)
            old = reference_analyze(conn, **self.KW)
            self.assertEqual(new, old)
            # sanity: the fixture actually exercises every bucket
            self.assertTrue(new["flaky"])
            self.assertTrue(new["broken"])
        finally:
            conn.close()

    def test_known_classifications(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "k.db")
        conn = store_mod.connect(db)
        for i in range(6):
            store_mod.record_run(conn, [
                {"test_id": "a::flaky",
                 "outcome": "passed" if i % 3 else "failed",
                 "duration_ms": 1, "message": ""},
                {"test_id": "a::broken", "outcome": "error",
                 "duration_ms": 1, "message": ""},
                {"test_id": "a::good", "outcome": "passed",
                 "duration_ms": 1, "message": ""},
            ], commit_sha=f"s{i}")
        store_mod.quarantine_add(conn, "a::flaky", "auto")
        a = detect_mod.analyze(conn, min_runs=5, stable_runs=10, window=50)
        self.assertEqual([s["test_id"] for s in a["flaky"]], ["a::flaky"])
        self.assertEqual([s["test_id"] for s in a["broken"]], ["a::broken"])
        self.assertEqual(a["stable"], [])  # streak 2 < stable_runs 10
        s = a["flaky"][0]
        self.assertEqual((s["passes"], s["failures"], s["runs"]), (4, 2, 6))
        self.assertAlmostEqual(s["flake_rate"], 2 / 6)
        self.assertEqual(s["pass_streak"], 2)
        self.assertTrue(s["quarantined"])
        conn.close()

    def test_query_count_is_constant(self):
        db = make_equivalence_db(tempfile.mkdtemp())
        conn = store_mod.connect(db)
        calls: list = []
        conn.set_trace_callback(calls.append)
        try:
            detect_mod.analyze(conn, **self.KW)
        finally:
            conn.set_trace_callback(None)
            conn.close()
        # 1 batched history query + 1 quarantine-set query
        self.assertLessEqual(len(calls), 3,
                             f"expected ~2 queries, got {len(calls)}")

    def test_fewer_queries_than_old_logic(self):
        db = make_equivalence_db(tempfile.mkdtemp())
        conn = store_mod.connect(db)
        new_calls, old_calls = [], []
        conn.set_trace_callback(new_calls.append)
        detect_mod.analyze(conn, **self.KW)
        conn.set_trace_callback(old_calls.append)
        reference_analyze(conn, **self.KW)
        conn.set_trace_callback(None)
        conn.close()
        self.assertLess(len(new_calls), len(old_calls),
                        f"new={len(new_calls)} old={len(old_calls)}")


class TestVersionFlag(unittest.TestCase):
    def test_version_flag(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                cli_mod.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(cli_mod.VERSION, buf.getvalue())
        self.assertIn("flakebox", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
