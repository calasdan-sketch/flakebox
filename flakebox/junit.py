"""JUnit XML ingestion: pytest, jest, surefire all speak JUnit."""

from __future__ import annotations

import xml.etree.ElementTree as ET

# outcome per testcase
PASSED, FAILED, ERROR, SKIPPED = "passed", "failed", "error", "skipped"


def test_id(classname: str, name: str) -> str:
    classname = (classname or "").strip()
    name = (name or "").strip()
    return f"{classname}::{name}" if classname else name


def parse(path: str) -> list[dict]:
    """Return [{test_id, outcome, duration_ms, message}].

    Rejects files containing DOCTYPE or ENTITY declarations before
    parsing: ElementTree performs no entity-expansion protection, and
    JUnit reports never legitimately need a DTD. This is the whole-file
    XML-bomb guard (read first, never parse hostile input).
    """
    with open(path, "rb") as f:
        raw = f.read()
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError(
            f"refusing to parse {path}: XML contains a DOCTYPE or ENTITY "
            "declaration (rejected as a possible XML bomb)")
    root = ET.fromstring(raw)
    # <testsuites> or bare <testsuite>
    suites = root.findall("testsuite") or ([root] if root.tag == "testsuite"
                                           else [])
    results = []
    for suite in suites:
        for tc in suite.findall("testcase"):
            tid = test_id(tc.get("classname", ""), tc.get("name", ""))
            duration_ms = int(float(tc.get("time", "0") or 0) * 1000)
            if tc.find("skipped") is not None:
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
                            "duration_ms": duration_ms, "message": message})
    return results
