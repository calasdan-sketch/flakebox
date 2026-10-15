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

# when a quarantined test stabilizes, the report tells you:
flakebox unquarantine --test "pkg.Class::test_x"
flakebox history --test "pkg.Class::test_x"   # per-test run history
```

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

No ML, fully explainable. A test is **flaky** when it has both passes and
failures across ≥5 runs (tunable via `--min-runs`). A test that only ever
fails is **broken**, not flaky — different problem, don't quarantine it.
A quarantined test with ≥10 consecutive passes (tunable) is **stable** —
the report tells you to un-quarantine it. History lives in SQLite
(`~/.flakebox/history.db` by default, `--db` to override).

## GitHub Action

`action.yml` is a composite action: restores the DB from cache, records the
JUnit report, and posts the flakebox report to the job summary. Optional
`fail-on-flaky: true` fails the step on new flakes.

```yaml
- uses: your-org/flakebox@v1
  with:
    junit: reports/junit.xml
```

## Tests

```bash
python3 -m unittest discover -s tests   # 35 tests, no network
```

## License

TBD
