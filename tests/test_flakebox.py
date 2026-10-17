"""Flakebox tests: synthetic JUnit XML, no network, stdlib only.

Portable by construction: no PyYAML (action.yml is read with a tiny flat
reader below), every text file is opened with an explicit encoding,
HOME/USERPROFILE/APPDATA point at a throwaway directory for the whole
module, and temp dirs are removed with a cleanup that tolerates
read-only or still-open files (Windows).
"""

import io
import json
import os
import random
import re
import shutil
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
        args = quar_mod.deselect_args(["b::t2", "a::t1", "a::t1"])
        self.assertEqual(args, ["--deselect", "a::t1", "--deselect", "b::t2"])

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
                                           "tests.test_a.TestA::test_flaky"])
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
        # must NOT invent a merge command: say plainly none exists
        self.assertIn("no merge command", text)
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


def reference_analyze(conn, min_runs=5, stable_runs=10, window=50):
    """Independent per-test (N+1) implementation of the detection rules
    (see detect.py's docstring), kept as the oracle for the batched
    analyze(): same classification, same counts, same order."""
    flaky, broken, stable = [], [], []
    for tid in store_mod.all_test_ids(conn):
        hist = store_mod.test_history(conn, tid, limit=window)  # newest first
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
        # 1 batched history query + 1 quarantine-set query
        self.assertLessEqual(len(calls), 3,
                             f"expected ~2 queries, got {len(calls)}")

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


if __name__ == "__main__":
    unittest.main()
