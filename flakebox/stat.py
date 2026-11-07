"""The statistics behind the verdicts: stdlib only, deterministic.

- wilson(): a 95% confidence interval for a failure rate. A test that
  failed 1 of 4 runs has a rate of 25% but anything from 1% to 70% is
  consistent with it; the report shows the interval so nobody reads two
  digits of precision into four runs.
- change_point(): did this test's failure rate step at some run? A
  stationary flake (the same chance every run) and a test that BROKE at
  commit X (passed for 40 runs, fails ever since) both contain passes and
  failures, and a "pass AND fail" rule calls both flaky - then quarantines
  the real regression. A likelihood-ratio scan over one change point tells
  them apart and names the run where the rate stepped.
- runs_to_catch(): how many runs it takes to see a flake of a given rate.
"""

from __future__ import annotations

import math

Z95 = 1.959964  # two-sided 95%
# Likelihood-ratio threshold (2 x the log-likelihood gain) for believing
# the failure rate stepped at one run. The scan looks at every split, so the
# usual chi-squared cutoff (3.84 for one parameter) is far too eager; 12
# keeps stationary flakes of rate 2-60% from being called a step in under
# 1% of 50-run histories (measured by benchmarks/accuracy.py).
CHANGE_LR = 12.0
MIN_BEFORE = 3  # runs on the old side of a step
MIN_AFTER = 3   # runs on the new side of a step


def wilson(failures: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for `failures` out of `n` runs. (0, 1) for
    n == 0: nothing is known."""
    if n <= 0:
        return 0.0, 1.0
    p = failures / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _ll(k: int, n: int) -> float:
    """Maximised Bernoulli log-likelihood of k failures in n runs."""
    if n == 0 or k == 0 or k == n:
        return 0.0
    p = k / n
    return k * math.log(p) + (n - k) * math.log(1 - p)


def change_point(series: list[int], threshold: float = CHANGE_LR):
    """`series` is 1 for a failed run and 0 for a pass, OLDEST first.
    Returns None when one constant failure rate explains the series, else
    {at, before, after, lr}: the failure rate was `before` for series[:at]
    and `after` from series[at:] on (the best single split, by likelihood
    ratio against the constant-rate model; `lr` is 2 x the gain)."""
    n = len(series)
    if n < MIN_BEFORE + MIN_AFTER:
        return None
    total = sum(series)
    if total == 0 or total == n:
        return None
    base = _ll(total, n)
    best_at, best = None, base
    run = 0
    for t in range(1, n):
        run += series[t - 1]
        if t < MIN_BEFORE or n - t < MIN_AFTER:
            continue
        ll = _ll(run, t) + _ll(total - run, n - t)
        if ll > best + 1e-12:
            best_at, best = t, ll
    if best_at is None:
        return None
    lr = 2 * (best - base)
    if lr < threshold:
        return None
    before = sum(series[:best_at]) / best_at
    after = sum(series[best_at:]) / (n - best_at)
    return {"at": best_at, "before": before, "after": after, "lr": lr}


def runs_to_catch(rate: float, confidence: float = 0.95) -> int | None:
    """Runs needed to see at least one failure of a test that fails with
    probability `rate` each run, with `confidence`. None for a rate of 0
    (it will never fail) ; 1 for a rate of 1."""
    if rate <= 0:
        return None
    if rate >= 1:
        return 1
    return math.ceil(math.log(1 - confidence) / math.log(1 - rate))
