# Changelog

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
