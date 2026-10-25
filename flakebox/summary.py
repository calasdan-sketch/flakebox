"""The markdown for a pull request or a job summary: of the tests that
failed in THIS report, which are known flakes and which are new.

Run it BEFORE `flakebox record` of the same report: the history then does
not yet contain this run, so a test that just failed for the first time is
new, not a "flake" made by its own failure.

A failed test is a known flake when it is flaky in the recorded history
(detect.analyze) or is quarantined. Anything else that failed is new. A
test that failed and then passed inside this report (a retry) is listed on
its own: the build is green, the test is not trustworthy.

Everything printed is deterministic: the same report and history give the
same text. Test names and messages come from the report, so they only ever
appear inside a code span (one fence longer than any backtick run in them),
cut to one line, with the @ of a mention broken.
"""

from __future__ import annotations

import re

from . import cost as cost_mod
from . import junit as junit_mod

MARKER = "<!-- flakebox:summary -->"
MAX_LISTED = 25
MAX_MESSAGE = 120


def classify(results: list[dict], analysis: dict,
             quarantined_ids) -> dict:
    """Split one report's results: {"known": [...], "new": [...],
    "broken": [...], "retried": [...], "failed": n, "tests": n}. `broken`
    failed in this report and in every recorded run before it (a bug, not a
    flake, and not new either). Each entry is a result row plus, for known
    flakes, `flake_rate` and `quarantined`."""
    flaky = {s["test_id"]: s for s in analysis["flaky"]}
    broken_ids = {s["test_id"] for s in analysis["broken"]}
    quarantined = set(quarantined_ids)
    known, new, broken, retried = [], [], [], []
    for r in results:
        if r["outcome"] in ("failed", "error"):
            if r["test_id"] in flaky or r["test_id"] in quarantined:
                target = known
            elif r["test_id"] in broken_ids:
                target = broken
            else:
                target = new
        elif r["outcome"] == junit_mod.RETRIED:
            target = retried
        else:
            continue
        row = dict(r)
        stat = flaky.get(r["test_id"])
        row["flake_rate"] = stat["flake_rate"] if stat else None
        row["quarantined"] = r["test_id"] in quarantined
        row["known"] = stat is not None or row["quarantined"]
        target.append(row)
    return {"known": known, "new": new, "broken": broken,
            "retried": retried,
            "failed": len(known) + len(new) + len(broken),
            "tests": len(results)}


def headline(c: dict) -> str:
    k, n, b = len(c["known"]), len(c["new"]), len(c.get("broken", ()))
    if not k and not n and not b:
        return "No test failed in this report."
    if not n and not b:
        if k == 1:
            return "This failure is a known flake."
        return f"These {k} failures are known flakes."
    if not k and not b:
        if n == 1:
            return "This failure is new; it is not a known flake."
        return f"These {n} failures are new; none is a known flake."
    if not k and not n:
        return ("1 failure was already failing." if b == 1
                else f"{b} failures were already failing.")
    parts = []
    if k:
        parts.append("1 failure is a known flake" if k == 1
                     else f"{k} failures are known flakes")
    if n:
        parts.append(("this one is new" if not b else "1 is new")
                     if n == 1 else f"{n} are new")
    if b:
        parts.append("1 was already failing" if b == 1
                     else f"{b} were already failing")
    if not k:      # no known flakes: start with the new ones
        parts[0] = parts[0].replace("1 is new", "1 failure is new").replace(
            f"{n} are new", f"{n} failures are new")
    return "; ".join(parts) + "."


def advice(c: dict) -> str:
    if c["new"]:
        return ("A new failure: look at it before you re-run; a re-run "
                "will not tell you what broke.")
    if c.get("broken"):
        return ("Nothing new failed, but a test that failed in every "
                "recorded run is broken, not flaky: re-running will not "
                "fix it.")
    if c["known"]:
        return ("Only known flakes failed: a re-run is reasonable, and "
                "flakebox will keep counting them.")
    return ""


def md_code(text: str) -> str:
    """`text` as one inline code span: one line, the fence longer than any
    run of backticks inside, padded when it starts or ends with one."""
    text = re.sub(r"\s+", " ", str(text)).strip()
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _message(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > MAX_MESSAGE:
        text = text[:MAX_MESSAGE - 1] + "…"
    return text.replace("@", "@​")


def _listing(rows: list[dict], with_rate: bool) -> list[str]:
    out = []
    for r in rows[:MAX_LISTED]:
        bits = [md_code(r["test_id"].replace("@", "@​"))]
        if with_rate and r["flake_rate"] is not None:
            bits.append(f"flaky {r['flake_rate']:.0%} of its runs")
        if r["quarantined"]:
            bits.append("quarantined")
        msg = _message(r["message"])
        line = "- " + ", ".join(bits)
        if msg:
            line += f": {md_code(msg)}"
        out.append(line)
    if len(rows) > MAX_LISTED:
        out.append(f"- and {len(rows) - MAX_LISTED} more")
    return out


def to_markdown(c: dict, est: dict | None = None, expired=None,
                expired_days: int | None = None,
                budget: str | None = None) -> str:
    lines = [MARKER, f"## Flakebox: {headline(c)}", ""]
    tip = advice(c)
    if tip:
        lines += [tip, ""]
    lines.append(f"{c['failed']} of {c['tests']} test(s) failed in this "
                 f"report.")
    if c["new"]:
        lines += ["", "### New failures", *_listing(c["new"], False)]
    if c.get("broken"):
        lines += ["", "### Already failing (broken, not flaky)",
                  *_listing(c["broken"], False)]
    if c["known"]:
        lines += ["", "### Known flakes", *_listing(c["known"], True)]
    if c["retried"]:
        lines += ["", "### Passed on retry (green, but not trustworthy)",
                  *_listing(c["retried"], True)]
    if expired:
        lines += ["", f"### Quarantine older than {expired_days} day(s)"]
        for e in expired[:MAX_LISTED]:
            lines.append(f"- {md_code(e['test_id'].replace('@', '@' + chr(0x200b)))}"
                         f", {e['days']} day(s)")
        lines.append("Look at each and run `flakebox retriage --test ID` to "
                     "keep it, or `flakebox unquarantine --test ID` to "
                     "release it.")
    if budget:
        lines += ["", budget]
    cost = cost_mod.line(est)
    if cost:
        lines += ["", cost]
    lines += ["", "<sub>flakebox: statistics over your own recorded runs, "
              "no AI. Same history, same words.</sub>", ""]
    return "\n".join(lines)
