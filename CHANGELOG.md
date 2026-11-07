# Changelog

## 0.6.0 (unreleased)

Benchmark: `python benchmarks/accuracy.py --check` (see README).

### Added
- `flakebox triage`, `renew`; `quarantine --owner --expires-days
  --auto-release`; `quarantine --format jest|go|maven`; `record --junit`
  takes several files or patterns; `--model stat|legacy`.
- Status `regressed` (change-point scan), 95% Wilson intervals,
  `stable_needed`, failure-mode `family`; JSON gains `regressed`,
  `rate_low/high`, `change`, `quarantine_expired`.
- Quarantine table: `owner`, `expires_at` (migrated; old entries never
  expire). Export/import carry them. The pytest plugin prints a summary
  of everything it quarantined and ignores an expired entry.
- Action inputs `triage`, `triage-output`.

### Changed
- Default model is `stat`: a regression is no longer listed as flaky or
  auto-quarantined (`--model legacy` for 0.5 behaviour); a quarantined test
  is "stable" after more than `--stable-runs` passes when its old failure
  rate needs more.
- A test repeated inside a report is one retried run: pytest reports (bare
  earlier attempts), fail-then-pass anywhere. Before, an always-failing
  test under `--reruns` read as flaky and a retried green test read as a
  clean pass. `--duplicate-passes-are-reruns` for other runners.
- `quarantine --format json` adds `expired` and `entries`; new quarantines
  expire after 30 days unless `--expires-days 0`.

## 0.5.0 (unreleased)

### Added
- **Proof of flakiness.** A test that passed and failed at the same
  `--commit`, or that Maven Surefire/Failsafe reports as failing and then
  passing on a retry (`<flakyFailure>` / `<flakyError>`, recorded as the new
  outcome `passed_on_rerun`), is flaky with fewer than `--min-runs` runs and
  ranks first. The report has a `proof` column; JSON gains
  `same_commit_flips`, `same_run_flips` and `proven`; `history` names the
  proof. `--ignore-commits` (report, quarantine, history) turns the
  same-commit proof off.
- **Failure modes.** Failure messages are folded into signatures (numbers,
  durations, ports, `0x` addresses become `N`). The report shows each
  flaky/failing test's top failure, `history` the top three with counts and
  each failed run's message, JSON has `failure_modes`. A failure element
  with no `message` attribute now records the first line of its text.
- `record` says how many tests passed only on a retry.

### Changed
- `report`, `quarantine`, `history`, `export` and `unquarantine` refuse a
  `--db` that does not exist (`error: no flakebox database at ...`, exit 2)
  instead of creating an empty one and reporting "No flaky tests detected".
  `record` and `import` still create it. An empty DB reports `No runs
  recorded yet`.
- Every text output escapes control, bidi and zero-width characters from
  test ids, messages, commits and paths. Markdown cells escape `|` and use
  a longer backtick fence when the id contains backticks.
- The suggested `flakebox unquarantine --test ...` command is shell-quoted
  (`--test t::x` instead of `--test "t::x"`), so an id containing `$(...)`
  or `"` cannot run anything when pasted.
- `unquarantine` of a test that is not quarantined says so on stderr
  (still exit 0).
- `store.test_history()` rows include `message`; `store.quarantine_remove()`
  returns whether the test was quarantined.

### Fixed after the panel review (still 0.5.0)
- **Windows unquarantine hints could run commands when pasted** (an id
  like `t::test[a&calc]` or `t::test[%PATH%]` was printed bare by
  `subprocess.list2cmdline`). On Windows an id is now only put in the
  command, double-quoted, when it is made of letters, digits, spaces and
  `_ . : / [ ] - , = + @ # ~`; otherwise the report suggests the new
  `flakebox unquarantine --all-stable` / `--all-retired`, which need no id.
  The same fallback is used on every platform for ids with `|`, a backtick
  or a control character (the escaped form matched nothing).
- `history` escapes the stored outcome; `import` refuses an outcome that
  is not one of `passed failed error skipped passed_on_rerun`.
- Same-commit proof is keyed on (commit, environment) and needs the pass
  and the failure to come from different runs: matrix legs and duplicate
  test ids no longer "prove" flakiness. New `flakebox record --env NAME`,
  stored per run, exported and imported, and part of the duplicate-report
  check; the Action records `--env`, from its new `env` input or
  `<db-key>-<runner.os>`.
- Failure-mode signatures keep short integers and digits inside
  identifiers (`assert 404 == 200` vs `500`, `HTTP 503` vs `401`, `user_1`
  vs `user_2`); only decimals, time/percent units, numbers after `:`, 4+
  digit numbers, hex and UUIDs fold. Each mode carries one raw `example`.
- A Surefire retry (`passed_on_rerun`) is one proof, not also a
  same-commit one; it counts as a pass and not in the flake rate (the run
  ended green), while still making the test flaky and breaking a streak.
- Read commands refuse a zero-byte file or a SQLite database without
  flakebox's tables (`is not a flakebox database`, exit 2) and leave it
  untouched; before, the schema was written into it and the report said
  "No runs recorded yet".
- A `<flakyError>` / `<failure>` with no `message` and no text uses its
  `type` attribute as the message.

### Compatibility
- Older flakebox versions that import a 0.5.0 export do not know the
  `passed_on_rerun` outcome: they ignore those results as evidence.
- `detect.stats()` takes optional `commits` (strings or (commit, env)
  tuples), `messages` and `run_ids`; existing calls are unchanged.
- The `runs` table gains an `env` column (added on open); exports gain
  `runs[].env`. Older flakebox ignores the field on import.
