"""Flakebox tests: synthetic JUnit XML, no network, stdlib only.

Portable by construction: no PyYAML (action.yml is read with a tiny flat
reader below), every text file is opened with an explicit encoding,
HOME/USERPROFILE/APPDATA point at a throwaway directory for the whole
module, and temp dirs are removed with a cleanup that tolerates
read-only or still-open files (Windows).
"""

import argparse
import ast
import errno
import importlib.util
import io
import json
import os
import random
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

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


def child_env(**extra) -> dict:
    """The environment for a real subprocess: this process's (HOME and the
    rest already point at the fake home) without PYTHONUNBUFFERED, plus
    `extra`. A new dict every time."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    env.update(extra)
    return env


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


def action_steps(text: str) -> list:
    """The steps of action.yml as [{"name", "uses", "env", "run"}].

    Not a YAML parser, just enough for this file: a step is a `- ` item
    under `steps:`; `env:` is a mapping of NAME: value lines; `run:` is a
    scalar - the rest of its line plus every following line that is blank
    or indented deeper than the key. That covers `|` and `>` blocks (their
    common indent removed, `>` lines folded with spaces) and plain
    multi-line scalars. `run` is None for a step without one."""
    def indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def deeper(line: str, ki: int) -> bool:
        return not line.strip() or indent(line) > ki

    def unquote(s: str) -> str:  # a scalar wrapped whole in one kind of quote
        s = s.strip()
        return s[1:-1] if len(s) > 1 and s[0] == s[-1] and s[0] in "\"'" else s

    lines = text.splitlines()
    top = next(i for i, ln in enumerate(lines) if ln.strip() == "steps:")
    body = []
    for ln in lines[top + 1:]:
        if ln.strip() and not ln.lstrip().startswith("#") \
                and indent(ln) <= indent(lines[top]):
            break  # left the steps list
        body.append(ln)
    item = min(indent(ln) for ln in body if ln.lstrip().startswith("- "))
    chunks: list = []
    for ln in body:
        if indent(ln) == item and ln.lstrip().startswith("- "):
            chunks.append([" " * item + "  " + ln.lstrip()[2:]])
        elif chunks:
            chunks[-1].append(ln)
    steps = []
    for chunk in chunks:
        ki = indent(chunk[0])
        step: dict = {"name": None, "uses": None, "env": {}, "run": None}
        j = 0
        while j < len(chunk):
            raw = chunk[j]
            j += 1
            m = re.match(r"([A-Za-z_][\w-]*):(?:\s+(.*))?$", raw.strip())
            if indent(raw) != ki or not m or raw.lstrip().startswith("#"):
                continue
            key, val = m.group(1), (m.group(2) or "").strip()
            block = []
            while j < len(chunk) and deeper(chunk[j], ki):
                block.append(chunk[j])
                j += 1
            if key == "env":
                for e in block:
                    name, _, v = e.strip().partition(":")
                    if name and not name.startswith("#"):
                        step["env"][name.strip()] = unquote(v)
            elif key in ("name", "uses"):
                step[key] = val
            elif key == "run":
                style = re.fullmatch(r"([|>])[+-]?\d*(?:\s+#.*)?", val)
                if style:
                    first = next((b for b in block if b.strip()), "")
                    rows = [b[indent(first):] if b.strip() else ""
                            for b in block]
                    if style.group(1) == ">":  # folded: one line per paragraph
                        rows = [" ".join(p.split("\n")) for p in
                                "\n".join(rows).strip("\n").split("\n\n")]
                    step["run"] = "\n".join(rows).strip("\n") + "\n"
                else:
                    parts = [val] + [b.strip() for b in block if b.strip()]
                    step["run"] = unquote(" ".join(parts))
        steps.append(step)
    return steps


def expand_expressions(text: str, context: dict) -> str:
    """What GitHub does to `${{ expr }}` before a script ever runs: paste
    the value into the text. An expression missing from `context` is a
    KeyError, so a new one cannot slip in without this test seeing it."""
    return re.sub(r"\$\{\{\s*([^}]*?)\s*\}\}",
                  lambda m: context[m.group(1)], text)


def unquoted_variable_uses(script: str, names) -> list:
    """The `names` that bash `script` expands outside double quotes, where
    the shell would split them at spaces and expand globs. A small scanner
    for what the action's scripts use: '...', "...", $( ) nesting and
    backslash escapes."""
    found, stack, i = [], ["top"], 0
    while i < len(script):
        ch, ctx = script[i], stack[-1]
        if ch == "\\":
            i += 2
        elif ch == "'" and ctx != "dq":
            end = script.find("'", i + 1)
            i = len(script) if end < 0 else end + 1
        elif ch == '"':
            stack.pop() if ctx == "dq" else stack.append("dq")
            i += 1
        elif ch == "$" and script.startswith("$(", i):
            stack.append("cmd")
            i += 2
        elif ch == ")" and ctx == "cmd":
            stack.pop()
            i += 1
        elif ch == "$" and re.match(r"\$\{?(\w+)", script[i:]):
            m = re.match(r"\$\{?(\w+)", script[i:])
            if m.group(1) in names and ctx != "dq":
                found.append(m.group(1))
            i += m.end()
        else:
            i += 1
    return found


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
        # the input that feeds a CLI flag must spell that flag, and reach
        # it through the environment, never pasted into the script
        self.assertIn('--min-runs "$FB_MIN_RUNS"', text)
        self.assertIn("FB_MIN_RUNS: ${{ inputs.min-runs }}", text)

    def test_min_runs_input_feeds_a_valid_int(self):
        default = flat_yaml(action_text())["inputs"]["min-runs"]["default"]
        code, out, _ = run_cli("--db", os.path.join(mktemp(), "h.db"),
                               "report", "--format", "json",
                               "--min-runs", default)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["flaky"], [])


class TestActionShellInjection(unittest.TestCase):
    """R5-1: `${{ github.ref_name }}` and `${{ inputs.* }}` inside a `run:`
    script are pasted into the bash text before bash reads it, so a branch
    named  x";touch${IFS}pwned;echo"y  ran a command in the user's job.
    Every expression belongs in the step's `env:`, read as a quoted
    variable."""

    # the panel's branch name (a legal git ref) and the other ways out of
    # a quoted word
    HOSTILE = ['x";touch${IFS}pwned;echo"y', "$(touch pwned)", "`touch pwned`",
               "a b;touch pwned #", "';touch pwned;'", "x\ntouch pwned"]

    def run_steps(self):
        return [s for s in action_steps(action_text()) if s["run"] is not None]

    def test_no_expression_inside_a_run_script(self):
        steps = self.run_steps()
        self.assertGreaterEqual(len(steps), 6)  # install, record, report, ...
        for s in steps:
            self.assertNotIn("${{", s["run"], f"step {s['name']!r} pastes an "
                             f"expression into its script - use env:")

    def test_the_scan_sees_every_shape_of_run(self):
        sample = (
            "runs:\n  using: composite\n  steps:\n"
            "    # a comment before the first step\n"
            "    - name: inline\n      shell: bash\n"
            "      run: echo \"${{ github.ref_name }}\"\n"
            "    - name: literal\n      shell: bash\n      run: |\n"
            "        echo hi\n        echo \"${{ inputs.x }}\"\n"
            "    - name: folded\n      shell: bash\n      run: >\n"
            "        python3 -m x\n        --y \"${{ github.sha }}\"\n"
            "    - run: echo \"${{ inputs.first-key }}\"\n      shell: bash\n"
            "    - name: clean\n      shell: bash\n"
            "      env:\n        REF: ${{ github.ref_name }}\n"
            "      run: echo \"$REF\"\n"
            "    - name: cache\n      uses: actions/cache/save@v4\n"
            "      with:\n        key: ${{ inputs.k }}\n")
        steps = action_steps(sample)
        self.assertEqual([s["name"] for s in steps],
                         ["inline", "literal", "folded", None, "clean", "cache"])
        self.assertEqual([s["run"] is not None and "${{" in s["run"]
                          for s in steps],
                         [True, True, True, True, False, False])
        self.assertEqual(steps[2]["run"], 'python3 -m x --y "${{ github.sha }}"\n')
        self.assertEqual(steps[4]["env"], {"REF": "${{ github.ref_name }}"})
        self.assertEqual(steps[5]["uses"], "actions/cache/save@v4")
        self.assertIsNone(steps[5]["run"])

    def test_the_real_steps_were_found(self):
        steps = action_steps(action_text())
        names = [s["name"] for s in steps]
        for expected in ("Record results", "Report", "Export history (optional)",
                         "Fail on new flakes (optional)"):
            self.assertIn(expected, names)
        record = steps[names.index("Record results")]
        self.assertIn("flakebox record", record["run"])
        self.assertGreaterEqual(len(steps), 8)

    def test_variable_scanner(self):
        names = {"A", "B"}
        bad = lambda script: unquoted_variable_uses(script, names)
        self.assertEqual(bad('echo "$A" "${B}" "x $A y"'), [])
        self.assertEqual(bad('mkdir -p -- "$(dirname -- "$A")"'), [])
        self.assertEqual(bad("echo $A"), ["A"])
        self.assertEqual(bad('echo "$B" ${A}'), ["A"])
        self.assertEqual(bad('echo "$(echo $A)"'), ["A"])
        self.assertEqual(bad("echo '$A' \\$B $C"), [])  # quoted, escaped, other
        self.assertEqual(bad('python3 -c "\nprint(\'$A\')\n" "$B"'), [])

    def test_every_environment_value_is_read_quoted(self):
        carriers = 0
        for s in self.run_steps():
            # the env entries that carry an expression into the script
            names = {k for k, v in s["env"].items() if "${{" in v}
            carriers += len(names)
            for name in names:
                self.assertRegex(s["run"], r"\$\{?" + name + r"\b",
                                 f"step {s['name']!r}: {name} is never used")
            self.assertEqual(
                unquoted_variable_uses(s["run"], names), [],
                f"step {s['name']!r} expands an environment value unquoted")
        self.assertGreaterEqual(carriers, 8)

    @unittest.skipIf(os.name == "nt" or shutil.which("bash") is None,
                     "runs the scripts under a POSIX bash with shell shims")
    def test_hostile_values_never_reach_the_shell(self):
        """The attack itself: run the action's own scripts under bash, with
        every expression a hostile string, the way the runner would."""
        tmp = mktemp()
        shims = os.path.join(tmp, "bin")
        os.makedirs(shims)
        shim_py = os.path.join(tmp, "shim.py")
        write_raw(tmp, "shim.py", (
            "import json, os, sys\n"
            "tool, argv = sys.argv[1], sys.argv[2:]\n"
            "if tool == 'pip' or argv[:2] == ['-m', 'flakebox']:\n"
            "    with open(os.environ['FB_SHIM_LOG'], 'a', encoding='utf-8') as f:\n"
            "        f.write(json.dumps([tool] + argv) + '\\n')\n"
            "    if 'json' in argv:\n"
            "        print('{\"flaky\": [], \"quarantined\": [], \"new\": []}')\n"
            "    sys.exit(0)\n"
            "os.execv(sys.executable, [sys.executable] + argv)\n"))
        for tool in ("python3", "pip"):
            shim = os.path.join(shims, tool)
            write_raw(shims, tool, "#!/bin/sh\nexec %s %s %s \"$@\"\n" % (
                shlex.quote(sys.executable), shlex.quote(shim_py), tool))
            os.chmod(shim, 0o755)
        for value in self.HOSTILE:
            work = os.path.join(tmp, f"w{self.HOSTILE.index(value)}")
            os.makedirs(work)
            log = os.path.join(work, "calls.log")
            summary = os.path.join(work, "summary.md")
            output = os.path.join(work, "output.txt")
            context = {e: value for e in (
                "inputs.junit", "inputs.min-runs", "inputs.quarantine-output",
                "inputs.export-output", "github.sha", "github.ref_name",
                "github.action_path", "steps.report.outputs.new_flaky")}
            for step in self.run_steps():
                env = {"PATH": shims + os.pathsep + os.environ.get("PATH", ""),
                       "HOME": _fake_home, "FB_SHIM_LOG": log,
                       "GITHUB_STEP_SUMMARY": summary, "GITHUB_OUTPUT": output}
                env.update({k: expand_expressions(v, context)
                            for k, v in step["env"].items()})
                proc = subprocess.run(
                    ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail",
                     "-c", expand_expressions(step["run"], context)],
                    cwd=work, env=env, capture_output=True, text=True,
                    timeout=120)
                what = f"{step['name']} with {value!r}: {proc.stderr}"
                self.assertFalse(os.path.exists(os.path.join(work, "pwned")),
                                 f"a command ran: {what}")
                if step["name"].startswith("Fail on new flakes"):
                    self.assertEqual(proc.returncode, 1, what)
                    self.assertEqual(
                        proc.stdout,
                        f"::error::flakebox detected {value} new flaky test(s)\n")
                else:
                    self.assertEqual(proc.returncode, 0, what)
            with open(log, encoding="utf-8") as f:
                calls = [json.loads(ln) for ln in f.read().splitlines()]
            # every hostile string arrived as exactly one argument
            self.assertEqual(calls, [
                ["pip", "install", value],
                ["python3", "-m", "flakebox", "record", "--junit", value,
                 "--commit", value, "--branch", value],
                ["python3", "-m", "flakebox", "report", "--min-runs", value],
                ["python3", "-m", "flakebox", "report", "--format", "json",
                 "--min-runs", value],
                ["python3", "-m", "flakebox", "quarantine", "--format", "json",
                 "--min-runs", value],
                ["python3", "-m", "flakebox", "export", "--out", value]],
                f"for {value!r}")
            with open(output, encoding="utf-8") as f:
                self.assertEqual(f.read(), "new_flaky=0\n")
            # the output paths are used as one file name each
            self.assertTrue(os.path.isfile(os.path.join(work, value)), value)


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
                              capture_output=True, text=True, timeout=60,
                              env=child_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), cli_mod.VERSION)


class TestChildProcessEnvironment(unittest.TestCase):
    """A test that starts a real interpreter must not depend on how the
    harness running it is configured. PYTHONUNBUFFERED is the one that
    matters: it moves a child's failed stdout write from the flush (what
    TestBrokenPipe is about) to the print, so the test would quietly check
    something else."""

    @staticmethod
    def is_os_environ(node) -> bool:
        return (isinstance(node, ast.Attribute) and node.attr == "environ"
                and isinstance(node.value, ast.Name) and node.value.id == "os")

    def test_child_env_drops_pythonunbuffered_and_keeps_the_rest(self):
        saved = os.environ.get("PYTHONUNBUFFERED")
        os.environ["PYTHONUNBUFFERED"] = "1"
        try:
            env = child_env(FB_EXTRA="x")
        finally:
            if saved is None:
                os.environ.pop("PYTHONUNBUFFERED", None)
            else:
                os.environ["PYTHONUNBUFFERED"] = saved
        self.assertNotIn("PYTHONUNBUFFERED", env)
        self.assertEqual(env["FB_EXTRA"], "x")
        self.assertEqual(env["HOME"], _fake_home)  # the fake home reaches the child
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))
        env["FB_EXTRA"] = "y"  # a copy: this process is untouched
        self.assertNotIn("FB_EXTRA", os.environ)

    def test_no_subprocess_in_this_file_inherits_the_environment_unfiltered(self):
        with open(os.path.join(REPO, "tests", "test_flakebox.py"),
                  encoding="utf-8") as f:
            tree = ast.parse(f.read())
        problems = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.func.attr in ("run", "Popen", "check_output",
                                           "check_call", "call")
                    and not any(k.arg == "env" for k in node.keywords)):
                problems.append(f"line {node.lineno}: subprocess."
                                f"{node.func.attr}() has no env=")
        for top in tree.body:
            if isinstance(top, ast.FunctionDef) and top.name == "child_env":
                continue  # the one place that may read the whole environment
            for node in ast.walk(top):
                inherits = False
                if isinstance(node, ast.Call):
                    fn = node.func
                    inherits = (
                        (isinstance(fn, ast.Name) and fn.id == "dict"
                         and len(node.args) == 1
                         and self.is_os_environ(node.args[0]))
                        or (isinstance(fn, ast.Attribute) and fn.attr == "copy"
                            and self.is_os_environ(fn.value)))
                elif isinstance(node, ast.Dict):
                    inherits = any(k is None and self.is_os_environ(v)
                                   for k, v in zip(node.keys, node.values))
                if inherits:
                    problems.append(f"line {node.lineno}: copies the whole "
                                    f"environment without child_env()")
        self.assertEqual(problems, [])


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
    """Argument text of every `open(...)` call, parentheses balanced
    (`os.open(...)` hands out a file descriptor and has no encoding)."""
    out = []
    for m in re.finditer(r"(?<![.\w])open\(", src):
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
<!DOCTYPE testsuite [<!ENTITY lol "lollollollol">]>
<testsuite name="pytest" tests="1">
  <testcase classname="a" name="t" time="0.1">&lol;</testcase>
</testsuite>"""

# an ENTITY declaration where none can be: not XML at all, so not a DTD either
STRAY_ENTITY = """<?xml version="1.0"?>
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

    def test_a_lowercase_doctype_is_refused_too(self):
        # not a DOCTYPE to expat (keywords are case-sensitive), so it is
        # refused as not well-formed instead; either way nothing is parsed
        from xml.etree.ElementTree import ParseError
        p = write_raw(mktemp(), "lower.xml", LOWERCASE_DOCTYPE)
        with self.assertRaises((ValueError, ParseError)):
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


ENCODINGS = ("utf-8", "utf-16", "utf-16-le", "utf-16-be", "latin-1")


def declared(xml: str, enc: str) -> str:
    """`xml` with the encoding its declaration names set to match `enc`
    (the -le / -be forms are written without a byte order mark)."""
    name = ("utf-16" if enc.startswith("utf-16") else
            "iso-8859-1" if enc == "latin-1" else enc)
    return xml.replace("utf-8", name)


def suite_xml(inner: str = "", prolog: str = "") -> str:
    """A one-test JUnit report; `inner` goes inside the testcase."""
    return (f'<?xml version="1.0" encoding="utf-8"?>{prolog}'
            f'<testsuite name="s"><testcase classname="a" name="t" time="0.1">'
            f'{inner}</testcase></testsuite>')


class TestDtdGuardScope(unittest.TestCase):
    """R5-4: the guard was a raw, case-insensitive scan of every byte of the
    file, so a report whose captured output merely CONTAINS the text
    "<!DOCTYPE html>" (a CDATA section, a comment, escaped text) was refused
    as a possible XML bomb. Only a declaration is one: expat now tokenises,
    and the document is stopped at the first DOCTYPE or ENTITY."""

    def mentions(self) -> dict:
        return {
            "cdata": suite_xml('<system-out><![CDATA[<!DOCTYPE html><html>'
                               '<!ENTITY x "y"></html>]]></system-out>'),
            "cdata in a failure": suite_xml(
                '<failure message="m"><![CDATA[<!DOCTYPE html>]]></failure>'),
            "escaped text": suite_xml(
                "<system-out>&lt;!DOCTYPE html&gt;</system-out>"),
            "escaped attribute": suite_xml().replace(
                'name="t"', 'name="&lt;!DOCTYPE html&gt;"'),
            "comment": suite_xml("<!-- <!DOCTYPE html> <!ENTITY a 'b'> -->"),
            "comment in the prolog": suite_xml(
                prolog="<!-- <!DOCTYPE html> -->"),
            "processing instruction": suite_xml(
                prolog="<?note <!DOCTYPE html> ?>"),
        }

    def test_text_that_mentions_a_doctype_is_not_a_declaration(self):
        for name, xml in self.mentions().items():
            rs = junit_mod.parse_bytes(xml.encode("utf-8"), name + ".xml")
            self.assertEqual([r["outcome"] for r in rs],
                             ["failed" if "failure" in name else "passed"], name)

    def test_the_same_in_every_encoding_expat_reads(self):
        for enc in ENCODINGS:
            for name, xml in self.mentions().items():
                raw = declared(xml, enc).encode(enc)
                self.assertEqual(
                    len(junit_mod.parse_bytes(raw, f"{name}-{enc}.xml")), 1,
                    f"{name} as {enc}")

    def test_cli_record_accepts_a_report_that_mentions_a_doctype(self):
        tmp = mktemp()
        p = write_raw(tmp, "html.xml", self.mentions()["cdata"])
        code, out, err = run_cli("--db", os.path.join(tmp, "h.db"),
                                 "record", "--junit", p)
        self.assertEqual(code, 0, err)
        self.assertIn("recorded run 1: 1 tests", out)

    def test_real_declarations_are_still_refused(self):
        entities = ('<!DOCTYPE s [<!ENTITY a "x"><!ENTITY b "&a;&a;&a;">]>')
        shapes = {
            "doctype only": "<!DOCTYPE s>",
            "external dtd": '<!DOCTYPE s SYSTEM "http://127.0.0.1/s.dtd">',
            "public dtd": '<!DOCTYPE s PUBLIC "-//X//EN" "s.dtd">',
            "internal subset": entities,
            "after a comment": "<!-- c -->" + entities,
            "after a processing instruction": "<?pi x?>" + entities,
            "with surrounding whitespace": "\n  " + entities + "\n",
        }
        for name, prolog in shapes.items():
            for enc in ENCODINGS:
                raw = declared(suite_xml(prolog=prolog), enc).encode(enc)
                with self.assertRaisesRegex(ValueError, "DOCTYPE",
                                            msg=f"{name} as {enc}"):
                    junit_mod.parse_bytes(raw, "x.xml")
        # with a byte order mark, and as the very first thing in the file
        with self.assertRaisesRegex(ValueError, "DOCTYPE"):
            junit_mod.parse_bytes(b"\xef\xbb\xbf" + b"<!DOCTYPE s><s/>", "x.xml")

    def test_a_billion_laughs_document_is_refused_before_it_is_expanded(self):
        levels = "".join('<!ENTITY l%d "%s">' % (i, ("&l%d;" % (i - 1)) * 10)
                         for i in range(1, 10))
        xml = ('<?xml version="1.0"?><!DOCTYPE s [<!ENTITY l0 "lol">' + levels
               + ']><testsuite><testcase classname="a" name="&l9;"/></testsuite>')
        with self.assertRaisesRegex(ValueError, "DOCTYPE"):
            junit_mod.parse_bytes(xml.encode("utf-8"), "laughs.xml")

    def test_not_well_formed_is_a_parse_error_not_a_dtd_claim(self):
        from xml.etree.ElementTree import ParseError
        for name, raw in {"stray entity declaration": STRAY_ENTITY,
                          "lowercase doctype": LOWERCASE_DOCTYPE,
                          "garbage": "not xml", "empty": ""}.items():
            with self.assertRaises(ParseError, msg=name):
                junit_mod.parse_bytes(raw.encode("utf-8"), "bad.xml")
        tmp = mktemp()
        code, _, err = run_cli("--db", os.path.join(tmp, "h.db"), "record",
                               "--junit", write_raw(tmp, "stray.xml", STRAY_ENTITY))
        self.assertEqual(code, 2)
        self.assertIn("cannot parse", err)
        self.assertNotIn("Traceback", err)


class TestPackaging(unittest.TestCase):
    def test_version_in_pyproject_matches_the_code_on_every_python(self):
        # no tomllib here (3.11+): read the one line
        with open(os.path.join(REPO, "pyproject.toml"), encoding="utf-8") as f:
            m = re.search(r'(?m)^version = "([^"]+)"\s*$', f.read())
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), cli_mod.VERSION)

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
    analyze(): same classification, same counts, same order. Recency is
    worked out here, in Python, from (created_at, id) - not by the SQL the
    code under test uses."""
    flaky, broken, stable, retired = [], [], [], []
    never = float("-inf")  # a run without a timestamp is the oldest
    newest_first = sorted(
        conn.execute("SELECT id, created_at FROM runs").fetchall(),
        key=lambda r: (never if r[1] is None else r[1], r[0]), reverse=True)
    rank = {run_id: n for n, (run_id, _) in enumerate(newest_first)}
    recent = [run_id for run_id, _ in newest_first[:max(retire_after, 0)]]
    all_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT test_id FROM results ORDER BY rowid")]
    for tid in all_ids:
        seen = sorted((rank[run_id], run_id, outcome) for run_id, outcome in
                      conn.execute("SELECT run_id, outcome FROM results "
                                   "WHERE test_id = ?", (tid,))
                      if run_id in rank)[:max(window, 0)]  # newest first
        if not seen:
            continue
        last_run = seen[0][1]
        outcomes = [o for _, _, o in seen]
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
        if recent and last_run not in recent:
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
    retired.sort(key=lambda s: (rank[s["last_run"]], s["test_id"]))
    return {"flaky": flaky, "broken": broken, "stable": stable,
            "retired": retired}


EQUIVALENCE_QUARANTINED = (3, 37, 66, 99, 121, 130, 140)


def equivalence_history() -> list:
    """~3.6k rows of seeded-random history incl. quarantined, flaky,
    broken, skip-heavy, and late-joiner (< min_runs) tests, as
    [(commit, results)] for its runs, oldest first."""
    rng = random.Random(20261015)
    n_tests, n_runs = 150, 24
    kinds = []
    for _ in range(n_tests):
        r = rng.random()
        kinds.append("pass" if r < 0.4 else "fail" if r < 0.6 else
                     "flaky" if r < 0.9 else "skippy")
    runs = []
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
        runs.append((f"sha{run}", results))
    return runs


def make_equivalence_db(tmp: str) -> str:
    db = os.path.join(tmp, "eq.db")
    conn = store_mod.connect(db)
    for commit, results in equivalence_history():
        store_mod.record_run(conn, results, commit_sha=commit)
    # quarantine a spread across kinds
    for i in EQUIVALENCE_QUARANTINED:
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


def verdicts(analysis: dict) -> dict:
    """An analyze() result keyed by test, without the run ids (which depend
    on the order the runs arrived in) - what must not depend on it."""
    return {bucket: {s["test_id"]: {k: v for k, v in s.items() if k != "last_run"}
                     for s in analysis[bucket]}
            for bucket in analysis}


class TestRecencyOrder(unittest.TestCase):
    """R5-2: "the newest N runs" was the N highest row ids, but an import
    appends rows, so older runs imported after newer local ones got the
    higher ids: a test that ran in every local run was reported retired and
    a recovered one flaky. Recency is the run's created_at; the row id only
    breaks ties."""

    def local_db(self, tmp: str) -> str:
        db = os.path.join(tmp, "local.db")
        conn = store_mod.connect(db)
        for i in range(10):  # the newest runs, recorded here
            store_mod.record_run(conn, [result("t::live", "passed"),
                                        result("t::flaky", "passed")],
                                 commit_sha=f"new{i}", created_at=1000 + i)
        conn.close()
        return db

    def older_export(self, tmp: str) -> str:
        return write_raw(tmp, "older.json", json.dumps({
            "flakebox_export": 1, "quarantine": [],
            "runs": [{"commit": f"old{i}", "branch": None,
                      "created_at": 100 + i, "report_sha": f"old-report-{i}",
                      "results": [["t::flaky", "failed", 1, ""],
                                  ["t::old", "failed", 1, ""]]}
                     for i in range(10)]}))

    def test_importing_an_older_export_does_not_make_old_runs_new(self):
        tmp = mktemp()
        db = self.local_db(tmp)
        code, out, _ = run_cli("--db", db, "import", self.older_export(tmp))
        self.assertEqual(code, 0)
        self.assertIn("imported 10 run(s)", out)
        code, out, _ = run_cli("--db", db, "report", "--format", "json")
        a = json.loads(out)
        # t::live ran in all of the newest runs; t::old only in the old ones
        self.assertEqual([s["test_id"] for s in a["retired"]], ["t::old"])
        self.assertEqual(a["retired"][0]["last_commit"], "old9")
        # t::flaky failed ten times long ago and has passed ten times since
        self.assertEqual(a["flaky"], [])
        self.assertEqual(a["broken"], [])
        conn = store_mod.connect(db)
        try:
            hist = store_mod.test_history(conn, "t::flaky", limit=12)
            self.assertEqual([h["commit"] for h in hist],
                             [f"new{i}" for i in range(9, -1, -1)]
                             + ["old9", "old8"])
            self.assertEqual(detect_mod.summarize(conn, "t::flaky")["status"],
                             detect_mod.RECOVERED)
            self.assertFalse(detect_mod.summarize(conn, "t::live")["retired"])
            self.assertTrue(detect_mod.summarize(conn, "t::old")["retired"])
            # the imported runs kept the time they were recorded at
            self.assertEqual(
                [r[0] for r in conn.execute(
                    "SELECT created_at FROM runs WHERE commit_sha LIKE 'old%' "
                    "ORDER BY id")], list(range(100, 110)))
            newest = [conn.execute("SELECT commit_sha FROM runs WHERE id = ?",
                                   (rid,)).fetchone()[0]
                      for rid in store_mod.recent_run_ids(conn, 3)]
            self.assertEqual(newest, ["new9", "new8", "new7"])
        finally:
            conn.close()
        # the CLI's history command agrees
        code, out, _ = run_cli("--db", db, "history", "--test", "t::flaky")
        self.assertIn("t::flaky: recovered (10 consecutive passes)", out)

    def test_arrival_order_never_changes_a_verdict(self):
        tmp = mktemp()
        runs = equivalence_history()
        n = len(runs)
        shuffled = list(range(n))
        random.Random(7).shuffle(shuffled)
        orders = {"oldest first": list(range(n)),
                  "newest first": list(range(n))[::-1],
                  "shuffled": shuffled}
        kw = dict(min_runs=5, stable_runs=5, window=10)
        seen, histories = {}, {}
        for name, arrival in orders.items():
            db = os.path.join(tmp, name.replace(" ", "_") + ".db")
            conn = store_mod.connect(db)
            for k in arrival:  # run k was made at 1000 + 10 * k
                commit, results = runs[k]
                store_mod.record_run(conn, results, commit_sha=commit,
                                     created_at=1000 + 10 * k)
            for i in EQUIVALENCE_QUARANTINED:
                store_mod.quarantine_add(conn, f"mod{i:03d}::test_{i:03d}", "auto")
            try:
                a = detect_mod.analyze(conn, **kw)
                # the batched query and the per-test oracle agree on this DB
                self.assertEqual(a, reference_analyze(conn, **kw), name)
                live = detect_mod.analyze(conn, retire_after=0, **kw)
                self.assertEqual(live, reference_analyze(
                    conn, retire_after=0, **kw), name)
                histories[name] = {
                    tid: [h["commit"] for h in
                          store_mod.test_history(conn, tid, limit=10)]
                    for tid in ("mod000::test_000", "mod060::test_060",
                                "mod140::test_140", "mod147::test_147")}
            finally:
                conn.close()
            seen[name] = verdicts(a)
        base = seen["oldest first"]
        self.assertEqual(len(base["retired"]), 7)
        for bucket in ("flaky", "broken", "stable"):
            self.assertTrue(base[bucket], bucket)
        self.assertEqual(seen["newest first"], base)
        self.assertEqual(seen["shuffled"], base)
        self.assertEqual(histories["newest first"], histories["oldest first"])
        self.assertEqual(histories["shuffled"], histories["oldest first"])
        self.assertEqual(histories["oldest first"]["mod000::test_000"],
                         [f"sha{k}" for k in range(n - 1, n - 11, -1)])

    def test_equal_timestamps_fall_back_to_the_order_runs_arrived_in(self):
        conn = store_mod.connect(":memory:")
        for i in range(4):
            store_mod.record_run(conn, [result("t::x", "passed")],
                                 commit_sha=f"c{i}", created_at=500)
        self.assertEqual([h["commit"] for h in
                          store_mod.test_history(conn, "t::x")],
                         ["c3", "c2", "c1", "c0"])
        self.assertEqual(store_mod.recent_run_ids(conn, 2), [4, 3])
        conn.close()

    def test_a_run_exported_without_a_timestamp_is_imported_as_the_oldest(self):
        tmp = mktemp()
        db = os.path.join(tmp, "u.db")
        conn = store_mod.connect(db)
        store_mod.record_run(conn, [result("t::x", "passed")],
                             commit_sha="local", created_at=1000)
        conn.close()
        exp = write_raw(tmp, "undated.json", json.dumps({
            "flakebox_export": 1, "quarantine": [],
            "runs": [{"commit": "undated", "report_sha": "u1",
                      "created_at": None,
                      "results": [["t::x", "failed", 1, ""]]}]}))
        code, out, _ = run_cli("--db", db, "import", exp)
        self.assertEqual(code, 0)
        conn = store_mod.connect(db)
        self.assertEqual([h["commit"] for h in
                          store_mod.test_history(conn, "t::x")],
                         ["local", "undated"])
        conn.close()


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
                env=child_env(PYTHONDONTWRITEBYTECODE="1"))
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


class TestDurationValues(unittest.TestCase):
    """R5-3: `duration_ms("inf")` raised OverflowError (only ValueError was
    caught), so a report with one such `time` was rejected whole although
    the README promises 0 and a warning; a negative time was stored as a
    negative duration, and `1e300` crashed `record` when SQLite refused the
    integer."""

    UNUSABLE = ("inf", "-inf", "+inf", "Infinity", "nan", "1e999", "-1e999",
                "-5", "-0.001", "1e300", "1e16", "fast", "1e308")

    def test_unusable_times_are_zero_with_one_warning_each(self):
        for text in self.UNUSABLE:
            warnings: list = []
            self.assertEqual(
                junit_mod.duration_ms(text, "a::t", warn=warnings.append), 0, text)
            self.assertEqual(len(warnings), 1, text)
            self.assertIn("a::t", warnings[0])
            self.assertIn(repr(text), warnings[0])

    def test_usable_times_are_unchanged_and_silent(self):
        cases = {"0.5": 500, "0,5": 500, "86400": 86400000, "0": 0, "-0": 0,
                 "": 0, "  2  ": 2000, "1e3": 1000000, "9e15": 9000000000000000000}
        for text, expected in cases.items():
            warnings: list = []
            self.assertEqual(
                junit_mod.duration_ms(text, "a::t", warn=warnings.append),
                expected, text)
            self.assertEqual(warnings, [], text)
        self.assertEqual(junit_mod.duration_ms(None), 0)

    def test_record_keeps_the_report_and_stores_zero(self):
        tmp = mktemp()
        cases = ["inf", "-5", "1e300", "nan", "0.25"]
        xml = "<testsuite>" + "".join(
            f'<testcase classname="a" name="t{i}" time="{t}"/>'
            for i, t in enumerate(cases)) + "</testsuite>"
        p = write_raw(tmp, "weird.xml", xml)
        db = os.path.join(tmp, "h.db")
        code, out, err = run_cli("--db", db, "record", "--junit", p)
        self.assertEqual(code, 0, err)
        self.assertIn("recorded run 1: 5 tests", out)
        self.assertEqual(err.count("warning"), 4, err)
        self.assertIn("a::t0", err)
        conn = store_mod.connect(db)
        durations = [r[0] for r in conn.execute(
            "SELECT duration_ms FROM results ORDER BY rowid")]
        conn.close()
        self.assertEqual(durations, [0, 0, 0, 0, 250])


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
        # a legacy run has no report hash: it is stamped with a content
        # fingerprint instead (round 4), so export/import recognises it
        exp = os.path.join(tmp, "e.json")
        self.assertEqual(run_cli("--db", db, "export", "--out", exp)[0], 0)
        with open(exp, encoding="utf-8") as f:
            sha = json.load(f)["runs"][0]["report_sha"]
        self.assertTrue(sha.startswith(store_mod.LEGACY_PREFIX), sha)


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
        # a hard-coded test count goes stale with the next test (it said
        # 90 while `unittest discover` ran 97)
        self.assertNotRegex(readme, r"\b\d+ tests?\b")
        for phrase in ("flakebox export", "flakebox import", "quarantine-output",
                       "export-output", "retired", "--retire-after", "best effort",
                       "flakebox report --db", "already recorded", "--force",
                       "PowerShell", "fingerprint", "recovered",
                       "refused as a whole before anything is written"):
            self.assertIn(phrase, " ".join(readme.split()), phrase)

    def test_readme_does_not_promise_what_a_bare_substitution_cannot_do(self):
        """R5-5: `pytest $(...)` splits an id at its space (see
        TestDeselectInTheShell); only `eval` re-parses the quoting."""
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            raw = f.read()
        flat = " ".join(raw.split())
        self.assertIn('eval "pytest $(flakebox quarantine --format deselect)"', flat)
        self.assertIn("A bare `pytest $(flakebox quarantine --format deselect)` "
                      "does not", flat)
        self.assertNotIn("survives `pytest $(...)` as one argument", flat)
        # no usage line runs the substitution bare
        self.assertNotRegex(raw, r"(?m)^pytest \$\(")


# ---------------------------------------------------------------------------
# Round 4 panel findings
# ---------------------------------------------------------------------------

HAVE_PYTEST = importlib.util.find_spec("pytest") is not None

PARAM_TESTS = (
    "import pytest\n\n"
    "@pytest.mark.parametrize('v', ['1.5', '2.0'])\n"
    "def test_version(v):\n    pass\n\n"
    "@pytest.mark.parametrize('v', ['0.5'])\n"
    "def test_unrelated(v):\n    pass\n\n"
    "@pytest.mark.parametrize('x', ['a b', 'c.d/e'])\n"
    "def test_param(x):\n    pass\n\n"
    "def test_plain():\n    pass\n"
)


def pytest_outcomes(cwd: str, extra: list) -> dict:
    """Run real pytest on `cwd`/tests and return {junit id: outcome}."""
    report = os.path.join(cwd, "j.xml")
    if os.path.exists(report):
        os.remove(report)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--junitxml", report, "tests"] + extra,
        cwd=cwd, capture_output=True, text=True, timeout=120,
        env=child_env(PYTHONDONTWRITEBYTECODE="1"))
    assert proc.returncode in (0, 5), proc.stdout + proc.stderr
    return {r["test_id"]: r["outcome"] for r in junit_mod.parse(report)}


class TestParametrisedIds(unittest.TestCase):
    """R4-1: the plugin split the final segment on `.`, so
    `test_version[1.5]` became `5]` and `test_unrelated[0.5]` was skipped
    too (a false green)."""

    def test_matcher_keeps_parameters_whole(self):
        m = exec_plugin(mktemp(), ["placeholder::x"])["_matches"]
        self.assertTrue(m("tests/test_c.py::test_version[1.5]",
                          "tests.test_c::test_version[1.5]"))
        self.assertFalse(m("tests/test_c.py::test_unrelated[0.5]",
                           "tests.test_c::test_version[1.5]"))
        self.assertFalse(m("tests/test_c.py::test_version[2.0]",
                           "tests.test_c::test_version[1.5]"))
        # path characters inside the parameters are parameters, not paths
        self.assertTrue(m("tests/test_c.py::test_param[c.d/e]",
                          "tests.test_c::test_param[c.d/e]"))
        self.assertFalse(m("tests/test_c.py::test_param[c.d/e]",
                           "tests.test_c::test_param[x.d/e]"))
        self.assertTrue(m("tests/test_a.py::TestA::test_x[a-b.c]",
                          "tests.test_a.TestA::test_x[a-b.c]"))
        self.assertFalse(m("tests/test_a.py::TestA::test_x[a-b.c]",
                           "tests.test_a.TestA::test_x"))

    @unittest.skipUnless(HAVE_PYTEST, "pytest not installed")
    def test_real_pytest_skips_only_the_quarantined_ids(self):
        tmp = mktemp()
        tdir = os.path.join(tmp, "tests")
        os.makedirs(tdir)
        write_raw(tdir, "test_c.py", PARAM_TESTS)
        quar_mod.write_pytest(tmp, ["tests.test_c::test_version[1.5]",
                                    "tests.test_c::test_param[a b]"])
        outcomes = pytest_outcomes(tmp, ["-p", "flakebox_quarantine"])
        self.assertEqual(outcomes, {
            "tests.test_c::test_version[1.5]": "skipped",
            "tests.test_c::test_version[2.0]": "passed",
            "tests.test_c::test_unrelated[0.5]": "passed",
            "tests.test_c::test_param[a b]": "skipped",
            "tests.test_c::test_param[c.d/e]": "passed",
            "tests.test_c::test_plain": "passed"})


class TestDeselectQuoting(unittest.TestCase):
    """R4-2: `--format deselect` printed unquoted ids; `test_param[a b]`
    became two words in `pytest $(...)`."""

    IDS = ["tests.test_c::test_param[a b]", "tests.test_c::test_param[$x;y]",
           "tests.test_a.TestA::test_x"]

    def test_line_is_shell_quoted(self):
        line = quar_mod.deselect_line(self.IDS)
        args = quar_mod.deselect_args(self.IDS)
        self.assertEqual(args[1::2], ["tests/test_a.py::TestA::test_x",
                                      "tests/test_c.py::test_param[$x;y]",
                                      "tests/test_c.py::test_param[a b]"])
        if os.name == "nt":
            self.assertIn('"tests/test_c.py::test_param[a b]"', line)
        else:
            self.assertEqual(shlex.split(line), args)
            self.assertIn("'tests/test_c.py::test_param[a b]'", line)
            self.assertIn("'tests/test_c.py::test_param[$x;y]'", line)
        # a plain id is left bare on every platform
        self.assertEqual(quar_mod.deselect_line(["m.T::test_a"]),
                         "--deselect m.py::T::test_a")
        self.assertEqual(quar_mod.deselect_line(["pkg.mod::test_a"]),
                         "--deselect pkg/mod.py::test_a")

    def test_cli_output_round_trips_through_the_shell(self):
        tmp = mktemp()
        db = os.path.join(tmp, "q.db")
        conn = store_mod.connect(db)
        for tid in self.IDS:
            store_mod.quarantine_add(conn, tid)
        conn.close()
        code, out, err = run_cli("--db", db, "quarantine", "--format", "deselect")
        self.assertEqual(code, 0)
        self.assertIn("3 in quarantine", err)
        if os.name != "nt":
            self.assertEqual(shlex.split(out), quar_mod.deselect_args(self.IDS))

    @unittest.skipUnless(HAVE_PYTEST, "pytest not installed")
    def test_real_pytest_deselects_a_parametrised_id_with_a_space(self):
        tmp = mktemp()
        tdir = os.path.join(tmp, "tests")
        os.makedirs(tdir)
        write_raw(tdir, "test_c.py", PARAM_TESTS)
        line = quar_mod.deselect_line(["tests.test_c::test_param[a b]",
                                       "tests.test_c::test_version[1.5]"])
        # what `eval "pytest $(flakebox quarantine --format deselect)"` hands
        # pytest (a bare $(...) would split the id at its space)
        words = shlex.split(line, posix=(os.name != "nt"))
        if os.name == "nt":
            words = [w.strip('"') for w in words]
        outcomes = pytest_outcomes(tmp, words)
        self.assertEqual(sorted(outcomes), [
            "tests.test_c::test_param[c.d/e]", "tests.test_c::test_plain",
            "tests.test_c::test_unrelated[0.5]", "tests.test_c::test_version[2.0]"])


@unittest.skipIf(os.name == "nt" or shutil.which("sh") is None,
                 "drives a POSIX shell; on Windows the line is quoted for cmd.exe")
class TestDeselectInTheShell(unittest.TestCase):
    """R5-5: the README said a parametrised id with a space survives
    `pytest $(flakebox quarantine --format deselect)` as one argument. It
    does not: command substitution splits the output at whitespace and never
    removes the quotes the line carries. `eval` parses the line again and
    does hand pytest every id intact. These tests run the real shell."""

    NASTY = ["tests.test_c::test_param[a b]",
             "tests.test_c::test_param[$x;y]",
             "tests.test_c::test_param[$(touch pwned)]",
             "tests.test_c::test_param[`touch pwned`]",
             "tests.test_c::test_param[it's]",
             'tests.test_c::test_param["q"]',
             "tests.test_c::test_param[back\\slash]",
             "tests.test_c::test_param[*?[x]]",
             "tests.test_c::test_param[tab\there]",
             "tests.test_c::test_param[line\nbreak]",
             "tests.test_c::test_plain"]
    # `pytest` prints its arguments NUL-separated; `flakebox` is this checkout
    PRELUDE = ('pytest() { for a in "$@"; do printf "%s\\0" "$a"; done; }\n'
               'flakebox() { "$FB_PYTHON" -m flakebox "$@"; }\n')

    def quarantine_db(self, tmp: str, ids) -> str:
        db = os.path.join(tmp, "q.db")
        conn = store_mod.connect(db)
        for tid in ids:
            store_mod.quarantine_add(conn, tid)
        conn.close()
        return db

    def shell(self, tmp: str, script: str, db: str) -> list:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": _fake_home,
               "FB_PYTHON": sys.executable, "FB_DB": db, "PYTHONPATH": REPO}
        proc = subprocess.run(["sh", "-c", self.PRELUDE + script], cwd=tmp,
                              env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.split("\0")[:-1]

    def test_eval_hands_pytest_every_id_intact_and_runs_nothing(self):
        tmp = mktemp()
        db = self.quarantine_db(tmp, self.NASTY)
        # the README's command, verbatim (plus --db)
        words = self.shell(
            tmp, 'eval "pytest $(flakebox --db "$FB_DB" quarantine '
                 '--format deselect 2>/dev/null)"', db)
        self.assertEqual(words, quar_mod.deselect_args(self.NASTY))
        self.assertEqual(len(words), 2 * len(self.NASTY))
        self.assertFalse(os.path.exists(os.path.join(tmp, "pwned")))

    def test_bare_command_substitution_splits_an_id_with_a_space(self):
        tmp = mktemp()
        db = self.quarantine_db(tmp, ["tests.test_c::test_param[a b]"])
        words = self.shell(
            tmp, 'pytest $(flakebox --db "$FB_DB" quarantine --format deselect '
                 '2>/dev/null)', db)
        # what the README used to promise was one argument arrives in two
        # pieces, the line's quotes still attached
        self.assertEqual(words, ["--deselect", "'tests/test_c.py::test_param[a",
                                 "b]'"])
        self.assertNotEqual(words, quar_mod.deselect_args(
            ["tests.test_c::test_param[a b]"]))

    def test_plain_ids_need_no_eval(self):
        tmp = mktemp()
        ids = ["tests.test_a.TestA::test_flaky", "tests.test_b::test_broken"]
        db = self.quarantine_db(tmp, ids)
        words = self.shell(
            tmp, 'pytest $(flakebox --db "$FB_DB" quarantine --format deselect '
                 '2>/dev/null)', db)
        self.assertEqual(words, quar_mod.deselect_args(ids))


class TestExportValidation(unittest.TestCase):
    """R4-3: a malformed export (`"runs": [1, 2]`) was an AttributeError
    traceback halfway through the import."""

    def test_bad_shapes_are_clean_exit_2(self):
        tmp = mktemp()
        db = os.path.join(tmp, "x.db")
        cases = {
            "runs_ints.json": ('{"flakebox_export": 1, "runs": [1, 2]}',
                               "runs[0] must be an object"),
            "runs_obj.json": ('{"flakebox_export": 1, "runs": {}}',
                              '"runs" must be a list'),
            "results.json": ('{"flakebox_export": 1, "runs": [{"commit": "c", '
                             '"results": [["a::t", "passed", 1]]}]}',
                             "runs[0].results[0] must be"),
            "results_str.json": ('{"flakebox_export": 1, "runs": [{"results": "x"}]}',
                                 "runs[0].results must be a list"),
            "commit.json": ('{"flakebox_export": 1, "runs": [{"commit": 5}]}',
                            "runs[0].commit has the wrong type"),
            "quar.json": ('{"flakebox_export": 1, "runs": [], "quarantine": [1]}',
                          "quarantine[0] must be an object"),
            "quar_obj.json": ('{"flakebox_export": 1, "quarantine": {"a": 1}}',
                              '"quarantine" must be a list'),
        }
        for name, (text, expect) in cases.items():
            p = write_raw(tmp, name, text)
            code, _, err = run_cli("--db", db, "import", p)
            self.assertEqual(code, 2, name)
            self.assertIn("cannot import", err)
            self.assertIn(expect, err, name)
            self.assertNotIn("Traceback", err)
        # nothing was written by any of the failed imports
        conn = store_mod.connect(db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        conn.close()
        # a minimal valid export still imports
        p = write_raw(tmp, "ok.json", '{"flakebox_export": 1, "runs": [{"commit": "c",'
                      ' "created_at": 5, "results": [["a::t", "passed", 1, null]]}]}')
        code, out, _ = run_cli("--db", db, "import", p)
        self.assertEqual(code, 0)
        self.assertIn("imported 1 run(s)", out)

    # R5-6: results[i][2] (duration_ms), results[i][3] (message) and a
    # quarantine entry's reason / at were never checked, so a file with a
    # bad value there passed the upfront check and failed halfway through
    # the import (after earlier runs were committed), as a misleading
    # "cannot use database" or a ValueError from int(). The version check
    # accepted `true` and `1.0` (== 1) as well.

    def test_a_bad_value_anywhere_refuses_the_whole_file(self):
        tmp = mktemp()
        db = os.path.join(tmp, "x.db")
        good_run = {"commit": "g", "branch": "main", "created_at": 5,
                    "report_sha": "gg", "results": [["a::t", "passed", 1, "ok"]]}
        good_q = {"test_id": "q::good", "reason": "auto", "at": 7}
        surrogate = "a::t" + chr(0xD800)  # valid in JSON, not in UTF-8

        def doc(run=None, quarantine=None, **top):
            d = {"flakebox_export": 1,
                 "runs": [good_run] + ([run] if run is not None else []),
                 "quarantine": [good_q] + ([quarantine] if quarantine is not None else [])}
            d.update(top)
            return d

        def run_with(**fields):
            r = {"commit": "b", "created_at": 6, "report_sha": "bb",
                 "results": [["a::t", "passed", 1, "m"]]}
            r.update(fields)
            return doc(run=r)

        def row(*cells):
            return run_with(results=[list(cells)])

        huge = 10 ** 400  # an int no float, and no SQLite INTEGER, can hold
        cases = [
            ("duration is an object", row("a::t", "passed", {"x": 1}, "m"),
             "runs[1].results[0][2] (duration_ms) has the wrong type (dict)"),
            ("duration is a string", row("a::t", "passed", "12", "m"),
             "runs[1].results[0][2] (duration_ms) has the wrong type (str)"),
            ("duration is a bool", row("a::t", "passed", True, "m"),
             "runs[1].results[0][2] (duration_ms) has the wrong type (bool)"),
            ("duration is NaN", row("a::t", "passed", float("nan"), "m"),
             "runs[1].results[0][2] (duration_ms) must be a finite number"),
            ("duration is Infinity", row("a::t", "passed", float("inf"), "m"),
             "runs[1].results[0][2] (duration_ms) must be a finite number"),
            ("duration is 1e300", row("a::t", "passed", 1e300, "m"),
             "runs[1].results[0][2] (duration_ms) is out of range"),
            ("duration is 2**63", row("a::t", "passed", 2 ** 63, "m"),
             "runs[1].results[0][2] (duration_ms) is out of range"),
            ("duration is a huge int", row("a::t", "passed", huge, "m"),
             "runs[1].results[0][2] (duration_ms) is out of range"),
            ("message is a list", row("a::t", "passed", 1, ["x"]),
             "runs[1].results[0][3] (message) has the wrong type (list)"),
            ("message is a number", row("a::t", "passed", 1, 5),
             "runs[1].results[0][3] (message) has the wrong type (int)"),
            ("test_id is a lone surrogate", row(surrogate, "passed", 1, "m"),
             "runs[1].results[0][0] (test_id) is not valid text"),
            ("outcome is an object", row("a::t", {"x": 1}, 1, "m"),
             "runs[1].results[0] must be [test_id, outcome, duration_ms, message]"),
            ("commit is a lone surrogate", run_with(commit="c" + chr(0xD800)),
             "runs[1].commit is not valid text"),
            ("created_at is NaN", run_with(created_at=float("nan")),
             "runs[1].created_at must be a finite number"),
            ("created_at is huge", run_with(created_at=1e300),
             "runs[1].created_at is out of range"),
            ("created_at is a string", run_with(created_at="yesterday"),
             "runs[1].created_at has the wrong type (str)"),
            ("created_at is a bool", run_with(created_at=True),
             "runs[1].created_at has the wrong type (bool)"),
            ("quarantine reason is an object",
             doc(quarantine={"test_id": "q::x", "reason": {"a": 1}}),
             "quarantine[1].reason has the wrong type (dict)"),
            ("quarantine reason is a number",
             doc(quarantine={"test_id": "q::x", "reason": 5}),
             "quarantine[1].reason has the wrong type (int)"),
            ("quarantine at is a string",
             doc(quarantine={"test_id": "q::x", "at": "yesterday"}),
             "quarantine[1].at has the wrong type (str)"),
            ("quarantine at is NaN",
             doc(quarantine={"test_id": "q::x", "at": float("nan")}),
             "quarantine[1].at must be a finite number"),
            ("quarantine at is a bool",
             doc(quarantine={"test_id": "q::x", "at": True}),
             "quarantine[1].at has the wrong type (bool)"),
            ("quarantine at is huge",
             doc(quarantine={"test_id": "q::x", "at": huge}),
             "quarantine[1].at is out of range"),
            ("quarantine test_id is a lone surrogate",
             doc(quarantine={"test_id": surrogate}),
             "quarantine[1].test_id is not valid text"),
            ("version is true", doc(flakebox_export=True), "not a flakebox export"),
            ("version is 1.0", doc(flakebox_export=1.0), "not a flakebox export"),
            ("version is a string", doc(flakebox_export="1"), "not a flakebox export"),
            ("version is 2", doc(flakebox_export=2), "not a flakebox export"),
        ]
        for name, document, expect in cases:
            p = write_raw(tmp, "bad.json", json.dumps(document))
            code, out, err = run_cli("--db", db, "import", p)
            self.assertEqual(code, 2, f"{name}: {out}{err}")
            self.assertIn("cannot import", err, name)
            self.assertIn(expect, err, name)
            self.assertNotIn("Traceback", err, name)
            # nothing was written - not even the valid run and quarantine
            # entry that come first in the file
            conn = store_mod.connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                             0, name)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0],
                0, name)
            conn.close()

    def test_nulls_and_numbers_an_exporter_can_write_still_import(self):
        tmp = mktemp()
        p = write_raw(tmp, "ok.json", json.dumps({
            "flakebox_export": 1,
            "runs": [{"commit": None, "branch": None, "created_at": 5.7,
                      "report_sha": None,
                      "results": [["a::t", "passed", None, None],
                                  ["a::u", "failed", 1.5, "boom"],
                                  ["a::v", "skipped", 0, ""]]}],
            "quarantine": [{"test_id": "q::x", "reason": None, "at": None},
                           {"test_id": "q::y"},
                           {"test_id": "q::z", "reason": "auto", "at": 12.5}]}))
        db = os.path.join(tmp, "h.db")
        code, out, err = run_cli("--db", db, "import", p)
        self.assertEqual(code, 0, err)
        self.assertIn("imported 1 run(s), skipped 0 already recorded, "
                      "3 new quarantine entries", out)
        conn = store_mod.connect(db)
        self.assertEqual(conn.execute("SELECT created_at FROM runs").fetchone()[0], 5)
        conn.close()


class TestImportInputs(unittest.TestCase):
    """`flakebox import` given a file it cannot use. JSON nested too deeply
    was a RecursionError traceback, and a directory, or a file that may not
    be read, an OSError traceback; both ended with exit status 1. Each is
    now `error: cannot import PATH: why`, one line, exit 2, and nothing is
    written."""

    DEPTH = 100000      # far deeper than any interpreter's limit

    def setUp(self):
        self.tmp = mktemp()
        self.db = os.path.join(self.tmp, "x.db")

    def stored(self) -> tuple:
        conn = store_mod.connect(self.db)
        try:
            return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                         for t in ("runs", "results", "quarantine"))
        finally:
            conn.close()

    def refused(self, path, *words) -> str:
        code, out, err = run_cli("--db", self.db, "import", path)
        self.assertEqual(code, 2, out + err)
        self.assertEqual(out, "")
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertTrue(err.startswith(f"error: cannot import {path}: "), err)
        for word in words:
            self.assertIn(word, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.stored(), (0, 0, 0))
        return err

    def nested(self) -> dict:
        deep = "[" * self.DEPTH + "]" * self.DEPTH
        return {
            "array.json": deep,
            "object.json": '{"a":' * self.DEPTH + "1" + "}" * self.DEPTH,
            "in_a_run.json": ('{"flakebox_export": 1, "runs": [{"commit": '
                              + deep + "}]}"),
        }

    def test_json_nested_too_deeply_is_a_clean_exit_2(self):
        for name, text in self.nested().items():
            with self.subTest(name):
                self.refused(write_raw(self.tmp, name, text),
                             "nested too deeply")

    def test_a_library_caller_gets_a_value_error_for_it(self):
        p = write_raw(self.tmp, "deep.json", self.nested()["array.json"])
        conn = store_mod.connect(self.db)
        try:
            with self.assertRaises(ValueError):
                store_mod.import_file(conn, p)
        finally:
            conn.close()

    def test_a_directory_is_a_clean_exit_2(self):
        d = os.path.join(self.tmp, "adir")
        os.mkdir(d)
        self.refused(d)

    def test_a_file_that_cannot_be_opened_is_a_clean_exit_2(self):
        p = write_raw(self.tmp, "export.json",
                      '{"flakebox_export": 1, "runs": []}')
        real_open = open

        def deny(file, *args, **kwargs):
            if file == p:
                raise PermissionError(errno.EACCES,
                                      os.strerror(errno.EACCES), file)
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", deny):
            self.refused(p, os.strerror(errno.EACCES))

    @unittest.skipIf(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
                     "read permission is not enforced on Windows (chmod only "
                     "sets the read-only flag) or against root")
    def test_a_file_without_read_permission_is_a_clean_exit_2(self):
        p = write_raw(self.tmp, "export.json",
                      '{"flakebox_export": 1, "runs": []}')
        os.chmod(p, 0)
        try:
            self.refused(p, os.strerror(errno.EACCES))
        finally:
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)

    def test_a_missing_file_still_says_so(self):
        missing = os.path.join(self.tmp, "missing.json")
        code, _, err = run_cli("--db", self.db, "import", missing)
        self.assertEqual(code, 2)
        self.assertEqual(err, f"error: no such file: {missing}\n")

    def test_the_command_line_does_the_same(self):
        env = child_env()
        env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
        deep = write_raw(self.tmp, "deep.json", self.nested()["array.json"])
        for path in (deep, self.tmp):
            with self.subTest(path):
                proc = subprocess.run(
                    [sys.executable, "-m", "flakebox", "--db", self.db,
                     "import", path],
                    capture_output=True, env=env, timeout=60)
                err = proc.stderr.decode("utf-8", "replace")
                self.assertEqual(proc.returncode, 2, err)
                self.assertEqual(len(err.splitlines()), 1, err)
                self.assertTrue(err.startswith("error: cannot import "), err)
                self.assertEqual(proc.stdout, b"")

    def test_the_readme_says_these_are_exit_2(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            readme = " ".join(f.read().split())
        for needle in ("A path that cannot be read (a directory, no "
                       "permission) and JSON nested too deeply to parse are "
                       "the same one-line error and exit 2",
                       "an `export --out` that cannot be written"):
            self.assertTrue(needle in readme, f"the README does not say {needle}")

    def test_a_good_file_after_a_bad_one_is_not_reached(self):
        # the first failure ends the run, as it always has
        good = write_raw(self.tmp, "good.json", json.dumps({
            "flakebox_export": 1,
            "runs": [{"commit": "c", "created_at": 5,
                      "results": [["a::t", "passed", 1, None]]}]}))
        code, out, err = run_cli("--db", self.db, "import", self.tmp, good)
        self.assertEqual(code, 2, out)
        self.assertEqual(self.stored(), (0, 0, 0))


class TestExportTarget(unittest.TestCase):
    """`flakebox export --out DIR` was an IsADirectoryError traceback. An
    --out that cannot be written is `error: cannot export ...`, exit 2."""

    def refused(self, out_path) -> str:
        tmp = mktemp()
        code, out, err = run_cli("--db", os.path.join(tmp, "x.db"),
                                 "export", "--out", out_path)
        self.assertEqual(code, 2, out + err)
        self.assertEqual(out, "")
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertTrue(err.startswith(f"error: cannot export to {out_path}: "),
                        err)
        self.assertNotIn("Traceback", err)
        return err

    def test_a_directory_is_a_clean_exit_2(self):
        self.refused(mktemp())

    def test_a_folder_that_does_not_exist_is_a_clean_exit_2(self):
        self.refused(os.path.join(mktemp(), "no", "such", "dir", "x.json"))

    def test_a_file_that_can_be_written_still_is(self):
        tmp = mktemp()
        target = os.path.join(tmp, "out.json")
        code, out, err = run_cli("--db", os.path.join(tmp, "x.db"),
                                 "export", "--out", target)
        self.assertEqual(code, 0, err)
        self.assertIn("exported 0 run(s)", out)
        with open(target, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["flakebox_export"], 1)


class TestLegacyRunFingerprint(unittest.TestCase):
    """R4-4: runs without a report_sha were re-added on every import."""

    def legacy_db(self, tmp: str) -> str:
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
            "INSERT INTO runs (commit_sha, branch, created_at) VALUES ('old2', 'main', 2);"
            "INSERT INTO results VALUES (1, 'a::t', 'failed', 1, '');"
            "INSERT INTO results VALUES (2, 'a::t', 'passed', 1, '');")
        legacy.commit()
        legacy.close()
        return db

    def test_import_twice_adds_nothing(self):
        tmp = mktemp()
        db = self.legacy_db(tmp)
        conn = store_mod.connect(db)  # migration stamps fingerprints
        shas = [r[0] for r in conn.execute("SELECT report_sha FROM runs ORDER BY id")]
        conn.close()
        self.assertTrue(all(s and s.startswith("legacy:") for s in shas), shas)
        self.assertNotEqual(shas[0], shas[1])
        exp = os.path.join(tmp, "e.json")
        self.assertEqual(run_cli("--db", db, "export", "--out", exp)[0], 0)
        local = os.path.join(tmp, "local.db")
        code, out, _ = run_cli("--db", local, "import", exp)
        self.assertIn("imported 2 run(s), skipped 0", out)
        code, out, _ = run_cli("--db", local, "import", exp)
        self.assertIn("imported 0 run(s), skipped 2 already recorded", out)
        # and back into the DB it came from
        code, out, _ = run_cli("--db", db, "import", exp)
        self.assertIn("imported 0 run(s), skipped 2 already recorded", out)
        conn = store_mod.connect(local)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)
        conn.close()

    def test_old_export_file_without_hashes_is_recognised(self):
        # an export written by flakebox 0.3 carries "report_sha": null
        tmp = mktemp()
        exp = write_raw(tmp, "old.json", json.dumps({
            "flakebox_export": 1, "quarantine": [],
            "runs": [{"commit": "c1", "branch": None, "created_at": 100,
                      "report_sha": None,
                      "results": [["a::t", "failed", 3, "boom"]]},
                     {"commit": "c1", "branch": None, "created_at": 101,
                      "report_sha": None,
                      "results": [["a::t", "passed", 2, None]]}]}))
        db = os.path.join(tmp, "h.db")
        code, out, _ = run_cli("--db", db, "import", exp)
        self.assertIn("imported 2 run(s), skipped 0", out)
        code, out, _ = run_cli("--db", db, "import", exp)
        self.assertIn("imported 0 run(s), skipped 2 already recorded", out)
        conn = store_mod.connect(db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)
        hist = store_mod.test_history(conn, "a::t")
        self.assertEqual([h["outcome"] for h in hist], ["passed", "failed"])
        conn.close()

    def test_api_recorded_runs_are_fingerprinted_and_real_hashes_untouched(self):
        tmp = mktemp()
        db = os.path.join(tmp, "h.db")
        conn = store_mod.connect(db)
        rid = store_mod.record_run(conn, [result("a::t", "passed")], commit_sha="c")
        sha = conn.execute("SELECT report_sha FROM runs WHERE id = ?", (rid,)).fetchone()[0]
        self.assertTrue(sha.startswith(store_mod.LEGACY_PREFIX))
        rid2 = store_mod.record_run(conn, [result("a::t", "passed")], commit_sha="c",
                                    report_sha="abc123")
        self.assertEqual(conn.execute("SELECT report_sha FROM runs WHERE id = ?",
                                      (rid2,)).fetchone()[0], "abc123")
        self.assertEqual(store_mod.find_run(conn, "c", "abc123"), rid2)
        self.assertEqual(store_mod.fingerprint_legacy_runs(conn), 0)
        conn.close()
        self.assertEqual(store_mod.legacy_fingerprint("c", 1, [("a", "passed", 1, "")]),
                         store_mod.legacy_fingerprint("c", 1, [["a", "passed", 1, ""]]))
        self.assertNotEqual(store_mod.legacy_fingerprint("c", 1, [("a", "passed", 1, "")]),
                            store_mod.legacy_fingerprint("c", 2, [("a", "passed", 1, "")]))


class TestSharedClassification(unittest.TestCase):
    """R4-5: `history` called a recovered test (18-pass streak) "flaky"
    while `report` left it out; both now go through classify()."""

    def recovered_db(self, tmp: str) -> str:
        return db_from_outcomes(tmp, {"a::t": ["failed", "failed"] + ["passed"] * 18})

    def test_history_and_report_agree(self):
        tmp = mktemp()
        db = self.recovered_db(tmp)
        code, out, _ = run_cli("--db", db, "report", "--format", "json")
        a = json.loads(out)
        self.assertEqual(a["flaky"], [])
        code, out, _ = run_cli("--db", db, "history", "--test", "a::t")
        self.assertEqual(code, 0)
        self.assertIn("a::t: recovered (18 consecutive passes)", out)
        self.assertNotIn("a::t: flaky", out)
        self.assertIn("flake rate 10%", out)
        # the same thresholds move both commands together
        code, out, _ = run_cli("--db", db, "report", "--format", "json",
                               "--stable-runs", "25")
        self.assertEqual([s["test_id"] for s in json.loads(out)["flaky"]], ["a::t"])
        code, out, _ = run_cli("--db", db, "history", "--test", "a::t",
                               "--stable-runs", "25")
        self.assertIn("a::t: flaky -", out)
        # a quarantined recovered test is "stable" in the report, and the
        # JSON carries the same status word
        conn = store_mod.connect(db)
        store_mod.quarantine_add(conn, "a::t")
        conn.close()
        code, out, _ = run_cli("--db", db, "report", "--format", "json")
        a = json.loads(out)
        self.assertEqual([s["test_id"] for s in a["stable"]], ["a::t"])
        self.assertEqual(a["stable"][0]["status"], detect_mod.RECOVERED)

    def test_history_notes_too_few_runs(self):
        tmp = mktemp()
        db = db_from_outcomes(tmp, {"a::t": ["failed", "passed"]})
        code, out, _ = run_cli("--db", db, "history", "--test", "a::t")
        self.assertIn("a::t: flaky (only 2 executed runs; report needs 5)", out)
        code, out, _ = run_cli("--db", db, "history", "--test", "a::t",
                               "--min-runs", "2")
        self.assertIn("a::t: flaky -", out)

    def test_classify_is_the_single_source(self):
        c = detect_mod.classify
        self.assertEqual(c(3, 2), detect_mod.FLAKY)
        self.assertEqual(c(3, 2, pass_streak=3, stable_runs=3), detect_mod.RECOVERED)
        self.assertEqual(c(3, 2, pass_streak=2, stable_runs=3), detect_mod.FLAKY)
        self.assertEqual(c(3, 2, pass_streak=99, stable_runs=0), detect_mod.FLAKY)
        self.assertEqual(c(0, 2, pass_streak=0, stable_runs=3), detect_mod.FAILING)
        self.assertEqual(c(5, 0, pass_streak=5, stable_runs=3), detect_mod.PASSING)
        s = detect_mod.stats(["passed"] * 10 + ["failed"], stable_runs=10)
        self.assertEqual(s["status"], detect_mod.RECOVERED)
        self.assertEqual(detect_mod.stats(["passed"] * 10 + ["failed"])["status"],
                         detect_mod.FLAKY)
        self.assertFalse(hasattr(store_mod, "last_seen"))


class TestBrokenPipe(unittest.TestCase):
    """R4-6: `flakebox report | head` must not end in a traceback."""

    def test_broken_pipe_in_main_is_a_quiet_exit_0(self):
        tmp = mktemp()
        db = make_db(tmp)

        class Broken(io.TextIOBase):
            def write(self, s):
                raise BrokenPipeError(32, "Broken pipe")

        err = io.StringIO()
        with redirect_stdout(Broken()), redirect_stderr(err):
            code = cli_mod.main(["--db", db, "report"])
        self.assertEqual(code, 0)
        self.assertNotIn("Traceback", err.getvalue())

    def test_reader_going_away_end_to_end(self):
        tmp = mktemp()
        db = make_db(tmp)
        env = child_env()
        env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [sys.executable, "-m", "flakebox", "--db", db, "report"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        proc.stdout.close()  # the reader (`head`) is gone before we write
        try:
            _, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0, err)
        self.assertNotIn(b"Traceback", err)
        self.assertNotIn(b"Exception ignored", err)


if __name__ == "__main__":
    unittest.main()
