# Changelog

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

### Compatibility
- Older flakebox versions that import a 0.5.0 export do not know the
  `passed_on_rerun` outcome: they ignore those results as evidence.
- `detect.stats()` takes optional `commits` and `messages`; existing calls
  are unchanged.
