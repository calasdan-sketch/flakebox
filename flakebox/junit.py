"""JUnit XML ingestion: pytest, jest, surefire all speak JUnit."""

from __future__ import annotations

import hashlib
import sys
import xml.etree.ElementTree as ET

from .quarantine import REASON as QUARANTINE_REASON

# outcome per testcase
PASSED, FAILED, ERROR, SKIPPED = "passed", "failed", "error", "skipped"

# The guard looks for these in every byte encoding expat can read: any
# ASCII-compatible one (UTF-8, Latin-1, ...) plus UTF-16 LE/BE. Case is
# folded on the bytes (ASCII letters only, which is all a keyword has).
_BANNED = [kw.encode(enc) for kw in ("<!doctype", "<!entity")
           for enc in ("ascii", "utf-16-le", "utf-16-be")]


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
    a number counts as 0 with a warning rather than rejecting the whole
    report."""
    text = (text or "").strip()
    if not text:
        return 0
    try:
        return int(float(text.replace(",", ".")) * 1000)
    except ValueError:
        warn(f"{where}: unreadable time={text!r}, recorded as 0")
        return 0


def check_no_dtd(raw: bytes, path: str = "") -> None:
    """Reject a report containing a DOCTYPE or ENTITY declaration, in any
    encoding expat accepts: ElementTree performs no entity-expansion
    protection, and JUnit reports never legitimately need a DTD."""
    lowered = raw.lower()
    if any(b in lowered for b in _BANNED):
        raise ValueError(
            f"refusing to parse {path}: XML contains a DOCTYPE or ENTITY "
            "declaration (rejected as a possible XML bomb)")


def parse_bytes(raw: bytes, path: str = "", warn=_warn) -> list[dict]:
    """Return [{test_id, outcome, duration_ms, message}] for every
    <testcase> anywhere in the document - <testsuites>, a bare
    <testsuite>, or suites nested in suites (surefire aggregates)."""
    check_no_dtd(raw, path)
    root = ET.fromstring(raw)
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
            el = tc.find("failure")
            outcome, message = FAILED, (el.get("message", "") or "")[:500]
        elif tc.find("error") is not None:
            el = tc.find("error")
            outcome, message = ERROR, (el.get("message", "") or "")[:500]
        else:
            outcome, message = PASSED, ""
        results.append({"test_id": tid, "outcome": outcome,
                        "duration_ms": ms, "message": message})
    return results


def read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def parse(path: str, warn=_warn) -> list[dict]:
    """Parse the report at `path` (whole file read first, DTD guard,
    then ElementTree)."""
    return parse_bytes(read(path), path, warn)
