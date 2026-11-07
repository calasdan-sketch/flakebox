"""JUnit XML ingestion: pytest, jest, surefire all speak JUnit."""

from __future__ import annotations

import hashlib
import sys
import xml.etree.ElementTree as ET
from xml.parsers import expat

from . import safe
from .quarantine import REASON as QUARANTINE_REASON

# outcome per testcase
PASSED, FAILED, ERROR, SKIPPED = "passed", "failed", "error", "skipped"
# Failed, then passed when the runner retried it, inside ONE run: Maven
# Surefire/Failsafe with rerunFailingTestsCount write the passing testcase
# with <flakyFailure>/<flakyError> children. Proof of flakiness on its own.
PASSED_ON_RERUN = "passed_on_rerun"
FLAKY_ELEMENTS = ("flakyFailure", "flakyError")

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


def _message(el) -> str:
    """The failure message of a <failure>/<error>/<flakyFailure> element:
    its `message` attribute; else the first non-blank line of its own text
    (runners that put everything in the body); else its `type` (Surefire
    leaves `message` out for an exception with a null message and puts the
    trace in a <stackTrace> child, so the element's own text is blank). At
    most 500 characters."""
    msg = (el.get("message", "") or "").strip()
    if not msg:
        for line in (el.text or "").splitlines():
            if line.strip():
                msg = line.strip()
                break
    if not msg:
        msg = (el.get("type", "") or "").strip()
    return msg[:500]


def _warn(msg: str) -> None:
    try:
        # msg can carry a test id or a path from the report: escaped
        print(f"flakebox: warning: {safe.text(msg)}", file=sys.stderr)
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


def raw_entries(raw: bytes, path: str = "", warn=_warn):
    """Every <testcase> of the document, in document order, as
    ([{test_id, outcome, duration_ms, message}], pytest_style). The
    entries are NOT collapsed: a runner that retries a test writes it
    once per attempt (see collapse())."""
    check_no_dtd(raw, path)
    root = ET.fromstring(raw)
    # pytest names its <testsuite> "pytest" (and its <testsuites> "pytest
    # tests"). pytest ids are unique, so a repeated one is a retry.
    pytest_style = any((ts.get("name") or "") == "pytest"
                       for ts in root.iter("testsuite")) \
        or (root.get("name") or "").startswith("pytest")
    results = []
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
            outcome, message = FAILED, _message(tc.find("failure"))
        elif tc.find("error") is not None:
            outcome, message = ERROR, _message(tc.find("error"))
        elif any(tc.find(tag) is not None for tag in FLAKY_ELEMENTS):
            el = next(tc.find(tag) for tag in FLAKY_ELEMENTS
                      if tc.find(tag) is not None)
            outcome, message = PASSED_ON_RERUN, _message(el)
        else:
            outcome, message = PASSED, ""
        results.append({"test_id": tid, "outcome": outcome,
                        "duration_ms": ms, "message": message})
    return results, pytest_style


def collapse(entries: list[dict], pytest_style: bool = False,
             duplicate_passes_are_reruns: bool = False) -> list[dict]:
    """One result per test id. A test that appears more than once in a
    report was run more than once: the runner retried it. Entries are in
    chronological order, and the LAST executed attempt decides the run:

    - last attempt failed -> `failed`/`error` (once - not one pass and one
      failure per attempt, which made an always-failing test look flaky);
    - last attempt passed after an earlier failure -> `passed_on_rerun`
      (proof of flakiness; the gotestsum --rerun-fails and Gradle
      test-retry shapes);
    - last attempt passed and the earlier ones look passed too ->
      `passed_on_rerun` for pytest reports (pytest-rerunfailures writes
      the failed attempts as bare <testcase> elements with no <failure>,
      so the retry is only visible as a repeated id) and when
      `duplicate_passes_are_reruns` is set; otherwise a plain pass, since
      other runners can legitimately repeat a name (table tests).
    A surefire `passed_on_rerun` stays one. Order of first appearance."""
    order: list[str] = []
    by_id: dict[str, list[dict]] = {}
    for r in entries:
        if r["test_id"] not in by_id:
            order.append(r["test_id"])
            by_id[r["test_id"]] = []
        by_id[r["test_id"]].append(r)
    out = []
    for tid in order:
        group = by_id[tid]
        if len(group) == 1:
            out.append(group[0])
            continue
        executed = [g for g in group if g["outcome"] != SKIPPED]
        if not executed:
            out.append(group[-1])
            continue
        last = executed[-1]
        earlier_fail = next((g for g in executed[:-1]
                             if g["outcome"] in (FAILED, ERROR,
                                                 PASSED_ON_RERUN)), None)
        if last["outcome"] in (FAILED, ERROR):
            out.append(dict(last))
        elif last["outcome"] == PASSED_ON_RERUN:
            out.append(dict(last))
        elif earlier_fail is not None:
            out.append({**last, "outcome": PASSED_ON_RERUN,
                        "message": earlier_fail["message"]})
        elif pytest_style or duplicate_passes_are_reruns:
            out.append({**last, "outcome": PASSED_ON_RERUN,
                        "message": ""})
        else:
            out.append(dict(last))
    return out


def parse_bytes(raw: bytes, path: str = "", warn=_warn,
                duplicate_passes_are_reruns: bool = False) -> list[dict]:
    """Return [{test_id, outcome, duration_ms, message}] for every test in
    the document - <testsuites>, a bare <testsuite>, or suites nested in
    suites (surefire aggregates) - with retried tests collapsed to one
    result (see collapse())."""
    entries, pytest_style = raw_entries(raw, path, warn)
    return collapse(entries, pytest_style, duplicate_passes_are_reruns)


def read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def parse(path: str, warn=_warn) -> list[dict]:
    """Parse the report at `path` (whole file read first, DTD guard,
    then ElementTree)."""
    return parse_bytes(read(path), path, warn)
