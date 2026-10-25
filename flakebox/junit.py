"""JUnit XML ingestion: pytest, jest, surefire all speak JUnit."""

from __future__ import annotations

import hashlib
import sys
import xml.etree.ElementTree as ET
from xml.parsers import expat

from .quarantine import REASON as QUARANTINE_REASON

# outcome per testcase
PASSED, FAILED, ERROR, SKIPPED = "passed", "failed", "error", "skipped"

# worst outcome wins when one report repeats a test
_SEVERITY = {SKIPPED: 0, PASSED: 1, FAILED: 2, ERROR: 3}

MAX_MS = 2 ** 63 - 1  # the largest duration an SQLite INTEGER can hold


def test_id(classname: str, name: str) -> str:
    classname = (classname or "").strip()
    name = (name or "").strip()
    return f"{classname}::{name}" if classname else name


def report_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_quarantine_xfail(skipped_el) -> bool:
    """pytest writes an xfail as <skipped type="pytest.xfail"
    message="expected test failure">reason</skipped>. Only the reason our
    own quarantine plugin attaches counts - a hand-written xfail stays a
    skip."""
    if (skipped_el.get("type") or "") != "pytest.xfail":
        return False
    text = (skipped_el.text or "") + " " + (skipped_el.get("message") or "")
    return QUARANTINE_REASON in text


def _warn(msg: str) -> None:
    try:
        print(f"flakebox: warning: {msg}", file=sys.stderr)
    except (OSError, ValueError):
        pass


def duration_ms(text, where: str = "", warn=_warn) -> int:
    """`time` attribute -> milliseconds. Accepts a decimal comma
    (`0,5`, written by some locales' runners); anything else that is not
    a finite, non-negative number of seconds the store can hold (`fast`,
    `inf`, `nan`, `-5`, `1e300`) counts as 0 with a warning rather than
    rejecting the whole report."""
    text = (text or "").strip()
    if not text:
        return 0
    try:
        ms = int(float(text.replace(",", ".")) * 1000)
    except (ValueError, OverflowError):  # not a number; or inf / nan
        ms = -1
    if not 0 <= ms <= MAX_MS:  # also: negative, or too big for the store
        warn(f"{where}: unreadable time={text!r}, recorded as 0")
        return 0
    return ms


class _Declaration(Exception):
    """expat reported a DOCTYPE or ENTITY declaration."""


def check_no_dtd(raw: bytes, path: str = "") -> None:
    """Reject a report that declares a DOCTYPE or an ENTITY. ElementTree
    performs no entity-expansion protection and JUnit reports never
    legitimately need a DTD, so the document is run through expat first and
    stopped at the first declaration, before anything is expanded.

    expat does the tokenising - the encoding (UTF-16 included), comments,
    processing instructions and CDATA - so text that merely CONTAINS
    "<!DOCTYPE html>" (captured output in a CDATA section, say) is not a
    declaration and passes. A document expat cannot tokenise is left for
    the real parse to report as not well-formed; no entity can have been
    declared in what it did read, because a declaration needs a DOCTYPE and
    the DOCTYPE would have stopped it first."""
    def refuse(*_args):
        raise _Declaration

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(raw, True)
    except _Declaration:
        raise ValueError(
            f"refusing to parse {path}: XML contains a DOCTYPE or ENTITY "
            "declaration (rejected as a possible XML bomb)") from None
    except expat.ExpatError:
        pass


def parse_bytes(raw: bytes, path: str = "", warn=_warn) -> list[dict]:
    """Return [{test_id, outcome, duration_ms, message}] for every
    <testcase> anywhere in the document - <testsuites>, a bare
    <testsuite>, or suites nested in suites (surefire aggregates)."""
    check_no_dtd(raw, path)
    root = ET.fromstring(raw)
    results = []
    seen: dict[str, dict] = {}
    cases = [root] if root.tag == "testcase" else root.iter("testcase")
    for tc in cases:
        tid = test_id(tc.get("classname", ""), tc.get("name", ""))
        ms = duration_ms(tc.get("time"), f"{path} testcase {tid}", warn)
        sk = tc.find("skipped")
        if sk is not None:
            if _is_quarantine_xfail(sk):
                # the generated plugin in xfail mode: the test really
                # ran and really failed, it just couldn't break the
                # build. Record the truth so stats stay honest.
                outcome, message = FAILED, (sk.text or "").strip()[:500]
            else:
                outcome, message = SKIPPED, ""
        elif tc.find("failure") is not None:
            el = tc.find("failure")
            outcome, message = FAILED, (el.get("message", "") or "")[:500]
        elif tc.find("error") is not None:
            el = tc.find("error")
            outcome, message = ERROR, (el.get("message", "") or "")[:500]
        else:
            outcome, message = PASSED, ""
        row = {"test_id": tid, "outcome": outcome,
               "duration_ms": ms, "message": message}
        # the same classname+name twice in one report (a retry plugin, a
        # parametrised id that collapses) is ONE run of that test: the
        # worst outcome wins, so one report never counts as two runs
        prev = seen.get(tid)
        if prev is None:
            seen[tid] = row
            results.append(row)
        elif _SEVERITY[outcome] > _SEVERITY[prev["outcome"]]:
            prev.update(row)
        else:
            prev["duration_ms"] = max(prev["duration_ms"], ms)
    return results


def read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def parse(path: str, warn=_warn) -> list[dict]:
    """Parse the report at `path` (whole file read first, DTD guard,
    then ElementTree)."""
    return parse_bytes(read(path), path, warn)
