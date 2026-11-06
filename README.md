# Flakebox (`flakebox`)

**Find, prove and quarantine flaky tests from the JUnit XML you already
have — one SQLite file, zero dependencies, no account.**

Flaky tests waste CI time and erode trust in every red build. Hosted
services (Trunk Flaky Tests, Datadog Test Optimization, Buildkite Test
Engine) solve this as a service; flakebox is the free, self-hosted
alternative: ingest JUnit XML, detect flakiness statistically, **prove** it
when the same commit both passed and failed, quarantine the flakes, and get
told when they're stable enough to come back.

## Quick start (60 seconds, synthetic example)

> The proof, failure-mode and safe-output features below are 0.5.0, which
> is unreleased: install from this repository
> (`pip install git+https://github.com/calasdan-sketch/flakebox`) until it
> is on PyPI. See `CHANGELOG.md`.

```bash
pip install flakebox
# record two CI runs of the same commit (here: a re-run that went green)
flakebox record --junit run1.xml --commit=abc123
flakebox record --junit run2.xml --commit=abc123
flakebox report
```

Output on that synthetic pair of reports (one test failed in run 1 only,
one Maven Surefire test needed its built-in retry in both runs):

```
## Flaky (pass AND fail - quarantine candidates)
| test | flake rate | fails/passes | runs | proof | top failure |
|---|---|---|---|---|---|
| `tests.test_api::test_login` | 50% | 1/1 | 2 | same commit | ConnectionError: port N refused after Ns |
| `com.acme.CartTest::checkout` | 0% | 0/2 | 2 | passed on retry x2 | expected: 200 but was: 503 |
```

(`checkout` never ended red - its rate is 0% - but the runner had to
retry it in both runs.) Two runs were enough: the same code gave both
answers, so no amount of
extra history could make these tests less flaky (see
[Proof of flakiness](#proof-of-flakiness)). Without proof, a test needs
`--min-runs` (5) executed runs before it is called flaky.

## How it compares

Only what each project documents; follow the links. "—" means the linked
page does not describe it, not that it is impossible.

| | flakebox | [Trunk Flaky Tests](https://docs.trunk.io/flaky-tests/overview) | [Datadog Flaky Test Mgmt](https://docs.datadoghq.com/tests/flaky_management/) | [Buildkite Test Engine](https://trunk.io/compare/trunk-vs-buildkite) | [flakewall](https://github.com/cicatriiz/flakewall) | [pytest-rerunfailures](https://pypi.org/project/pytest-rerunfailures/) |
|---|---|---|---|---|---|---|
| Self-hosted, no account | yes (SQLite file) | no (SaaS) | no (SaaS) | no (SaaS) | yes (YAML file) | yes (plugin) |
| Input | JUnit XML, any runner | "language-, environment-, and framework-agnostic" | — | collectors; JUnit XML | JUnit XML | pytest only |
| Long-run statistical detection | yes (window of 50 runs) | yes (threshold monitor) | yes (failure-rate policies) | yes (flakiness score) | pass/fail flips across given reports | no (retries only) |
| Same-commit / retry proof | yes, both | pass-on-retry | — | pass-on-retry | — | — |
| Separates broken from flaky | yes | yes | yes | — | — | n/a |
| Quarantine that keeps running tests | yes (`--mode xfail`) | yes | yes | Buildkite Pipelines only | yes (guard list) | n/a |
| Leaves quarantine when fixed | tells you (stable streak) | configurable recovery period | yes ("Fixed" state) | — | TTL expiry | n/a |
| Failure grouping | normalized messages | AI clustering by stack trace | AI root-cause categories | — | — | — |
| Ticketing, PR comments, dashboards | no | yes | yes | yes | Slack, annotations | no |

The hosted tools do much more around the workflow (PR comments, tickets,
ownership, dashboards); flakebox is for teams that want the detection and
the quarantine without sending test data anywhere.

## Install

## Install

Zero dependencies — Python 3.10+ stdlib only.

```bash
pip install flakebox
flakebox --version
```

Source and issues: https://github.com/calasdan-sketch/flakebox

## Usage

```bash
# in CI, after your test step (pytest --junitxml=reports/junit.xml):
flakebox record --junit reports/junit.xml --commit="$GITHUB_SHA"

# what is flaky?
flakebox report
flakebox report --format json

# quarantine the flaky set for pytest:
flakebox quarantine --format pytest -o .
python -m pytest -p flakebox_quarantine   # flakes are skipped, build stays green

# ...or keep running the flakes without letting them fail the build, so
# they keep recording results and can earn their way back out:
flakebox quarantine --format pytest --mode xfail -o .

# the full quarantine set as shell-quoted pytest --deselect args (best
# effort, see below; eval parses the quoting) or as JSON for other runners
eval "pytest $(flakebox quarantine --format deselect)"
flakebox quarantine --format json

# when a quarantined test stabilizes, the report tells you:
flakebox unquarantine --test "pkg.Class::test_x"
flakebox history --test "pkg.Class::test_x"   # per-test run history, same
                                              # thresholds/status words as report

# carry the history around (CI artifact -> laptop, job -> aggregator):
flakebox export --out flakebox-export.json
flakebox import flakebox-export.json           # merges; re-import adds nothing
```

`--db PATH` picks the database and goes before **or** after the
subcommand: `flakebox --db ci.db report` and `flakebox report --db ci.db`
are the same command. Default `~/.flakebox/history.db`
(`%USERPROFILE%\.flakebox\history.db` on Windows). A `--db` that is not a
SQLite database is a clean `error: cannot use database ...`, exit 2.
So are the other arguments flakebox cannot use: each ends in an `error:`
line and exit 2, never a traceback. That covers an `-o` that is a file or
cannot be created, an integer option that reaches the database
(`--retire-after`, `history --limit`) outside the 64-bit range SQLite
stores (-9223372036854775808 to 9223372036854775807), and a `--commit`,
`--branch` or `--test` that is not valid UTF-8 text (a stray byte in the
command line).

`report`, `quarantine`, `history`, `export` and `unquarantine` need a
database that exists: a mistyped `--db` is `error: no flakebox database
at ...`, exit 2, and no file is created (before 0.5.0 it created an empty
one and reported "No flaky tests detected"). `record` and `import` create
it. A database with no runs in it says `No runs recorded yet`.
`unquarantine` of a test that is not quarantined says so on stderr.
`unquarantine --all-stable` / `--all-retired` release every quarantined
test the report would list as stable / retired (they take the same
threshold options as `report`). A read command also refuses a `--db` that
exists but is not flakebox's: a zero-byte file or another program's
SQLite database is `error: ... is not a flakebox database`, exit 2, and is
left untouched (it is opened read-only to check).

`flakebox record` hashes the report: recording the same file for the
same commit twice says `already recorded as run N` and adds nothing
(`--force` records it anyway), so a retried CI step cannot double-count.
Nested `<testsuite>` elements (surefire aggregates) are read, `time="0,5"`
is 500 ms, and a `time` that is not a finite, non-negative number
(`fast`, `inf`, `nan`, `-5`, `1e300`) is recorded as 0 with a warning
instead of rejecting the report. A report that declares a `DOCTYPE` or an
`ENTITY` is refused (exit 2) as a possible XML bomb, before anything is
expanded; text that merely mentions `<!DOCTYPE html>`, such as captured
output in a CDATA section, is fine.

A report with no test cases at all (`<testsuite tests="0"/>`, or a file
written before the tests ran) is not recorded. It is nearly always a test
step that broke, and a run without tests makes every test look absent:
three of them in a row retire the whole suite, and `report` says "No flaky
tests detected". `record` prints a warning on stderr and exits 0, so a
build that really ran no tests (docs only) is not failed. `--allow-empty`
records the report anyway; it then counts like any other run. Empty runs
that an earlier flakebox recorded stay in the history (there is no
command to delete a run) and stop mattering once `--retire-after` (3) real
runs have followed, or at once with `--retire-after 0`.

`flakebox quarantine` is idempotent and safe to run on every build: it
adds the current flaky set (never re-stamping tests already in
quarantine) and always emits the **whole** quarantine set, not just the
newly added tests. The one-line summary (`quarantined 1 new test(s); 3
in quarantine.`) goes to stderr for `--format deselect`, so stdout is
only ever the `--deselect` arguments.

Run the plugin with `python -m pytest -p flakebox_quarantine`, not as the
bare `pytest` command: `-p` imports `flakebox_quarantine` at start-up, and
only `python -m pytest` puts the current directory (where `-o .` wrote the
plugin) on `sys.path`. The bare command fails with an `ImportError` (`No
module named 'flakebox_quarantine'`). If `-o` wrote the plugin to another
directory, put that directory on `PYTHONPATH` first
(`PYTHONPATH=ci python -m pytest -p flakebox_quarantine`; `set
PYTHONPATH=ci` in `cmd.exe`, `$env:PYTHONPATH = "ci"` in PowerShell); with
it set, the bare `pytest` command works too.

`--format deselect` is **best effort**: JUnit ids are not pytest node
ids, so flakebox maps the common pytest shapes
(`pkg.module.Class::test_x` → `pkg/module.py::Class::test_x`,
`pkg.module::test_x` → `pkg/module.py::test_x`) and pytest ignores a
`--deselect` that matches nothing. A lowercase class name, a
`python_files` pattern other than `*.py`, or a non-pytest producer breaks
the guess, and pytest's `--deselect` is a *prefix* match (`--deselect
t.py::test_x` also drops `test_xy` and `test_x[1]`); the generated plugin
(`--format pytest`) matches whole names and path components instead (see
below) and is the reliable path. The line quotes each argument for
the shell (POSIX quoting; `cmd.exe` quoting on Windows), so the shell
has to parse it once more: `eval "pytest $(flakebox quarantine --format
deselect)"` hands pytest every id as one argument, even
`test_param[a b]` or one containing `$(...)`. A bare `pytest
$(flakebox quarantine --format deselect)` does not: command
substitution splits the output at whitespace and never removes the
quotes in it, so an id with a space or a shell character arrives in
pieces with the quotes still attached (ids without either are fine).
PowerShell has no `$(...)` word splitting, so `Invoke-Expression "pytest
$(flakebox quarantine --format deselect)"` is the counterpart of `eval`,
but it is **untested**: the Windows line is quoted for `cmd.exe`, which
leaves `;`, `&`, `,`, `$`, backticks and parentheses bare, and PowerShell
may parse those itself (an id that contains one may be split, or even
run). If an id can contain one, use the plugin.

On Windows the `--format deselect` line is written with a plain LF ending,
not CR LF: text-mode output would end the last id with a carriage return,
which Git-Bash's `$(...)` keeps (the id then matches nothing). That is
simulated in the tests with a text stream that writes CR LF; it has not
been run on Windows.

The plugin matches a test to a quarantined id by name and by whole path
components, in either spelling (`tests/test_mod.py::TestA::test_x` and
`tests.test_mod.TestA::test_x` are the same test): the names, with any
`[parameters]`, must be equal, and so must the module path and class
names, except that one side may lack leading directories, because CI and
your machine can have different rootdirs. So `test_mod.py::test_x` is the
quarantined `tests.test_mod::test_x`, but `pkg_a/test_util.py::test_x` is
never `pkg_b/test_util.py::test_x`. When the *recorded* id is the shorter
one, it cannot say which directory it came from and every test that ends
the same way matches: record and test from the same rootdir. Parametrised
ids are matched whole: quarantining `test_version[1.5]` skips exactly that
case, never `test_unrelated[0.5]` or `test_version[2.0]`.

Example report:

```
## Flaky (pass AND fail — quarantine candidates)
| test | flake rate | fails/passes | runs |
|---|---|---|---|
| `tests.test_a.TestA::test_flaky` | 50% | 3/3 | 6 |

## Consistently failing (broken, not flaky — fix, don't quarantine)
- `tests.test_b::test_broken` (6 failures, 0 passes)

## Stable — ready to un-quarantine
- `tests.test_c::test_old_flake` (12 consecutive passes)

## Retired (absent from the last 3 runs — deleted or renamed, not counted)
- `tests.test_d::test_removed` (was failing; last seen run 41, commit 9f3c2a1)
```

(The flaky table also has the `proof` and `top failure` columns shown in
the quick start; trimmed here.)

## How detection works

No ML, fully explainable. Only *executed* runs count — a skipped run is
not evidence either way, so it never pushes a test over `--min-runs` and
never breaks a pass streak. "Newest" always means the newest recorded
time of a run (its row id only breaks a tie), so merging an older export
into a DB never makes old runs look recent.

- **flaky**: at least one pass *and* at least one failure (`failed` or
  `error`) across ≥5 executed runs (`--min-runs`), and not currently on
  a pass streak of ≥10 (`--stable-runs`).
- **recovered**: would be flaky, but the newest ≥10 executed runs all
  passed — it has recovered and is left alone, so quarantining it,
  releasing it and re-quarantining it from the same stale failures can't
  loop. `report` leaves it out of the flaky table and `flakebox history`
  says `recovered (18 consecutive passes)`: one `classify()` decides the
  word for every command, and `history` takes the same `--min-runs`,
  `--stable-runs` and `--retire-after` flags as `report`.
- **failing** (the "Consistently failing" section): ≥5 executed runs and
  never a pass. Broken, not flaky — different problem, don't quarantine
  it. `flakebox history` labels it `failing` and shows no flake rate.
- **stable**: a quarantined test whose newest ≥10 executed runs all
  passed — the report tells you to un-quarantine it.
- **retired**: a test with history that appears in none of the newest 3
  recorded runs (`--retire-after`, `0` disables) — deleted or renamed.
  It is listed on its own with where it was last seen and counted
  nowhere else, so a test you removed does not stay "consistently
  failing" for the rest of its 50-run window. One or two partial runs
  (someone recorded a single file) do not retire anything. A retired
  test still in quarantine is flagged with the `unquarantine` command
  to run.
- Everything else (always passing, or too few executed runs) is not
  reported.

The flake rate is the share of executed runs that ended red (`failed` or
`error`); a run that passed only on a retry ended green and is not
counted in it, it is counted in the `proof` column instead. The `runs`
column is executed runs. History lives in SQLite (`--db` to override).

### Proof of flakiness

Google defines a flaky result as "a test that exhibits both a passing and
a failing result with the same code"
([Google Testing Blog, 2016](https://testing.googleblog.com/search/label/John%20Micco)).
flakebox records which commit every run came from, so it can check exactly
that, and it reads the retry evidence some runners write:

- **same commit**: the test passed in one run and failed in a
  *different* run of the same `--commit` **and the same `--env`**. A CI
  "re-run" of a red build gives this for free. Two results inside one run
  (a duplicate test id, e.g. jest-junit names without the file path) never
  count, and neither do runs from different environments: a test that
  passes on Linux and fails on Windows at one commit has a platform bug,
  not a flake. Set `flakebox record --env NAME` on every matrix leg (the
  Action does it for you: its `env` input defaults to
  `<db-key>-<runner.os>`; set it per combination, such as
  `${{ matrix.os }}-py${{ matrix.python }}`, when one OS runs several
  Python versions). Runs recorded without `--env` share one environment.
- **passed on retry**: Maven Surefire / Failsafe with
  `rerunFailingTestsCount` write a test that failed and then passed as a
  `<testcase>` with `<flakyFailure>` / `<flakyError>` children
  ([Surefire docs](https://maven.apache.org/surefire/maven-surefire-plugin/examples/rerun-failing-tests.html)).
  flakebox records it as `passed_on_rerun`: one executed run that ended
  green, so it counts as a pass and not in the flake rate, but it makes the
  test flaky, is one `passed on retry` proof (never also a same-commit
  one), and breaks a pass streak. A test that failed
  every retry (`<failure>` plus `<rerunFailure>`) is a plain failure.

A flaky test with proof is reported (and `quarantine`d) with fewer than
`--min-runs` executed runs, and ranks first; the `proof` column and
`flakebox history` say which proof. A *recovered* test (newest
`--stable-runs` runs all passed) stays recovered either way. JSON output
carries `same_commit_flips`, `same_run_flips` and `proven` per test.

`--ignore-commits` (on `report`, `quarantine` and `history`) turns the
same-commit proof off. Use it when the DB holds local runs from an
uncommitted worktree, or runs that all used one placeholder `--commit`:
then "the same commit" was not the same code. Runs recorded without
`--commit` never count as the same commit.

### Failure modes

Each failing run's message (the `message` attribute; else the first line
of the element's text; else its `type`, which is what Surefire leaves for
an exception without a message) is folded into a signature: only values
that change from run to run become `N` - numbers with decimals or a time
or `%` unit (`30.2s`, `3000 ms`), numbers after a `:` (ports, line
numbers), numbers of four or more digits, `0x` addresses and UUIDs -
whitespace is collapsed, first line only. So `timeout after 30.2s` and
`timeout after 31.7s` are one mode, while `assert 404 == 200` and `assert
500 == 200`, `HTTP 503` and `HTTP 401`, `user_1` and `user_2` stay
apart. The report shows each flaky or failing test's most common mode,
`history` lists the top three with counts (and each failed run's
message), and the JSON has `failure_modes: [{"message", "count",
"example"}]`, where `example` is the newest raw message of that mode. It is string folding,
not clustering: two different stack traces with the same first line are
one mode.

### Untrusted text is printed safely

Test ids, failure messages, commits and branches come from whoever wrote
the test or pushed the branch. Every text output escapes control
characters, bidi overrides and zero-width characters (`\x1b`, `\u202e`,
`\u200b`), so a test id cannot clear your terminal or disguise itself as
another id. In the markdown report a `|` in an id is escaped (it would
otherwise split the table cell on GitHub) and backticks get a longer code
fence. The suggested `flakebox unquarantine --test ...` commands are
quoted so that pasting one runs flakebox and nothing else. On POSIX
shells that is single-quoting (`shlex.quote`), which is safe for every
id. On Windows no quoting is safe in both `cmd.exe` and PowerShell for
every character, so an id is only put in the command, in double quotes,
when it consists of letters, digits, spaces and `_ . : / [ ] - , = + @ #
~`. For any other id (one with `&`, `%`, `!`, `$`, a backtick, `"`, `\`),
and on every platform for an id with `|`, a backtick or a control
character, the report suggests a command that needs no id instead:
`flakebox unquarantine --all-stable --stable-runs N` or `--all-retired
--retire-after N`, which release every test the report lists in that
section. JSON output escapes all of this already. The
`--format deselect` line is the exception by design: it must be the exact
ids, quoted for the shell.

### Quarantine modes

The generated pytest plugin reads `flakebox_quarantine.json` (the sorted
ids, the mode and a count; no timestamp, so regenerating an unchanged set
rewrites identical files and `git` shows no diff):

- `--mode skip` (default): quarantined tests are skipped. They record no
  result, so they cannot become *stable* on their own — run them
  somewhere non-blocking (a separate job) and `flakebox record` that, or
  use xfail mode.
- `--mode xfail`: quarantined tests still run as non-strict `xfail`,
  so they can never fail the build, but their real pass/fail lands in
  the JUnit report (`flakebox record` maps the plugin's xfail back to a
  failure) and the report can tell you when they have stabilised.

### Export / import

`flakebox export --out FILE` writes the whole DB (runs with results, and
the quarantine set) as one JSON document. `flakebox import FILE...`
merges exports into a DB: a run already present (same commit and same
report hash) is skipped, so importing the same file twice, or into the
DB it came from, adds nothing; quarantine entries merge as a set. Runs
recorded without a report hash (flakebox < 0.3, or through the Python
API) carry a content **fingerprint** (`legacy:` + a digest of commit,
timestamp and results) instead, stamped when the DB is opened, so they
are recognised too. Imported runs keep their original timestamp, and
the newest-runs windows (the pass streak, `--retire-after`, `history`)
follow timestamps, not the order rows were added; a run exported
without a timestamp is imported as the oldest. A malformed export is
refused as a whole before anything is written: every field is checked
first (types, finite numbers that fit a 64-bit integer, text UTF-8 can
encode, `"flakebox_export": 1` exactly) and the error names the first bad
one (`error: cannot import ...: runs[0] must be an object`, exit 2).
A path that cannot be read (a directory, no permission) and JSON nested
too deeply to parse are the same one-line error and exit 2, and so is an
`export --out` that cannot be written.
This is how CI's history reaches a laptop, and how per-job DBs of a
matrix build become one (see the Action).

## GitHub Action

`action.yml` is a composite action: restores the DB from cache, records
the JUnit report, posts the flakebox report to the job summary, and —
optionally — quarantines the flaky set and exports the history as
artifact-ready files.

```yaml
- uses: calasdan-sketch/flakebox@main
  with:
    junit: reports/junit.xml
    min-runs: "5"          # -> flakebox report --min-runs 5
    fail-on-flaky: "true"
    quarantine-output: flakebox/quarantine.json
    export-output: flakebox/history.json
- uses: actions/upload-artifact@v4
  with:
    name: flakebox
    path: flakebox/
```

`@main` is this repository's default branch. A `@v1` reference works only once a
release is tagged `v1`; until then use the branch, or pin a commit SHA.

| input               | default    | what it does                                  |
|---------------------|------------|-----------------------------------------------|
| `junit`             | (required) | path to the JUnit XML → `flakebox record --junit` (a report with no test cases is warned about, not recorded) |
| `db-key`            | `flakebox` | cache key prefix for the history DB           |
| `min-runs`          | `5`        | executed runs before "flaky" → `flakebox report --min-runs` |
| `fail-on-flaky`     | `false`    | fail the step when flaky tests are found that are not quarantined |
| `quarantine-output` | (off)      | run `flakebox quarantine --format json` after the report and write the whole set here |
| `export-output`     | (off)      | `flakebox export --out` the history DB here   |

**The acknowledgement loop.** The history DB lives in the CI cache, where
nobody can run `flakebox quarantine` by hand — so on its own,
`fail-on-flaky: true` fails *every* build from the first flake. With
`quarantine-output` set, the action quarantines the flaky set in the
history DB right after the report (before the fail step): a new flake
fails exactly one build, is acknowledged from the next run on, and the
JSON file names it. Then, on a laptop:

```bash
gh run download --name flakebox              # quarantine.json + history.json
flakebox import flakebox/history.json         # CI's history, locally
flakebox quarantine --format pytest -o .      # flakebox_quarantine.{py,json}
git add flakebox_quarantine.py flakebox_quarantine.json && git commit
```

and run the suite with `python -m pytest -p flakebox_quarantine` (skip
mode) or `--mode xfail` so the quarantined tests keep reporting and can
earn their way back out. When the report says a test is stable,
`flakebox unquarantine --test ...` locally, regenerate the plugin, commit.

Input names use dashes everywhere (`min-runs`, never `min_runs`); the test
suite reads `action.yml` and checks that every referenced input is
declared, every declared input is used, and every CLI flag it passes
exists. Matrix builds: see the cache-race note at the top of `action.yml`
(per-job DBs, then an aggregator job that `flakebox import`s every job's
export).

**No expression ever reaches a shell.** GitHub pastes the value of a
`${{ ... }}` expression into a `run:` script *before* bash parses it, so a
branch named `x";touch${IFS}pwned;echo"y` (a legal git ref) would run a
command in your job. The action passes every input and `github.*` value
through the step's `env:` and reads it as a quoted variable
(`"$FB_BRANCH"`), so the shell only ever sees a string. Each option takes
its value as one word, `--branch="$FB_BRANCH"`, so a value that starts
with `-` (a ref or a path can) is still that option's value and never the
start of another option. The test suite scans `action.yml` for `${{`
inside any `run:` and for an option and its value written as two words,
and runs each script under bash with hostile values and with values that
start with `-`. The two `actions/cache` steps are first-party
and use the `v4` tag, not a commit SHA; pin them in a fork if your policy
requires it.

## Limitations

- **Recency is wall-clock time.** "Newest" is when a run was recorded
  (an imported run keeps the time it was exported with); runs recorded in
  the same second tie and fall back to the order they were added, and
  machines whose clocks disagree order their merged runs by those clocks.
- **`--format deselect` is a guess** at pytest node ids (see above); the
  generated plugin is the reliable path, `eval` is needed for ids with a
  space or a shell character, and the PowerShell form is untested.
- **Any DOCTYPE is refused**, harmless or not: JUnit reports never need
  one, and refusing them all keeps the XML-bomb check simple.
- **The Action's cache steps use the `v4` tag**, not a commit SHA (above).
- **Same-commit proof trusts `--commit` and `--env`.** A placeholder
  commit reused for different code, matrix legs recorded without distinct
  `--env` values and then merged, or a test that broke because something
  outside the repo changed between two runs of one commit, are reported as
  proven flaky. `--ignore-commits` turns the proof off. Runs recorded
  before 0.5.0 have no environment and share one.
- **Retry proof is read from Surefire/Failsafe's `flakyFailure` /
  `flakyError` only.** Other runners' retry output (pytest-rerunfailures,
  jest `retryTimes`, ...) is not recognised as a retry; whatever they write
  as the final result is recorded.
- **Failure modes fold text, they do not cluster** (see above).
- **A first-ever build with an empty report fails the Action's report
  step**: nothing was recorded, so there is no database to report on
  (`error: no flakebox database`). The next build that runs tests creates it.

## Tests

```bash
python3 -m unittest discover -s tests   # no network, stdlib only
python3 -m pytest -q                    # same suite
```

No PyYAML anywhere (the tests read `action.yml` with a tiny flat reader
and prove `import flakebox` works with `yaml` blocked). Tests point
`HOME`/`USERPROFILE`/`APPDATA` at a throwaway directory, so they never
touch your real `~/.flakebox`. A few tests drive real `pytest` as a
subprocess to prove the mapped `--deselect` ids and the generated plugin
work on parametrised ids (skipped when pytest is not installed). Runs on Windows 11: stdout/stderr are
reconfigured to replace unencodable characters (the report's emoji on a
cp1252 console), and every text file is opened as UTF-8.

## License

MIT — see `LICENSE`.
