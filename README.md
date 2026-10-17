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
flakebox record --junit reports/junit.xml --commit $GITHUB_SHA

# what is flaky?
flakebox report
flakebox report --format json

# quarantine the flaky set for pytest:
flakebox quarantine --format pytest -o .
pytest -p flakebox_quarantine        # flakes are skipped, build stays green

# ...or keep running the flakes without letting them fail the build, so
# they keep recording results and can earn their way back out:
flakebox quarantine --format pytest --mode xfail -o .

# the full quarantine set as pytest --deselect args (best effort, see
# below) or as JSON for other runners
pytest $(flakebox quarantine --format deselect)
flakebox quarantine --format json

# when a quarantined test stabilizes, the report tells you:
flakebox unquarantine --test "pkg.Class::test_x"
flakebox history --test "pkg.Class::test_x"   # per-test run history

# carry the history around (CI artifact -> laptop, job -> aggregator):
flakebox export --out flakebox-export.json
flakebox import flakebox-export.json           # merges; re-import is a no-op
```

`--db PATH` picks the database and goes before **or** after the
subcommand: `flakebox --db ci.db report` and `flakebox report --db ci.db`
are the same command. Default `~/.flakebox/history.db`
(`%USERPROFILE%\.flakebox\history.db` on Windows). A `--db` that is not a
SQLite database is a clean `error: cannot use database ...`, exit 2.

`flakebox record` hashes the report: recording the same file for the
same commit twice says `already recorded as run N` and adds nothing
(`--force` records it anyway), so a retried CI step cannot double-count.
Nested `<testsuite>` elements (surefire aggregates) are read, `time="0,5"`
is 500 ms, and a `time` that is not a number is recorded as 0 with a
warning instead of rejecting the report.

`flakebox quarantine` is idempotent and safe to run on every build: it
adds the current flaky set (never re-stamping tests already in
quarantine) and always emits the **whole** quarantine set, not just the
newly added tests. The one-line summary (`quarantined 1 new test(s); 3
in quarantine.`) goes to stderr for `--format deselect`, so stdout is
only ever the `--deselect` arguments.

`--format deselect` is **best effort**: JUnit ids are not pytest node
ids, so flakebox maps the common pytest shapes
(`pkg.module.Class::test_x` → `pkg/module.py::Class::test_x`,
`pkg.module::test_x` → `pkg/module.py::test_x`) and pytest ignores a
`--deselect` that matches nothing. A lowercase class name, a
`python_files` pattern other than `*.py`, or a non-pytest producer breaks
the guess; the generated plugin (`--format pytest`) suffix-matches node
ids instead and is the reliable path.

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
never breaks a pass streak.

- **flaky**: at least one pass *and* at least one failure (`failed` or
  `error`) across ≥5 executed runs (`--min-runs`), and not currently on
  a pass streak of ≥10 (`--stable-runs`) — a test that has been passing
  that long has recovered and is left alone, so quarantining it,
  releasing it and re-quarantining it from the same stale failures can't
  loop.
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

The generated pytest plugin reads `flakebox_quarantine.json`:

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
DB it came from, adds nothing; quarantine entries merge as a set. This
is how CI's history reaches a laptop, and how per-job DBs of a matrix
build become one (see the Action).

## GitHub Action

`action.yml` is a composite action: restores the DB from cache, records
the JUnit report, posts the flakebox report to the job summary, and —
optionally — quarantines the flaky set and exports the history as
artifact-ready files.

```yaml
- uses: your-org/flakebox@v1
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

| input               | default    | what it does                                  |
|---------------------|------------|-----------------------------------------------|
| `junit`             | (required) | path to the JUnit XML → `flakebox record --junit` |
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

and run the suite with `pytest -p flakebox_quarantine` (skip mode) or
`--mode xfail` so the quarantined tests keep reporting and can earn
their way back out. When the report says a test is stable, `flakebox
unquarantine --test ...` locally, regenerate the plugin, commit.

Input names use dashes everywhere (`min-runs`, never `min_runs`); the test
suite reads `action.yml` and checks that every referenced input is
declared, every declared input is used, and every CLI flag it passes
exists. Matrix builds: see the cache-race note at the top of `action.yml`
(per-job DBs, then an aggregator job that `flakebox import`s every job's
export).

## Tests

```bash
python3 -m unittest discover -s tests   # 76 tests, no network, stdlib only
python3 -m pytest -q                    # same suite
```

No PyYAML anywhere (the tests read `action.yml` with a tiny flat reader
and prove `import flakebox` works with `yaml` blocked). Tests point
`HOME`/`USERPROFILE`/`APPDATA` at a throwaway directory, so they never
touch your real `~/.flakebox`. One test drives real `pytest` as a
subprocess to prove the mapped `--deselect` ids work (skipped when
pytest is not installed). Runs on Windows 11: stdout/stderr are
reconfigured to replace unencodable characters (the report's emoji on a
cp1252 console), and every text file is opened as UTF-8.

## License

MIT — see `LICENSE`.
