# Changelog

## 0.5.0

- Other runners: `flakebox quarantine --format jest|vitest|go` prints a `--testNamePattern`, `-t` or `go test -skip` option (best effort: by test name; Go subtests cannot be listed).
- `flakebox report --format html`: a static flake-trend page (failures per run, an outcome strip per flaky test).
- `flakebox prune --keep-runs N`; the write-ahead log is folded into history.db when a command closes it; `--max-report-mb` (default 200) on `record` and `summary`.
- README: a pull-request comment workflow that updates its own comment by the marker; Windows wording limited to what the CI matrix runs.
- Deeply nested JSON in `import` is refused by a depth limit of our own (400 levels) instead of relying on RecursionError, which Python 3.14 does not raise with a large stack.
- `flakebox summary --junit REPORT`: markdown for a pull-request comment or a job summary that says which of this report's failures are known flakes and which are new ("3 failures are known flakes; this one is new"), lists tests that failed and passed on retry, and says which failures were already failing before (broken, not flaky). Run it before `flakebox record` of the same report.
- `--max-flaky-percent P` on `report` and `summary`: exit 1 when more than P percent of the active tests are flaky. `report --format json` now has a `totals` block (and `budget`).
- `--quarantine-days N` (off by default) with `--quarantine-expiry warn|fail` on `report`, `summary` and `quarantine`: a quarantine older than N days is listed, and with `fail` the command exits 1 until each one is looked at again. New `flakebox retriage --test ID` keeps a quarantined test and restarts its clock.
- A cost line in `report` and `summary`, built only from the `time` attributes and commits in the reports you recorded: how many recent runs a flaky test failed in, how much reported test time those failed attempts took, and how many runs repeated a commit that had already run. It is test time as the runner reports it, not CI wall-clock time. `report --format json` has it as `cost`.
- Removed the README's unsourced "10-25% of CI compute" claim.
- Failure messages: when a runner leaves `message` empty (jest-junit) or writes only "Failed" (gotestsum), the first useful line of the failure text is used.
- Reports from jest (jest-junit), vitest and Go (gotestsum, including `--rerun-fails`) are tested against real files written by those tools.
- The Action has new inputs `summary`, `comment-output`, `max-flaky-percent`, `quarantine-days` and `quarantine-expiry`.
- Tests only: the bash-run release-workflow steps are skipped on Windows.

## 0.4.6

- A test that fails and then passes inside one JUnit report (a retry plugin) is now recorded as a flake ("retried"), not hidden behind the worst outcome. `flakebox report` lists it ("Passed on retry"), `flakebox history` calls it flaky, and one such run is enough (it does not wait for `--min-runs`). A pass followed by a failure is still a failure. A duplicated test keeps its longest duration.
- The GitHub Action no longer runs `pip install`: it runs the package from its own checkout, so it works on a system Python that refuses pip installs (PEP 668).
- CI installs pytest (ten plugin tests skipped without it), smoke-tests the installed `flakebox` command on Linux, Windows and macOS, and runs the unpacked sdist's own tests.
- The release workflow refuses a tag whose commit is not on `origin/main`.

## 0.4.5

- `--min-runs` must now be 1 or more; `0` or a negative number is rejected with exit code 2.
- A test that appears twice in one JUnit report (retry plugins, collapsed ids) now counts as one run, with the worst outcome, instead of two runs.
- The GitHub Action installs flakebox with the same `python3` that runs it, and its cache-save key includes the run attempt so a re-run no longer collides.
- The Action is documented as Linux and macOS only.
- Clearer hint when releasing a test: un-quarantine only once it is stable now.
- Package metadata restored: authors, project URLs, keywords, classifiers (Python 3.10-3.14, Beta, pytest).
- `pip install flakebox` is the documented install.

## 0.4.4

- Previous release.
