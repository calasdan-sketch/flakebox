"""Likely root-cause family of a failure, from its message alone.

Heuristic and honest about it: the family is a hint for where to look
first, chosen by the first matching rule below, never a verdict. A message
nothing matches is `unknown`, not a guess. Categories follow the usual
breakdown of flaky-test causes (timing / async waits, network, resource
limits, concurrency, shared state and test order, time and randomness).
"""

from __future__ import annotations

import re

# (family, pattern) in priority order: the first match wins.
_RULES = [
    ("network", re.compile(
        r"connection (?:refused|reset|aborted|closed)|econn(?:refused|reset)"
        r"|econnaborted|connect(?:ion)? (?:timed? ?out|error)|name or service "
        r"not known|getaddrinfo|\bdns\b|socket|unreachable|broken pipe|"
        r"http(?:/\d(?:\.\d)?)? ?(?:502|503|504)|\b(?:502|503|504)\b "
        r"(?:bad gateway|service unavailable|gateway)|ssl|tls handshake|"
        r"eof|remote end closed", re.I)),
    ("resource", re.compile(
        r"out of memory|outofmemory|\boom\b|memoryerror|no space left|disk full"
        r"|too many open files|emfile|address already in use|eaddrinuse"
        r"|cannot allocate|resource temporarily unavailable|port \S* in use",
        re.I)),
    ("timing", re.compile(
        r"time(?:d)? ?out|timeout|deadline|exceeded .*\b(?:ms|seconds?|s)\b"
        r"|did not (?:appear|become|complete|finish)|not (?:ready|visible) "
        r"(?:within|after)|waiting for|still (?:running|pending)|too slow|"
        r"took longer|expected .* within|eventually", re.I)),
    ("concurrency", re.compile(
        r"\brace\b|race condition|deadlock|concurrent ?modification|lock (?:wait )?timeout|"
        r"database is locked|already (?:locked|being used)|"
        r"thread|interrupted|unexpected (?:order|interleav)|"
        r"stale element|detached", re.I)),
    ("shared-state", re.compile(
        r"already exists|duplicate (?:key|entry)|unique constraint|"
        r"integrity ?error|leftover|not (?:cleaned|reset)|dirty|"
        r"fixture .* (?:not found|already)|state (?:leak|pollut)|"
        r"order[- ]dependent|previous test", re.I)),
    ("time-or-random", re.compile(
        r"timezone|time zone|\bdst\b|daylight|clock|midnight|"
        r"\bseed\b|random|flaky date|leap", re.I)),
]

FAMILIES = tuple(name for name, _ in _RULES) + ("unknown",)


def family(message) -> str:
    """The first matching family for a failure message, else `unknown`.
    Looks at the first 1 KiB only (an imported message can be any size)."""
    text = (message or "")[:1024]
    for name, pattern in _RULES:
        if pattern.search(text):
            return name
    return "unknown"
