# Changelog

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
