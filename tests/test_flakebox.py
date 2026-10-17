"""Flakebox tests: synthetic JUnit XML, no network, stdlib only.

Portable by construction: no PyYAML (action.yml is read with a tiny flat
reader below), every text file is opened with an explicit encoding,
HOME/USERPROFILE/APPDATA point at a throwaway directory for the whole
module, and temp dirs are removed with a cleanup that tolerates
read-only or still-open files (Windows).
"""

import argparse
import io
import json
import os
import random
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from flakebox import cli as cli_mod
from flakebox import detect as detect_mod
from flakebox import junit as junit_mod
from flakebox import quarantine as quar_mod
from flakebox import store as store_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------------
# environment isolation (module-wide)
# --------------------------------------------------------------------------

_ENV_VARS = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
             "XDG_CONFIG_HOME", "XDG_DATA_HOME")
_saved_env: dict = {}
_fake_home = ""
_tmp_dirs: list = []


def _rmtree(path: str) -> None:
    """rmtree that copes with read-only files and, on Windows, files a
    SQLite handle still holds open (left behind, never a test failure)."""
    def _onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda f, p, e: _onerror(f, p, e))
    else:
        shutil.rmtree(path, onerror=_onerror)


def setUpModule():
    global _fake_home
    _fake_home = tempfile.mkdtemp(prefix="fbh")
    for var in _ENV_VARS:
        _saved_env[var] = os.environ.get(var)
        os.environ[var] = _fake_home


def tearDownModule():
    for var, old in _saved_env.items():
        if old is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = old
    for d in _tmp_dirs:
        _rmtree(d)
    _rmtree(_fake_home)


def mktemp() -> str:
    d = tempfile.mkdtemp(prefix="fb")
    _tmp_dirs.append(d)
    return d


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

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
    with open(p, "w", encoding="utf-8") as f:
        f.write(xml)
    return p


def result(test_id: str, outcome: str) -> dict:
    return {"test_id": test_id, "outcome": outcome, "duration_ms": 1,
            "message": ""}


def make_db(tmp: str, flaky_fails: int = 3, runs: int = 6) -> str:
    """DB where test_flaky fails `flaky_fails` of `runs` runs,
    test_broken always errors, test_stable always passes."""
    db = os.path.join(tmp, "history.db")
    conn = store_mod.connect(db)
    for i in range(runs):
        results = [
            result("tests.test_a.TestA::test_stable", "passed"),
            result("tests.test_a.TestA::test_flaky",
                   "failed" if i < flaky_fails else "passed"),
            result("tests.test_b::test_broken", "error"),
        ]
        store_mod.record_run(conn, results, commit_sha=f"sha{i}")
    conn.close()
    return db


def db_from_outcomes(tmp: str, series: dict) -> str:
    """series: {test_id: [outcome, ...]} oldest first (None = absent)."""
    db = os.path.join(tmp, "s.db")
    conn = store_mod.connect(db)
    n = max(len(v) for v in series.values())
    for i in range(n):
        rs = [result(tid, outs[i]) for tid, outs in series.items()
              if i < len(outs) and outs[i] is not None]
        store_mod.record_run(conn, rs, commit_sha=f"s{i}")
    conn.close()
    return db


def run_cli(*argv) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli_mod.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestJunit(unittest.TestCase):
    def test_parse(self):
        tmp = mktemp()
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
            junit_mod.parse(os.path.join(mktemp(), "nope", "x.xml"))


class TestDetect(unittest.TestCase):
    def test_flaky_vs_broken(self):
        db = make_db(mktemp())
        conn = store_mod.connect(db)
        a = detect_mod.analyze(conn, min_runs=5)
        conn.close()
        flaky_ids = [s["test_id"] for s in a["flaky"]]
        broken_ids = [s["test_id"] for s in a["broken"]]
        self.assertIn("tests.test_a.TestA::test_flaky", flaky_ids)
        self.assertIn("tests.test_b::test_broken", broken_ids)
        self.assertNotIn("tests.test_a.TestA::test_stable", flaky_ids)
        s = next(x for x in a["flaky"]
                 if x["test_id"] == "tests.test_a.TestA::test_flaky")
        self.assertAlmostEqual(s["flake_rate"], 0.5)

    def test_min_runs_gates(self):
        db = make_db(mktemp(), runs=3)
        conn = store_mod.connect(db)
        a = detect_mod.analyze(conn, min_runs=5)
        conn.close()
        self.assertEqual(a["flaky"], [])

    def test_stable_unquarantine(self):
        tmp = mktemp()
        db = make_db(tmp, flaky_fails=2, runs=12)
        conn = store_mod.connect(db)
        store_mod.quarantine_add(conn, "tests.test_a.TestA::test_flaky",
                                 "auto")
        a = detect_mod.analyze(conn, min_runs=5, stable_runs=10)
        conn.close()
        stable_ids = [s["test_id"] for s in a["stable"]]
        self.assertIn("tests.test_a.TestA::test_flaky", stable_ids)


class TestQuarantineFiles(unittest.TestCase):
    def test_pytest_plugin(self):
        tmp = mktemp()
        written = quar_mod.write_pytest(
            tmp, ["tests.test_a.TestA::test_flaky"])
        self.assertEqual(len(written), 2)
        with open(os.path.join(tmp, "flakebox_quarantine.json"),
                  encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["tests"], ["tests.test_a.TestA::test_flaky"])
        self.assertEqual(data["mode"], "skip")
        with open(os.path.join(tmp, "flakebox_quarantine.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("pytest_collection_modifyitems", src)
        # the plugin must be valid python
        compile(src, "flakebox_quarantine.py", "exec")

    def test_deselect_args(self):
        # JUnit ids are mapped to pytest node ids (module -> file.py)
        args = quar_mod.deselect_args(["b::t2", "a::t1", "a::t1"])
        self.assertEqual(args, ["--deselect", "a.py::t1", "--deselect", "b.py::t2"])

    def test_matches_id_formats(self):
        m = exec_plugin(mktemp(), ["test_demo.TestDemo::test_flaky"])["_matches"]
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
        tmp = mktemp()
        db = os.path.join(tmp, "h.db")
        for i in range(6):
            p = write_junit(tmp, fail_flaky=(i < 3))
            code, _, _ = run_cli("--db", db, "record", "--junit", p,
                                 "--commit", f"sha{i}")
            self.assertEqual(code, 0)
        code, out, _ = run_cli("--db", db, "report")
        self.assertEqual(code, 0)
        self.assertIn("test_flaky", out)
        self.assertIn("Flaky", out)

    def test_report_json(self):
        db = make_db(mktemp())
        code, out, _ = run_cli("--db", db, "report", "--format", "json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertIn("flaky", data)
        self.assertIn("broken", data)
        self.assertIn("stable", data)

    def test_quarantine_pytest(self):
        tmp = mktemp()
        db = make_db(tmp)
        code, _, _ = run_cli("--db", db, "quarantine",
                             "--format", "pytest", "-o", tmp)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(
            os.path.join(tmp, "flakebox_quarantine.py")))
        conn = store_mod.connect(db)
        self.assertTrue(store_mod.is_quarantined(
            conn, "tests.test_a.TestA::test_flaky"))
        conn.close()

    def test_unquarantine(self):
        db = make_db(mktemp())
        conn = store_mod.connect(db)
        store_mod.quarantine_add(conn, "tests.test_a.TestA::test_flaky")
        code, _, _ = run_cli("--db", db, "unquarantine",
                             "--test", "tests.test_a.TestA::test_flaky")
        self.assertEqual(code, 0)
        self.assertFalse(store_mod.is_quarantined(
            conn, "tests.test_a.TestA::test_flaky"))
        conn.close()

    def test_history(self):
        db = make_db(mktemp())
        code, out, _ = run_cli("--db", db, "history",
                               "--test", "tests.test_a.TestA::test_flaky")
        self.assertEqual(code, 0)
        self.assertIn("flaky", out)
        self.assertIn("flake rate 50%", out)

    def test_record_missing(self):
        code, _, _ = run_cli("--db", ":memory:", "record",
                             "--junit", os.path.join(mktemp(), "nope.xml"))
        self.assertEqual(code, 2)


# --------------------------------------------------------------------------
# action.yml (panel finding 1) - read with a minimal flat-YAML reader, no
# PyYAML anywhere in this repo
# --------------------------------------------------------------------------

def flat_yaml(text: str) -> dict:
    """Just enough YAML for action.yml: nested mappings by 2-space
    indentation, scalars, block scalars (>- / |) and comments. Lists of
    steps are kept as raw text under their key."""
    root: dict = {}
    stack = [(-1, root)]
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        body = raw.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if body.startswith("- "):
            parent.setdefault("_items", []).append(body[2:])
            continue
        key, _, val = body.partition(":")
        key, val = key.strip().strip('"'), val.strip()
        if val in (">-", ">", "|", "|-"):
            block = []
            while i < len(lines) and (not lines[i].strip() or
                                      len(lines[i]) - len(lines[i].lstrip(" ")) > indent):
                block.append(lines[i].strip())
                i += 1
            parent[key] = " ".join(b for b in block if b)
        elif val == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = val.strip('"').strip("'")
    return root


def action_text() -> str:
    with open(os.path.join(REPO, "action.yml"), encoding="utf-8") as f:
        return f.read()


class TestActionYml(unittest.TestCase):
    def test_structure(self):
        data = flat_yaml(action_text())
        self.assertEqual(data["runs"]["using"], "composite")
        self.assertIn("junit", data["inputs"])
        self.assertEqual(data["inputs"]["junit"]["required"], "true")
        self.assertEqual(data["inputs"]["min-runs"]["default"], "5")

    def test_every_referenced_input_is_declared(self):
        text = action_text()
        declared = set(flat_yaml(text)["inputs"])
        # both `${{ inputs.x }}` and bare `inputs.x` inside an `if:`
        referenced = set(re.findall(r"\binputs\.([A-Za-z0-9_-]+)", text))
        self.assertTrue(referenced, "no inputs.* references found")
        # the old file referenced inputs.min_runs while declaring min-runs:
        # GitHub silently expands an undeclared input to "" and the report
        # step then dies with `--min-runs: invalid int value: ''`
        self.assertEqual(referenced - declared, set(),
                         f"undeclared inputs referenced: {referenced - declared}")
        self.assertEqual(declared - referenced, set(),
                         f"declared inputs never used: {declared - referenced}")

    def test_cli_flags_in_action_exist(self):
        """Every `--flag` the action passes to `flakebox <cmd>` must be a
        real flag of that subcommand's parser."""
        text = action_text()
        parser = cli_mod.build_parser()
        subs = next(a for a in parser._actions
                    if isinstance(a, argparse_sub_type())).choices
        found = 0
        for cmd, flags in re.findall(
                r"python3 -m flakebox (\w+)((?:\s+(?:--[a-z-]+(?:\s+\"[^\"]*\")?))*)",
                text):
            self.assertIn(cmd, subs, f"unknown subcommand in action.yml: {cmd}")
            known = {o for a in subs[cmd]._actions for o in a.option_strings}
            for flag in re.findall(r"--[a-z-]+", flags):
                found += 1
                self.assertIn(flag, known, f"`flakebox {cmd}` has no {flag}")
        self.assertGreaterEqual(found, 4)
        # the input that feeds a CLI flag must spell that flag
        self.assertIn('--min-runs "${{ inputs.min-runs }}"', text)

    def test_min_runs_input_feeds_a_valid_int(self):
        default = flat_yaml(action_text())["inputs"]["min-runs"]["default"]
        code, out, _ = run_cli("--db", os.path.join(mktemp(), "h.db"),
                               "report", "--format", "json",
                               "--min-runs", default)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["flaky"], [])


def argparse_sub_type():
    import argparse
    return argparse._SubParsersAction


# --------------------------------------------------------------------------
# no PyYAML (panel finding 4)
# --------------------------------------------------------------------------


class TestNoYamlDependency(unittest.TestCase):
    def test_package_and_tests_never_import_yaml(self):
        for sub in ("flakebox", "tests"):
            d = os.path.join(REPO, sub)
            for name in os.listdir(d):
                if name.endswith(".py"):
                    with open(os.path.join(d, name), encoding="utf-8") as f:
                        src = f.read()
                    self.assertNotRegex(src, r"^\s*(import yaml|from yaml)",
                                        f"{sub}/{name} imports yaml")

    def test_import_flakebox_with_yaml_blocked(self):
        code = ("import sys; sys.modules['yaml'] = None\n"
                "import flakebox, flakebox.cli, flakebox.detect, "
                "flakebox.junit, flakebox.quarantine, flakebox.store\n"
                "print(flakebox.cli.VERSION)")
        proc = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), cli_mod.VERSION)


# --------------------------------------------------------------------------
# quarantine: idempotent, complete output, converges (panel finding 2)
# --------------------------------------------------------------------------


class TestQuarantineIdempotent(unittest.TestCase):
    def test_quarantine_add_is_a_set(self):
        db = os.path.join(mktemp(), "q.db")
        conn = store_mod.connect(db)
        self.assertTrue(store_mod.quarantine_add(conn, "a::t", "auto: flaky"))
        first = store_mod.quarantined(conn)
        # re-adding must not touch the row (old code INSERT OR REPLACEd it,
        # resetting the timestamp and overwriting the reason)
        conn.execute("UPDATE quarantine SET quarantined_at = 1000")
        conn.commit()
        self.assertFalse(store_mod.quarantine_add(conn, "a::t", "manual"))
        rows = store_mod.quarantined(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "auto: flaky")
        self.assertEqual(rows[0]["at"], 1000)
        self.assertEqual(first[0]["test_id"], rows[0]["test_id"])
        conn.close()

    def test_deselect_and_json_print_the_full_set_every_run(self):
        db = make_db(mktemp())
        outs = [run_cli("--db", db, "quarantine", "--format", "deselect")
                for _ in range(3)]
        for code, out, _err in outs:
            self.assertEqual(code, 0)
            self.assertEqual(out.split(), ["--deselect",
                                           "tests/test_a.py::TestA::test_flaky"])
        self.assertIn("1 new", outs[0][2])
        self.assertIn("0 new", outs[1][2])
        code, out, _ = run_cli("--db", db, "quarantine", "--format", "json")
        data = json.loads(out)
        self.assertEqual(data, {"quarantined": ["tests.test_a.TestA::test_flaky"],
                                "new": []})

    def test_repeated_quarantine_against_always_failing_runner_converges(self):
        """Fake runner: `always` fails every run, `flaky` alternates,
        `good` passes. Quarantining repeatedly (bounded loop) must reach
        a fixed point after the first pass, never quarantine the
        always-failing (broken) test, and never re-stamp rows."""
        db = db_from_outcomes(mktemp(), {
            "r::always": ["failed"] * 8,
            "r::flaky": ["failed", "passed", "failed", "passed",
                         "failed", "passed", "failed", "passed"],
            "r::good": ["passed"] * 8,
        })
        snapshots = []
        for _ in range(5):  # hard cap: the loop in CI would be unbounded
            code, _, _ = run_cli("--db", db, "quarantine", "--format", "json")
            self.assertEqual(code, 0)
            conn = store_mod.connect(db)
            snapshots.append(sorted((q["test_id"], q["at"], q["reason"])
                                    for q in store_mod.quarantined(conn)))
            conn.close()
        self.assertEqual([t for t, _, _ in snapshots[0]], ["r::flaky"])
        self.assertTrue(all(s == snapshots[0] for s in snapshots),
                        "quarantine set drifted across identical runs")

    def test_recovered_test_is_not_requarantined(self):
        """quarantine -> 10 straight passes -> 'stable' -> unquarantine ->
        must NOT be flagged flaky again from the stale failures (the old
        code ping-ponged forever: stable and flaky at the same time)."""
        db = db_from_outcomes(mktemp(), {
            "p::t": ["failed", "failed", "failed"] + ["passed"] * 10})
        conn = store_mod.connect(db)
        store_mod.quarantine_add(conn, "p::t", "auto: flaky")
        a = detect_mod.analyze(conn, min_runs=5, stable_runs=10)
        self.assertEqual([s["test_id"] for s in a["stable"]], ["p::t"])
        self.assertEqual(a["flaky"], [], "stable and flaky at once")
        store_mod.quarantine_remove(conn, "p::t")
        conn.close()
        code, out, _ = run_cli("--db", db, "quarantine", "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"quarantined": [], "new": []})
        # still a candidate while the streak is short
        db2 = db_from_outcomes(mktemp(), {
            "p::t": ["failed", "failed", "failed"] + ["passed"] * 9})
        conn = store_mod.connect(db2)
        a = detect_mod.analyze(conn, min_runs=5, stable_runs=10)
        conn.close()
        self.assertEqual([s["test_id"] for s in a["flaky"]], ["p::t"])


# --------------------------------------------------------------------------
# stats (panel finding 3)
# --------------------------------------------------------------------------


class TestStats(unittest.TestCase):
    def test_three_way_classification(self):
        db = db_from_outcomes(mktemp(), {
            "c::flaky": ["passed", "failed", "passed", "passed", "error"],
            "c::failing": ["failed", "error", "failed", "failed", "failed"],
            "c::passing": ["passed"] * 5,
        })
        conn = store_mod.connect(db)
        a = detect_mod.analyze(conn, min_runs=5)
        self.assertEqual([s["test_id"] for s in a["flaky"]], ["c::flaky"])
        self.assertEqual([s["test_id"] for s in a["broken"]], ["c::failing"])
        self.assertEqual(a["stable"], [])
        self.assertEqual(detect_mod.summarize(conn, "c::flaky")["status"], "flaky")
        self.assertEqual(detect_mod.summarize(conn, "c::failing")["status"],
                         "failing")
        self.assertEqual(detect_mod.summarize(conn, "c::passing")["status"],
                         "passing")
        conn.close()
        self.assertEqual(detect_mod.classify(0, 0), "unknown")

    def test_skipped_runs_are_not_evidence(self):
        # 1 pass + 1 fail + 3 skips = 5 recorded runs but only 2 executed:
        # the old code called this flaky with "5 runs"
        db = db_from_outcomes(mktemp(), {
            "s::t": ["passed", "failed", "skipped", "skipped", "skipped"],
            "s::f": ["failed", "skipped", "failed", "skipped", "failed",
                     "failed", "failed"],
        })
        conn = store_mod.connect(db)
        a = detect_mod.analyze(conn, min_runs=5)
        self.assertEqual(a["flaky"], [])
        # 5 executed failures around 2 skips: failing, and the counts say
        # executed runs, not recorded rows
        self.assertEqual([s["test_id"] for s in a["broken"]], ["s::f"])
        s = a["broken"][0]
        self.assertEqual((s["runs"], s["recorded"], s["skips"]), (5, 7, 2))
        self.assertAlmostEqual(detect_mod.summarize(conn, "s::t")["flake_rate"],
                               0.5)
        conn.close()

    def test_always_failing_is_reported_failing_not_flaky(self):
        db = make_db(mktemp())
        code, out, _ = run_cli("--db", db, "history",
                               "--test", "tests.test_b::test_broken")
        self.assertEqual(code, 0)
        self.assertIn("failing", out)
        self.assertNotIn("flake rate", out)
        code, out, _ = run_cli("--db", db, "report")
        self.assertIn("Consistently failing", out)
        flaky_table = out.split("## Consistently failing")[0]
        self.assertNotIn("test_broken", flaky_table)

    def test_pass_streak_ignores_skips(self):
        s = detect_mod.stats(["passed", "skipped", "passed", "failed", "passed"])
        self.assertEqual(s["pass_streak"], 2)
        self.assertEqual((s["runs"], s["passes"], s["failures"], s["skips"]),
                         (4, 3, 1, 1))
        self.assertEqual(detect_mod.stats([])["status"], "unknown")


# --------------------------------------------------------------------------
# quarantine plugin modes + xfail round trip
# --------------------------------------------------------------------------


def exec_plugin(tmp: str, ids: list, mode: str = "skip") -> dict:
    """Load the REAL generated plugin from `tmp` with a stub pytest."""
    quar_mod.write_pytest(tmp, ids, mode=mode)
    plugin = os.path.join(tmp, "flakebox_quarantine.py")

    class _Mark:
        def __getattr__(self, kind):
            return lambda **kw: (kind, kw)

    class _Pytest:
        mark = _Mark()

    ns: dict = {"__file__": plugin}
    with open(plugin, encoding="utf-8") as f:
        src = f.read()
    real = sys.modules.get("pytest")
    sys.modules["pytest"] = _Pytest()  # type: ignore[assignment]
    try:
        exec(compile(src, plugin, "exec"), ns)
    finally:
        if real is not None:
            sys.modules["pytest"] = real
        else:
            sys.modules.pop("pytest", None)
    return ns


class _Item:
    def __init__(self, nodeid):
        self.nodeid, self.markers = nodeid, []

    def add_marker(self, m):
        self.markers.append(m)


class TestQuarantineModes(unittest.TestCase):
    def test_skip_mode_default(self):
        ns = exec_plugin(mktemp(), ["m.T::test_a"])
        items = [_Item("m.py::T::test_a"), _Item("m.py::T::test_b")]
        ns["pytest_collection_modifyitems"](items)
        self.assertEqual(items[0].markers,
                         [("skip", {"reason": quar_mod.REASON})])
        self.assertEqual(items[1].markers, [])

    def test_xfail_mode_runs_tests_without_failing_build(self):
        ns = exec_plugin(mktemp(), ["m.T::test_a"], mode="xfail")
        item = _Item("m.py::T::test_a")
        ns["pytest_collection_modifyitems"]([item])
        self.assertEqual(item.markers,
                         [("xfail", {"reason": quar_mod.REASON, "strict": False})])

    def test_bad_mode_rejected(self):
        with self.assertRaises(ValueError):
            quar_mod.write_pytest(mktemp(), ["x::y"], mode="ignore")

    def test_cli_mode_flag(self):
        tmp = mktemp()
        db = make_db(tmp)
        code, _, _ = run_cli("--db", db, "quarantine", "--format", "pytest",
                             "-o", tmp, "--mode", "xfail")
        self.assertEqual(code, 0)
        with open(os.path.join(tmp, "flakebox_quarantine.json"),
                  encoding="utf-8") as f:
            self.assertEqual(json.load(f)["mode"], "xfail")

    def test_junit_maps_quarantine_xfail_back_to_failure(self):
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="3">
  <testcase classname="m.T" name="test_q" time="0.1">
    <skipped type="pytest.xfail" message="expected test failure">{quar_mod.REASON}</skipped>
  </testcase>
  <testcase classname="m.T" name="test_xfail_by_design" time="0.1">
    <skipped type="pytest.xfail" message="expected test failure">known bug #12</skipped>
  </testcase>
  <testcase classname="m.T" name="test_skipped" time="0.1">
    <skipped type="pytest.skip" message="no gpu"/>
  </testcase>
</testsuite>"""
        tmp = mktemp()
        p = os.path.join(tmp, "x.xml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(xml)
        by_id = {r["test_id"]: r for r in junit_mod.parse(p)}
        self.assertEqual(by_id["m.T::test_q"]["outcome"], "failed")
        self.assertEqual(by_id["m.T::test_q"]["message"], quar_mod.REASON)
        self.assertEqual(by_id["m.T::test_xfail_by_design"]["outcome"], "skipped")
        self.assertEqual(by_id["m.T::test_skipped"]["outcome"], "skipped")


# --------------------------------------------------------------------------
# Windows portability
# --------------------------------------------------------------------------


class TestPortability(unittest.TestCase):
    def test_report_survives_cp1252_console(self):
        """The report prints emoji; on a cp1252 Windows console that used
        to raise UnicodeEncodeError and kill the CI step."""
        tmp = mktemp()
        db = make_db(tmp)
        conn = store_mod.connect(db)
        store_mod.quarantine_add(conn, "tests.test_a.TestA::test_flaky", "auto")
        conn.close()
        for argv in (["--db", db, "report"],
                     ["--db", make_db(mktemp(), flaky_fails=0), "report"]):
            raw = io.BytesIO()
            console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict",
                                       write_through=True)
            with redirect_stdout(console):
                code = cli_mod.main(argv)
            console.flush()
            self.assertEqual(code, 0)
            self.assertIn(b"Flakebox report", raw.getvalue())

    def test_default_db_follows_home_env(self):
        self.assertTrue(cli_mod.default_db().startswith(_fake_home))
        # argparse wraps long default paths across lines (it did on
        # Windows, where temp paths are long): compare without whitespace.
        squash = lambda s: re.sub(r"\s+", "", s)
        self.assertIn(squash(cli_mod.default_db()),
                      squash(cli_mod.build_parser().format_help()))

    def test_text_files_are_opened_with_utf8(self):
        pkg = os.path.join(REPO, "flakebox")
        for name in os.listdir(pkg):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(pkg, name), encoding="utf-8") as f:
                src = f.read()
            for call in open_calls(src):
                if '"rb"' in call or "'rb'" in call:
                    continue
                self.assertIn("encoding=", call, f"{name}: open({call})")


def open_calls(src: str) -> list:
    """Argument text of every `open(...)` call, parentheses balanced."""
    out = []
    for m in re.finditer(r"\bopen\(", src):
        depth, i = 1, m.end()
        while i < len(src) and depth:
            depth += {"(": 1, ")": -1}.get(src[i], 0)
            i += 1
        out.append(src[m.end():i - 1])
    return out


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
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


class TestXmlBombGuard(unittest.TestCase):
    def test_rejects_doctype_bomb(self):
        p = write_raw(mktemp(), "bomb.xml", DOCTYPE_BOMB)
        with self.assertRaisesRegex(ValueError, "DOCTYPE"):
            junit_mod.parse(p)

    def test_rejects_entity_declaration(self):
        p = write_raw(mktemp(), "entity.xml", ENTITY_DECL)
        with self.assertRaisesRegex(ValueError, "ENTITY"):
            junit_mod.parse(p)

    def test_rejection_is_case_insensitive(self):
        p = write_raw(mktemp(), "lower.xml", LOWERCASE_DOCTYPE)
        with self.assertRaises(ValueError):
            junit_mod.parse(p)

    def test_cli_record_rejects_bomb_with_exit_2(self):
        tmp = mktemp()
        p = write_raw(tmp, "bomb.xml", DOCTYPE_BOMB)
        code, _, err = run_cli("--db", os.path.join(tmp, "h.db"),
                               "record", "--junit", p)
        self.assertEqual(code, 2)
        self.assertIn("DOCTYPE", err)

    def test_legit_xml_still_parses(self):
        tmp = mktemp()
        p = write_junit(tmp, True)
        rs = junit_mod.parse(p)
        self.assertEqual(len(rs), 4)


class TestPackaging(unittest.TestCase):
    def test_pyproject_metadata(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib unavailable (needs Python 3.11+)")
        with open(os.path.join(REPO, "pyproject.toml"), "rb") as f:
            data = tomllib.load(f)
        proj = data["project"]
        self.assertEqual(proj["name"], "flakebox")
        self.assertEqual(proj["version"], cli_mod.VERSION)
        self.assertEqual(proj["scripts"]["flakebox"], "flakebox.cli:main")
        self.assertEqual(proj["license"], "MIT")
        self.assertTrue(os.path.exists(os.path.join(REPO, proj["readme"])))
        self.assertEqual(data["tool"]["setuptools"]["packages"], ["flakebox"])

    def test_entry_point_resolves(self):
        import importlib
        module_name, attr = "flakebox.cli", "main"
        target = getattr(importlib.import_module(module_name), attr)
        self.assertTrue(callable(target))
        # the exact [project.scripts] target, spelled out
        self.assertIs(target, cli_mod.main)


class TestActionCacheKeys(unittest.TestCase):
    def test_keys_are_per_job(self):
        text = action_text()
        # both restore and save cache keys must scope by job
        self.assertIn("${{ github.job }}", text)
        restore = text.index("Restore flakebox history")
        save = text.index("Save flakebox history")
        self.assertIn("github.job", text[restore:save])
        self.assertIn("github.job", text[save:])

    def test_cache_race_is_documented_honestly(self):
        text = action_text().lower()
        # documents the limitation and the aggregator pattern
        self.assertIn("aggregator", text)
        # the merge path is `flakebox import`; there is no `flakebox merge`
        # and the action must not pretend there is
        self.assertIn("flakebox import", text)
        self.assertNotIn("flakebox merge", text)
        # structural bits the action needs still present
        self.assertIn("GITHUB_STEP_SUMMARY", action_text())


# ---------------------------------------------------------------------------
# Regression tests for the Round 2 fix batch.
# ---------------------------------------------------------------------------


class TestQuarantineMatchesClassless(unittest.TestCase):
    """FIX A: classless node IDs (test_mod.py::test_x) must match."""

    def setUp(self):
        self.m = exec_plugin(mktemp(), ["placeholder::x"])["_matches"]

    def test_classless_matches_both_directions(self):
        self.assertTrue(self.m("test_mod.py::test_x", "test_mod::test_x"))
        self.assertTrue(self.m("test_mod::test_x", "test_mod.py::test_x"))

    def test_classless_matches_dotted_junit_variant(self):
        self.assertTrue(self.m("tests/test_mod.py::test_x",
                               "pkg.test_mod::test_x"))
        self.assertTrue(self.m("pkg.test_mod::test_x",
                               "tests/test_mod.py::test_x"))

    def test_class_based_still_matches(self):
        self.assertTrue(self.m("test_demo.py::TestDemo::test_flaky",
                               "test_demo.TestDemo::test_flaky"))
        self.assertTrue(self.m("tests/test_a.py::TestA::test_x",
                               "tests.test_a.TestA::test_x"))

    def test_non_matching_names_still_miss(self):
        self.assertFalse(self.m("test_mod.py::test_x", "test_mod::test_y"))
        self.assertFalse(self.m("test_mod.py::test_x", "other_mod::test_x"))
        self.assertFalse(self.m("test_mod.py::TestA::test_x",
                                "test_mod.TestB::test_x"))
        self.assertFalse(self.m("test_x", "test_x"))  # too short: needs 2 segs


class TestStorePragmas(unittest.TestCase):
    """FIX B: concurrent-writer pragmas are set on every connect."""

    def test_busy_timeout_and_wal(self):
        db = os.path.join(mktemp(), "h.db")
        conn = store_mod.connect(db)
        try:
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(timeout, 5000)
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(journal, "wal")
        finally:
            conn.close()

    def test_pragmas_survive_reconnect(self):
        tmp = mktemp()
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


def reference_analyze(conn, min_runs=5, stable_runs=10, window=50,
                      retire_after=3):
    """Independent per-test (N+1) implementation of the detection rules
    (see detect.py's docstring), kept as the oracle for the batched
    analyze(): same classification, same counts, same order."""
    flaky, broken, stable, retired = [], [], [], []
    recent = [r[0] for r in conn.execute(
        "SELECT id FROM runs ORDER BY id DESC LIMIT ?", (retire_after,))]
    all_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT test_id FROM results ORDER BY rowid")]
    for tid in all_ids:
        hist = store_mod.test_history(conn, tid, limit=window)  # newest first
        last_run = conn.execute(
            "SELECT MAX(run_id) FROM results WHERE test_id = ?", (tid,)).fetchone()[0]
        outcomes = [h["outcome"] for h in hist]
        passes = sum(1 for o in outcomes if o == "passed")
        fails = sum(1 for o in outcomes if o in ("failed", "error"))
        skips = sum(1 for o in outcomes if o == "skipped")
        streak = 0
        for o in outcomes:
            if o == "passed":
                streak += 1
            elif o != "skipped":
                break
        executed = passes + fails
        status = ("flaky" if passes and fails else "failing" if fails
                  else "passing" if passes else "unknown")
        s = {"runs": executed, "recorded": len(outcomes), "passes": passes,
             "failures": fails, "skips": skips,
             "flake_rate": (fails / executed) if executed else 0.0,
             "pass_streak": streak, "status": status, "test_id": tid,
             "quarantined": store_mod.is_quarantined(conn, tid)}
        if recent and last_run < min(recent):
            s["retired"] = True
            s["last_run"] = last_run
            s["last_commit"] = conn.execute(
                "SELECT commit_sha FROM runs WHERE id = ?", (last_run,)).fetchone()[0]
            retired.append(s)
            continue
        s["retired"] = False
        if executed < min_runs:
            continue
        recovered = streak >= stable_runs
        if status == "flaky" and not recovered:
            flaky.append(s)
        elif status == "failing":
            broken.append(s)
        if s["quarantined"] and recovered:
            stable.append(s)
    flaky.sort(key=lambda s: (-s["flake_rate"], -s["failures"]))
    broken.sort(key=lambda s: -s["failures"])
    retired.sort(key=lambda s: (-s["last_run"], s["test_id"]))
    return {"flaky": flaky, "broken": broken, "stable": stable,
            "retired": retired}


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
            if 138 <= i < 145 and run >= n_runs - 4 - (i - 138):
                continue  # retired: deleted 4..10 runs before the end
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
            results.append(result(tid, outcome))
        store_mod.record_run(conn, results, commit_sha=f"sha{run}")
    # quarantine a spread across kinds
    for i in (3, 37, 66, 99, 121, 130, 140):
        store_mod.quarantine_add(conn, f"mod{i:03d}::test_{i:03d}", "auto")
    conn.close()
    return db


class TestAnalyzeBatchEquivalence(unittest.TestCase):
    """FIX C: batched analyze() must equal the per-test oracle exactly."""

    KW = dict(min_runs=5, stable_runs=5, window=10)  # window < runs on purpose

    def test_identical_results_on_synthetic_dataset(self):
        db = make_equivalence_db(mktemp())
        conn = store_mod.connect(db)
        try:
            new = detect_mod.analyze(conn, **self.KW)
            old = reference_analyze(conn, **self.KW)
            self.assertEqual(new, old)
            # sanity: the fixture actually exercises every bucket
            self.assertTrue(new["flaky"])
            self.assertTrue(new["broken"])
            self.assertTrue(new["stable"])
            self.assertEqual(len(new["retired"]), 7)
            # retirement is a knob: 0 disables it and everything is live
            live = detect_mod.analyze(conn, retire_after=0, **self.KW)
            self.assertEqual(live["retired"], [])
            self.assertEqual(live, reference_analyze(conn, retire_after=0, **self.KW))
        finally:
            conn.close()

    def test_known_classifications(self):
        tmp = mktemp()
        db = os.path.join(tmp, "k.db")
        conn = store_mod.connect(db)
        for i in range(6):
            store_mod.record_run(conn, [
                result("a::flaky", "passed" if i % 3 else "failed"),
                result("a::broken", "error"),
                result("a::good", "passed"),
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
        db = make_equivalence_db(mktemp())
        conn = store_mod.connect(db)
        calls: list = []
        conn.set_trace_callback(calls.append)
        try:
            detect_mod.analyze(conn, **self.KW)
        finally:
            conn.set_trace_callback(None)
            conn.close()
        # 1 batched history query + quarantine set + newest run ids +
        # commits of the retired tests' last runs: constant, never per test
        self.assertLessEqual(len(calls), 5,
                             f"expected ~4 queries, got {len(calls)}")

    def test_fewer_queries_than_old_logic(self):
        db = make_equivalence_db(mktemp())
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


# ---------------------------------------------------------------------------
# Round 3 panel findings.
# ---------------------------------------------------------------------------


class TestRetired(unittest.TestCase):
    """F-1: a deleted/renamed test must age out of the report instead of
    staying 'consistently failing' for its whole 50-run window."""

    def test_deleted_test_ages_out_of_the_report(self):
        tmp = mktemp()
        db = db_from_outcomes(tmp, {
            "t::stable": ["passed"] * 25,
            "t::broken": ["failed"] * 7 + [None] * 18,   # deleted after run 7
            "t::flaky": ["failed", "passed"] * 12 + ["failed"],
        })
        conn = store_mod.connect(db)
        a = detect_mod.analyze(conn, min_runs=5)
        self.assertEqual([s["test_id"] for s in a["broken"]], [])
        self.assertEqual([s["test_id"] for s in a["flaky"]], ["t::flaky"])
        self.assertEqual([s["test_id"] for s in a["retired"]], ["t::broken"])
        r = a["retired"][0]
        self.assertEqual((r["status"], r["last_run"], r["last_commit"],
                          r["retired"]), ("failing", 7, "s6", True))
        self.assertTrue(detect_mod.summarize(conn, "t::broken")["retired"])
        self.assertFalse(detect_mod.summarize(conn, "t::flaky")["retired"])
        # the knob: 0 disables retirement and the old answer is back
        a0 = detect_mod.analyze(conn, min_runs=5, retire_after=0)
        self.assertEqual([s["test_id"] for s in a0["broken"]], ["t::broken"])
        self.assertEqual(a0["retired"], [])
        conn.close()
        # CLI: its own section, not the failing one; JSON carries it
        code, out, _ = run_cli("--db", db, "report")
        self.assertEqual(code, 0)
        self.assertIn("## Retired", out)
        self.assertNotIn("test_broken", out.split("## Retired")[0].split(
            "## Consistently failing")[-1] if "## Consistently failing" in out else "")
        self.assertIn("`t::broken` (was failing; last seen run 7, commit s6)", out)
        code, out, _ = run_cli("--db", db, "report", "--format", "json")
        self.assertEqual([s["test_id"] for s in json.loads(out)["retired"]],
                         ["t::broken"])
        code, out, _ = run_cli("--db", db, "report", "--retire-after", "0")
        self.assertIn("Consistently failing", out)
        code, out, _ = run_cli("--db", db, "history", "--test", "t::broken")
        self.assertIn("[retired", out)

    def test_short_absence_is_not_retirement(self):
        # a partial run or two (someone ran one file) must not retire a test
        db = db_from_outcomes(mktemp(), {
            "t::stable": ["passed"] * 10,
            "t::broken": ["failed"] * 8 + [None, None],
        })
        conn = store_mod.connect(db)
        a = detect_mod.analyze(conn, min_runs=5)  # retire_after defaults to 3
        self.assertEqual([s["test_id"] for s in a["broken"]], ["t::broken"])
        self.assertEqual(a["retired"], [])
        conn.close()

    def test_retired_quarantined_test_is_not_requarantined_but_flagged(self):
        db = db_from_outcomes(mktemp(), {
            "t::live": ["passed"] * 10,
            "t::gone": ["failed", "passed"] * 3 + [None] * 4,
        })
        conn = store_mod.connect(db)
        store_mod.quarantine_add(conn, "t::gone", "auto: flaky")
        conn.close()
        code, out, _ = run_cli("--db", db, "quarantine", "--format", "json")
        self.assertEqual(json.loads(out), {"quarantined": ["t::gone"], "new": []})
        code, out, _ = run_cli("--db", db, "report")
        self.assertIn("still quarantined", out)
        self.assertIn('flakebox unquarantine --test "t::gone"', out)


def record_runs(tmp: str, db: str, n: int = 6, flaky_until: int = 3) -> None:
    """Record `n` runs through the CLI (so runs carry a report hash)."""
    for i in range(n):
        p = write_junit(tmp, fail_flaky=(i < flaky_until))
        code, _, _ = run_cli("--db", db, "record", "--junit", p,
                             "--commit", f"sha{i}")
        assert code == 0


class TestActionQuarantineLoop(unittest.TestCase):
    """F-2: the Action can acknowledge flakes (quarantine) and hand the set
    and the history to developers; export/import carries the DB."""

    def test_quarantine_and_export_inputs_are_wired(self):
        text = action_text()
        inputs = flat_yaml(text)["inputs"]
        for name in ("quarantine-output", "export-output"):
            self.assertIn(name, inputs)
            self.assertEqual(inputs[name]["default"], "")
            self.assertEqual(inputs[name]["required"], "false")
        self.assertIn("python3 -m flakebox quarantine --format json", text)
        self.assertIn("python3 -m flakebox export --out", text)
        self.assertIn("if: inputs.quarantine-output != ''", text)
        self.assertIn("if: inputs.export-output != ''", text)
        # quarantine (acknowledge) and export run BEFORE the fail step, so a
        # new flake fails one build and the artifacts exist for that build
        q = text.index("name: Quarantine flaky tests")
        e = text.index("name: Export history")
        f = text.index("name: Fail on new flakes")
        self.assertLess(text.index("name: Report"), q)
        self.assertLess(q, f)
        self.assertLess(e, f)
        # fail-on-flaky tells the truth about needing quarantine-output
        self.assertIn("quarantine-output", inputs["fail-on-flaky"]["description"])

    def test_ci_acknowledge_loop_end_to_end(self):
        tmp = mktemp()
        db = os.path.join(tmp, "ci.db")
        record_runs(tmp, db)

        def new_flaky() -> int:
            code, out, _ = run_cli("--db", db, "report", "--format", "json")
            self.assertEqual(code, 0)
            return sum(1 for t in json.loads(out)["flaky"] if not t["quarantined"])

        self.assertEqual(new_flaky(), 1)           # build N: fail-on-flaky fires
        code, out, _ = run_cli("--db", db, "quarantine", "--format", "json")
        self.assertEqual(code, 0)
        qset = json.loads(out)
        self.assertEqual(qset["quarantined"], ["tests.test_a.TestA::test_flaky"])
        self.assertEqual(new_flaky(), 0)           # build N+1: acknowledged
        # developer side: the quarantine artifact becomes the plugin
        plugin_dir = os.path.join(tmp, "repo")
        quar_mod.write_pytest(plugin_dir, qset["quarantined"])
        with open(os.path.join(plugin_dir, "flakebox_quarantine.json"),
                  encoding="utf-8") as f:
            self.assertEqual(json.load(f)["tests"], qset["quarantined"])

    def test_export_import_round_trip_and_idempotence(self):
        tmp = mktemp()
        db = os.path.join(tmp, "ci.db")
        record_runs(tmp, db)
        code, _, _ = run_cli("--db", db, "quarantine", "--format", "json")
        exp = os.path.join(tmp, "flakebox-export.json")
        code, out, _ = run_cli("--db", db, "export", "--out", exp)
        self.assertEqual(code, 0)
        self.assertIn("exported 6 run(s), 1 quarantine entry", out)
        with open(exp, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["flakebox_export"], 1)
        self.assertEqual(len(data["runs"]), 6)
        self.assertTrue(all(r["report_sha"] for r in data["runs"]))
        # a laptop with no history imports it and sees CI's picture
        local = os.path.join(tmp, "local.db")
        code, out, _ = run_cli("--db", local, "import", exp)
        self.assertEqual(code, 0)
        self.assertIn("imported 6 run(s), skipped 0 already recorded, "
                      "1 new quarantine entry", out)
        code, out, _ = run_cli("--db", local, "report", "--format", "json")
        a = json.loads(out)
        self.assertEqual([s["test_id"] for s in a["flaky"]],
                         ["tests.test_a.TestA::test_flaky"])
        self.assertTrue(a["flaky"][0]["quarantined"])
        self.assertEqual([s["test_id"] for s in a["broken"]],
                         ["tests.test_b::test_broken"])
        # importing the same export again (or into its own source) adds nothing
        code, out, _ = run_cli("--db", local, "import", exp)
        self.assertIn("imported 0 run(s), skipped 6 already recorded, "
                      "0 new quarantine entries", out)
        code, out, _ = run_cli("--db", db, "import", exp)
        self.assertIn("imported 0 run(s), skipped 6", out)
        conn = store_mod.connect(db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 6)
        conn.close()
        # history came along: per-test history on the laptop
        code, out, _ = run_cli("--db", local, "history",
                               "--test", "tests.test_a.TestA::test_flaky")
        self.assertIn("flake rate 50%", out)

    def test_import_merges_two_per_job_exports(self):
        # the aggregator pattern from action.yml: two matrix jobs, one DB
        tmp = mktemp()
        exports = []
        for job in ("linux", "windows"):
            db = os.path.join(tmp, f"{job}.db")
            conn = store_mod.connect(db)
            for i in range(5):
                store_mod.record_run(conn, [result(f"{job}::t", "passed"),
                                            result("shared::t", "failed" if job == "linux" else "passed")],
                                     commit_sha=f"c{i}", report_sha=f"{job}{i}")
            conn.close()
            exp = os.path.join(tmp, f"{job}.json")
            self.assertEqual(run_cli("--db", db, "export", "--out", exp)[0], 0)
            exports.append(exp)
        merged = os.path.join(tmp, "merged.db")
        code, out, _ = run_cli("--db", merged, "import", *exports)
        self.assertEqual(code, 0)
        self.assertIn("imported 10 run(s)", out)
        code, out, _ = run_cli("--db", merged, "report", "--format", "json",
                               "--retire-after", "0")
        a = json.loads(out)
        self.assertEqual([s["test_id"] for s in a["flaky"]], ["shared::t"])
        self.assertEqual(a["flaky"][0]["runs"], 10)

    def test_import_rejects_garbage_cleanly(self):
        tmp = mktemp()
        bad = write_raw(tmp, "bad.json", '{"hello": "world"}')
        code, _, err = run_cli("--db", os.path.join(tmp, "x.db"), "import", bad)
        self.assertEqual(code, 2)
        self.assertIn("not a flakebox export", err)
        code, _, err = run_cli("--db", os.path.join(tmp, "x.db"), "import",
                               os.path.join(tmp, "missing.json"))
        self.assertEqual(code, 2)
        self.assertIn("no such file", err)


class TestDeselectNodeIds(unittest.TestCase):
    """F-3: `pytest $(flakebox quarantine --format deselect)` must emit ids
    pytest recognises - JUnit ids are not node ids."""

    def test_mapping_of_common_shapes(self):
        m = quar_mod.pytest_nodeid
        self.assertEqual(m("tests.test_a.TestA::test_flaky"),
                         "tests/test_a.py::TestA::test_flaky")
        self.assertEqual(m("tests.test_b::test_broken"), "tests/test_b.py::test_broken")
        self.assertEqual(m("pkg.sub.test_m.Outer.Inner::test_x[1-2]"),
                         "pkg/sub/test_m.py::Outer::Inner::test_x[1-2]")
        self.assertEqual(m("test_m::test_x"), "test_m.py::test_x")
        # already a node id, or no classname at all: untouched
        self.assertEqual(m("tests/test_a.py::TestA::test_x"),
                         "tests/test_a.py::TestA::test_x")
        self.assertEqual(m("test_x"), "test_x")
        self.assertEqual(m("TestA::test_x"), "TestA::test_x")

    def test_pytest_honours_mapped_deselect(self):
        try:
            import pytest  # noqa: F401
        except ImportError:
            self.skipTest("pytest not installed")
        tmp = mktemp()
        tdir = os.path.join(tmp, "t")
        os.makedirs(tdir)
        write_raw(tdir, "test_m.py",
                  "class TestM:\n    def test_a(self):\n        pass\n\n"
                  "    def test_b(self):\n        pass\n\n\n"
                  "def test_c():\n    pass\n")

        def run_pytest(extra: list) -> list:
            report = os.path.join(tmp, "j.xml")
            if os.path.exists(report):
                os.remove(report)
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "--junitxml", report, "t"] + extra,
                cwd=tmp, capture_output=True, text=True, timeout=120,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertIn(proc.returncode, (0, 5), proc.stdout + proc.stderr)
            return sorted(r["test_id"] for r in junit_mod.parse(report))

        ran = run_pytest([])
        self.assertEqual(ran, ["t.test_m.TestM::test_a", "t.test_m.TestM::test_b",
                               "t.test_m::test_c"])
        # the raw JUnit ids (what the old code emitted) deselect nothing
        self.assertEqual(run_pytest(["--deselect", "t.test_m.TestM::test_a",
                                     "--deselect", "t.test_m::test_c"]), ran)
        # the mapped ids deselect exactly those tests
        args = quar_mod.deselect_args(["t.test_m.TestM::test_a", "t.test_m::test_c"])
        self.assertEqual(run_pytest(args), ["t.test_m.TestM::test_b"])


class TestJunitShapes(unittest.TestCase):
    """F-4: nested suites, locale decimals, UTF-16 DOCTYPE bypass."""

    def test_nested_testsuites_are_read(self):
        xml = ('<?xml version="1.0"?><testsuites><testsuite name="outer">'
               '<testsuite name="inner"><testcase classname="a" name="t1" time="0.1"/>'
               '</testsuite><testcase classname="a" name="t2" time="0.1">'
               '<failure message="m"/></testcase></testsuite>'
               '<testsuite name="flat"><testcase classname="b" name="t3"/></testsuite>'
               '</testsuites>')
        p = write_raw(mktemp(), "nested.xml", xml)
        by_id = {r["test_id"]: r["outcome"] for r in junit_mod.parse(p)}
        self.assertEqual(by_id, {"a::t1": "passed", "a::t2": "failed", "b::t3": "passed"})

    def test_decimal_comma_and_unreadable_time(self):
        xml = ('<testsuite><testcase classname="a" name="comma" time="0,5"/>'
               '<testcase classname="a" name="junk" time="fast"/>'
               '<testcase classname="a" name="blank" time=""/>'
               '<testcase classname="a" name="none"/></testsuite>')
        p = write_raw(mktemp(), "t.xml", xml)
        warnings: list = []
        rs = {r["test_id"]: r["duration_ms"]
              for r in junit_mod.parse(p, warn=warnings.append)}
        self.assertEqual(rs, {"a::comma": 500, "a::junk": 0, "a::blank": 0, "a::none": 0})
        self.assertEqual(len(warnings), 1)
        self.assertIn("a::junk", warnings[0])
        self.assertIn("'fast'", warnings[0])
        # the default warning goes to stderr, and the report is accepted
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(len(junit_mod.parse(p)), 4)
        self.assertIn("warning", err.getvalue())

    def test_utf16_doctype_is_rejected(self):
        tmp = mktemp()
        bomb = ('<?xml version="1.0" encoding="utf-16"?>'
                '<!DOCTYPE testsuite [<!ENTITY a "expanded">]>'
                '<testsuite><testcase classname="a" name="&a;" time="0.1"/></testsuite>')
        for enc in ("utf-16", "utf-16-le", "utf-16-be"):
            p = os.path.join(tmp, f"bomb-{enc}.xml")
            with open(p, "wb") as f:
                f.write(bomb.encode(enc))
            with self.assertRaisesRegex(ValueError, "DOCTYPE", msg=enc):
                junit_mod.parse(p)
            code, _, err = run_cli("--db", os.path.join(tmp, "h.db"),
                                   "record", "--junit", p)
            self.assertEqual(code, 2)
            self.assertIn("DOCTYPE", err)
        # a legitimate UTF-16 report still parses
        ok = ('<?xml version="1.0" encoding="utf-16"?><testsuite>'
              '<testcase classname="a" name="t" time="0.1"/></testsuite>')
        p = os.path.join(tmp, "ok16.xml")
        with open(p, "wb") as f:
            f.write(ok.encode("utf-16"))
        self.assertEqual(junit_mod.parse(p)[0]["test_id"], "a::t")


class TestCorruptDb(unittest.TestCase):
    """F-5: a file that is not a SQLite database is a clean exit 2."""

    def test_corrupt_db_is_a_clean_error(self):
        tmp = mktemp()
        bad = os.path.join(tmp, "bad.db")
        with open(bad, "wb") as f:
            f.write(b"this is not a sqlite database\n" * 64)
        for argv in (["--db", bad, "report"],
                     ["report", "--db", bad],
                     ["--db", bad, "record", "--junit", write_junit(tmp, True)],
                     ["--db", bad, "quarantine", "--format", "json"]):
            code, out, err = run_cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("cannot use database", err)
            self.assertIn("not a database", err)
            self.assertIn(bad, err)


class TestRecordDedupe(unittest.TestCase):
    """F-6: recording the same report twice must not double-count."""

    def test_same_report_twice_is_recorded_once(self):
        tmp = mktemp()
        db = os.path.join(tmp, "h.db")
        p = write_junit(tmp, True)
        code, out, _ = run_cli("--db", db, "record", "--junit", p, "--commit", "abc")
        self.assertEqual(code, 0)
        self.assertIn("recorded run 1", out)
        code, out, _ = run_cli("--db", db, "record", "--junit", p, "--commit", "abc")
        self.assertEqual(code, 0)
        self.assertIn("already recorded as run 1", out)
        # a different commit with identical bytes is a different run
        code, out, _ = run_cli("--db", db, "record", "--junit", p, "--commit", "def")
        self.assertIn("recorded run 2", out)
        # no commit given: identical bytes still count as the same report
        code, out, _ = run_cli("--db", db, "record", "--junit", p)
        self.assertIn("recorded run 3", out)
        code, out, _ = run_cli("--db", db, "record", "--junit", p)
        self.assertIn("already recorded as run 3", out)
        # --force overrides
        code, out, _ = run_cli("--db", db, "record", "--junit", p, "--commit", "abc",
                               "--force")
        self.assertIn("recorded run 4", out)
        conn = store_mod.connect(db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 4)
        self.assertEqual(detect_mod.summarize(conn, "tests.test_a.TestA::test_flaky")["failures"], 4)
        conn.close()

    def test_old_db_without_report_sha_is_migrated(self):
        tmp = mktemp()
        db = os.path.join(tmp, "old.db")
        legacy = sqlite3.connect(db)
        legacy.executescript(
            "CREATE TABLE runs (id INTEGER PRIMARY KEY, commit_sha TEXT, "
            "branch TEXT, created_at INTEGER);"
            "CREATE TABLE results (run_id INTEGER, test_id TEXT, outcome TEXT, "
            "duration_ms INTEGER, message TEXT);"
            "CREATE TABLE quarantine (test_id TEXT PRIMARY KEY, "
            "quarantined_at INTEGER, reason TEXT);"
            "INSERT INTO runs (commit_sha, branch, created_at) VALUES ('old', 'main', 1);"
            "INSERT INTO results VALUES (1, 'a::t', 'failed', 1, '');")
        legacy.commit()
        legacy.close()
        conn = store_mod.connect(db)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        self.assertIn("report_sha", cols)
        self.assertEqual(store_mod.test_history(conn, "a::t")[0]["commit"], "old")
        conn.close()
        p = write_junit(tmp, True)
        code, out, _ = run_cli("--db", db, "record", "--junit", p, "--commit", "new")
        self.assertEqual(code, 0)
        self.assertIn("recorded run 2", out)
        # a legacy run has no hash: export/import carries it without
        # pretending to recognise it
        exp = os.path.join(tmp, "e.json")
        self.assertEqual(run_cli("--db", db, "export", "--out", exp)[0], 0)
        with open(exp, encoding="utf-8") as f:
            self.assertIsNone(json.load(f)["runs"][0]["report_sha"])


class TestDbFlagPosition(unittest.TestCase):
    """F-7: --db works after the subcommand as well as before it."""

    def test_db_after_subcommand(self):
        tmp = mktemp()
        db = make_db(tmp)
        before = run_cli("--db", db, "report", "--format", "json")
        after = run_cli("report", "--db", db, "--format", "json")
        self.assertEqual(before, after)
        self.assertIn("test_flaky", after[1])
        code, out, _ = run_cli("history", "--db", db,
                               "--test", "tests.test_a.TestA::test_flaky")
        self.assertEqual(code, 0)
        self.assertIn("flaky", out)
        code, out, _ = run_cli("record", "--junit", write_junit(tmp, True),
                               "--db", os.path.join(tmp, "r.db"))
        self.assertEqual(code, 0)
        self.assertIn("recorded run 1", out)
        # neither given: the HOME-relative default
        code, out, _ = run_cli("report")
        self.assertEqual(code, 0)
        self.assertIn(cli_mod.default_db(), out)

    def test_help_is_raw_and_subcommands_show_db(self):
        parser = cli_mod.build_parser()
        self.assertIs(parser.formatter_class, argparse.RawDescriptionHelpFormatter)
        self.assertIn("--db works before or after the subcommand", parser.format_help())
        subs = next(a for a in parser._actions
                    if isinstance(a, argparse_sub_type())).choices
        self.assertEqual(sorted(subs), ["export", "history", "import", "quarantine",
                                        "record", "report", "unquarantine"])
        for name, sub in subs.items():
            self.assertIn("--db", sub.format_help(), name)


class TestDocsAndDeadCode(unittest.TestCase):
    def test_dead_code_removed(self):
        self.assertFalse(hasattr(store_mod, "all_test_ids"))
        self.assertFalse(hasattr(cli_mod, "DEFAULT_DB"))

    def test_readme_is_truthful(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("pip install git+https://github.com/calasdan-sketch/flakebox", readme)
        self.assertNotIn("git clone <repo>", readme)
        self.assertNotRegex(readme, r"(?m)^\s*#?\s*(?:or: )?alias ")
        m = re.search(r"# (\d+) tests", readme)
        self.assertIsNotNone(m)
        suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        self.assertEqual(int(m.group(1)), suite.countTestCases())
        for phrase in ("flakebox export", "flakebox import", "quarantine-output",
                       "export-output", "retired", "--retire-after", "best effort",
                       "flakebox report --db", "already recorded", "--force"):
            self.assertIn(phrase, readme, phrase)


if __name__ == "__main__":
    unittest.main()
