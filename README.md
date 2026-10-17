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
git clone <repo> && cd flakebox
# no-install alias (run from the repo root):
alias flakebox="python3 -m flakebox"
# or: pip install .   # provides the `flakebox` console script
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

# other runners: the full quarantine set as pytest --deselect args / JSON
pytest $(flakebox quarantine --format deselect)
flakebox quarantine --format json

# when a quarantined test stabilizes, the report tells you:
flakebox unquarantine --test "pkg.Class::test_x"
flakebox history --test "pkg.Class::test_x"   # per-test run history
```

`flakebox quarantine` is idempotent and safe to run on every build: it
adds the current flaky set (never re-stamping tests already in
quarantine) and always emits the **whole** quarantine set, not just the
newly added tests. The one-line summary (`quarantined 1 new test(s); 3
in quarantine.`) goes to stderr for `--format deselect`, so stdout is
only ever the `--deselect` arguments.

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
- Everything else (always passing, or too few executed runs) is not
  reported.

The flake rate is failures ÷ executed runs; the `runs` column is
executed runs. History lives in SQLite (`~/.flakebox/history.db` by
default — `%USERPROFILE%\.flakebox\history.db` on Windows — `--db` to
override).

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

## GitHub Action

`action.yml` is a composite action: restores the DB from cache, records the
JUnit report, and posts the flakebox report to the job summary. Optional
`fail-on-flaky: true` fails the step on new flakes.

```yaml
- uses: your-org/flakebox@v1
  with:
    junit: reports/junit.xml
    min-runs: "5"          # -> flakebox report --min-runs 5
    fail-on-flaky: "true"
```

| input           | default    | what it does                                  |
|-----------------|------------|-----------------------------------------------|
| `junit`         | (required) | path to the JUnit XML → `flakebox record --junit` |
| `db-key`        | `flakebox` | cache key prefix for the history DB           |
| `min-runs`      | `5`        | executed runs before "flaky" → `flakebox report --min-runs` |
| `fail-on-flaky` | `false`    | fail the step when new flaky tests are found  |

Input names use dashes everywhere (`min-runs`, never `min_runs`); the test
suite reads `action.yml` and checks that every referenced input is
declared and every CLI flag it passes exists.

## Tests

```bash
python3 -m unittest discover -s tests   # 56 tests, no network, stdlib only
python3 -m pytest -q                    # same suite
```

No PyYAML anywhere (the tests read `action.yml` with a tiny flat reader
and prove `import flakebox` works with `yaml` blocked). Tests point
`HOME`/`USERPROFILE`/`APPDATA` at a throwaway directory, so they never
touch your real `~/.flakebox`. Runs on Windows 11: stdout/stderr are
reconfigured to replace unencodable characters (the report's emoji on a
cp1252 console), and every text file is opened as UTF-8.

## License

MIT — see `LICENSE`.
