"""Release and CI workflow checks (0.4.6): the release workflow refuses a
tag whose commit is not on origin/main, and its scripts are EXECUTED here
with a faked tag and a throwaway origin; CI installs pytest, smoke-tests the
console script on the OS matrix and runs the sdist's own tests."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WF = os.path.join(REPO, ".github", "workflows")
sys.path.insert(0, HERE)

from workflow_helper import job_steps  # noqa: E402

HAVE_WF = os.path.isdir(WF)
VERSION_RE = r'^version = "([^"]+)"'


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


def _git(cwd, *args, check=True):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    p = subprocess.run(["git", *args], cwd=cwd, env=env, text=True,
                       capture_output=True)
    if check and p.returncode:
        raise AssertionError(f"git {args}: {p.stderr}")
    return p.stdout.strip()


@unittest.skipUnless(HAVE_WF, "workflows are not shipped in the sdist")
class TestReleaseWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _read(".github", "workflows", "release.yml")
        cls.steps = job_steps(cls.text, "build")

    def step(self, prefix):
        for s in self.steps:
            if (s["name"] or "").startswith(prefix):
                return s
        self.fail(f"no step named {prefix!r}")

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shim = os.path.join(self.tmp, "bin")
        os.makedirs(shim)
        os.symlink(sys.executable, os.path.join(shim, "python"))
        self.shim = shim
        # origin with main (c1 <- c2) and a side branch (c1 <- c3)
        self.origin = os.path.join(self.tmp, "origin.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", self.origin)
        seed = os.path.join(self.tmp, "seed")
        os.makedirs(seed)
        _git(seed, "init", "-q", "-b", "main")
        with open(os.path.join(seed, "pyproject.toml"), "w") as f:
            f.write('[project]\nname = "x"\nversion = "1.2.3"\n')
        _git(seed, "add", "."), _git(seed, "commit", "-qm", "c1")
        self.c1 = _git(seed, "rev-parse", "HEAD")
        with open(os.path.join(seed, "a"), "w") as f:
            f.write("a")
        _git(seed, "add", "."), _git(seed, "commit", "-qm", "c2")
        self.c2 = _git(seed, "rev-parse", "HEAD")
        _git(seed, "checkout", "-q", "-b", "side", self.c1)
        with open(os.path.join(seed, "b"), "w") as f:
            f.write("b")
        _git(seed, "add", "."), _git(seed, "commit", "-qm", "c3")
        self.c3 = _git(seed, "rev-parse", "HEAD")
        _git(seed, "remote", "add", "origin", self.origin)
        _git(seed, "push", "-q", "origin", "main", "side")
        # what actions/checkout with fetch-depth: 0 leaves behind
        self.ci = os.path.join(self.tmp, "ci")
        _git(self.tmp, "clone", "-q", self.origin, self.ci)

    def run_step(self, step, cwd, **ctx):
        """Run a step's script under bash the way the runner does, with
        its env: expressions filled from `ctx` (sha, tag) and the faked
        GITHUB_REF / GITHUB_SHA the runner would also set."""
        sub = {"github.sha": ctx.get("sha", ""),
               "github.ref_name": ctx.get("tag", "")}
        env = {"PATH": self.shim + os.pathsep + os.environ["PATH"],
               "HOME": self.tmp, "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_TERMINAL_PROMPT": "0",
               "GITHUB_REF": "refs/tags/" + ctx.get("tag", ""),
               "GITHUB_SHA": ctx.get("sha", "")}
        for k, v in step["env"].items():
            for expr, val in sub.items():
                v = v.replace("${{ " + expr + " }}", val)
            env[k] = v
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
             step["run"]], cwd=cwd, env=env, text=True, capture_output=True,
            timeout=120)

    # --- the new ancestry check --------------------------------------
    def test_checkout_fetches_the_whole_history(self):
        checkout = self.steps[0]
        self.assertTrue((checkout["uses"] or "").startswith("actions/checkout"))
        self.assertIn("fetch-depth: 0", checkout["text"])

    def test_the_main_check_runs_before_anything_is_built(self):
        names = [s["name"] or s["uses"] or "" for s in self.steps]
        ancestry = next(i for i, n in enumerate(names)
                        if "must be on main" in n)
        build = next(i for i, s in enumerate(self.steps)
                     if s["run"] and "python -m build" in s["run"])
        self.assertLess(ancestry, build)

    def test_the_tag_value_never_sits_in_the_script_text(self):
        for s in self.steps:
            if s["run"]:
                self.assertNotIn("${{", s["run"], s["name"])

    def test_a_commit_on_main_passes(self):
        p = self.run_step(self.step("The tagged commit must be on main"),
                          self.ci, sha=self.c2, tag="v1.2.3")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_an_older_commit_on_main_passes(self):
        p = self.run_step(self.step("The tagged commit must be on main"),
                          self.ci, sha=self.c1, tag="v1.2.3")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_a_commit_only_on_a_side_branch_is_refused(self):
        _git(self.ci, "fetch", "-q", "origin", "side")
        p = self.run_step(self.step("The tagged commit must be on main"),
                          self.ci, sha=self.c3, tag="v1.2.3")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error::", p.stdout)
        self.assertIn("not on origin/main", p.stdout)

    def test_the_check_fetches_main_itself(self):
        # a clone that only has the tag's branch still learns origin/main
        lone = os.path.join(self.tmp, "lone")
        _git(self.tmp, "clone", "-q", "--single-branch", "-b", "side",
             self.origin, lone)
        p = self.run_step(self.step("The tagged commit must be on main"),
                          lone, sha=self.c3, tag="v1.2.3")
        self.assertEqual(p.returncode, 1)
        self.assertIn("not on origin/main", p.stdout)
        # ... and a main commit that this clone never fetched is found
        p = self.run_step(self.step("The tagged commit must be on main"),
                          lone, sha=self.c2, tag="v1.2.3")
        self.assertEqual(p.returncode, 0, p.stderr)

    # --- the existing tag == version check, run for real ----------------
    @unittest.skipIf(sys.version_info < (3, 11), "the script uses tomllib")
    def test_a_matching_tag_passes(self):
        p = self.run_step(self.step("Tag must equal"), self.ci, tag="v1.2.3")
        self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipIf(sys.version_info < (3, 11), "the script uses tomllib")
    def test_a_wrong_tag_fails(self):
        p = self.run_step(self.step("Tag must equal"), self.ci, tag="v9.9.9")
        self.assertEqual(p.returncode, 1)
        self.assertIn("1.2.3", p.stdout + p.stderr)

    @unittest.skipIf(sys.version_info < (3, 11), "the script uses tomllib")
    def test_a_hostile_tag_name_is_data_not_code(self):
        marker = os.path.join(self.ci, "pwned")
        p = self.run_step(self.step("Tag must equal"), self.ci,
                          tag='v1";touch pwned;echo "')
        self.assertEqual(p.returncode, 1)
        self.assertFalse(os.path.exists(marker))

    def test_the_real_tag_step_matches_this_checkouts_version(self):
        import re
        version = re.search(VERSION_RE, _read("pyproject.toml"), re.M).group(1)
        if sys.version_info < (3, 11):
            self.skipTest("the script uses tomllib")
        p = self.run_step(self.step("Tag must equal"), REPO, tag="v" + version)
        self.assertEqual(p.returncode, 0, p.stderr)


@unittest.skipUnless(HAVE_WF, "workflows are not shipped in the sdist")
class TestCiWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _read(".github", "workflows", "ci.yml")

    def test_the_test_job_installs_pytest_before_running_the_tests(self):
        steps = job_steps(self.text, "test")
        runs = [s["run"] or "" for s in steps]
        pytest_at = next(i for i, r in enumerate(runs) if "pip install pytest" in r)
        tests_at = next(i for i, r in enumerate(runs) if "unittest discover" in r)
        self.assertLess(pytest_at, tests_at)

    def test_the_console_script_is_smoke_tested_on_every_os(self):
        self.assertIn("os: [ubuntu-latest, windows-latest, macos-latest]",
                      self.text)
        steps = job_steps(self.text, "test")
        smoke = [s for s in steps if s["run"] and "flakebox --version" in s["run"]]
        self.assertTrue(smoke)
        self.assertIn("pip install .", smoke[0]["run"])

    def test_the_package_job_runs_the_sdists_own_tests(self):
        steps = job_steps(self.text, "package")
        sd = [s for s in steps if (s["name"] or "").startswith("Run the tests from the sdist")]
        self.assertEqual(len(sd), 1)
        script = sd[0]["run"]
        for needle in ("tar -xzf dist/*.tar.gz", "unittest discover -s tests",
                       "set -e"):
            self.assertIn(needle, script)
        build = next(i for i, s in enumerate(steps)
                     if s["run"] and "python -m build" in s["run"])
        self.assertLess(build, steps.index(sd[0]))


if __name__ == "__main__":
    unittest.main()
