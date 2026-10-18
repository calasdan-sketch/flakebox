# Flakebox (`flakebox`)

Self-hosted flaky-test triage for small teams. Flaky tests eat 10–25% of
CI compute and erode trust in every red build. Trunk and Datadog will sell
you the fix per-seat; flakebox is the free, self-hosted alternative:
ingest JUnit XML, detect flakiness statistically, quarantine the flakes,
and get told when they're stable enough to come back. SQLite, stdlib-only,
no SaaS.

## Install

Zero dependencies — Python 3.10+ stdlib only.

```bash
pip install git+https://github.com/calasdan-sketch/flakebox
flakebox --version
```

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

The flake rate is failures ÷ executed runs; the `runs` column is
executed runs. History lives in SQLite (`--db` to override).

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
