"""Printing untrusted text safely.

Test ids, failure messages, commits and branches come from JUnit files and
the command line - from whoever wrote the test or pushed the branch. Printed
raw, a control character can rewrite the terminal (an ESC sequence can
clear the screen, retitle the window or hide the lines before it), and a
bidi override or zero-width character can make one id look like another.
Everything flakebox prints in its text formats goes through `text()`;
markdown cells also go through `md_code()`, and suggested commands through
`shell_arg()` so pasting one can never run anything. JSON output is already
safe: `json.dumps` escapes every control and non-ASCII character.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess

# C0 and C1 controls and DEL; bidi embeddings, overrides, isolates and
# marks; zero-width characters; the line/paragraph separators and the BOM.
_UNSAFE = re.compile(
    "[\x00-\x1f\x7f-\x9f"
    "؜​-‏ -‮⁠-⁩﻿]")


def _escape(match: re.Match) -> str:
    ch = match.group(0)
    code = ord(ch)
    if code <= 0xff:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}"


def text(value) -> str:
    """`value` as a string with every control, bidi and zero-width
    character shown as a visible escape (`\\x1b`, `\\u202e`). A lone
    surrogate (a stray byte from the command line) is escaped too, so the
    result always encodes as UTF-8."""
    if value is None:
        return "-"
    s = str(value)
    s = _UNSAFE.sub(_escape, s)
    if any("\ud800" <= c <= "\udfff" for c in s):
        s = "".join(f"\\u{ord(c):04x}" if "\ud800" <= c <= "\udfff" else c
                    for c in s)
    return s


def md_code(value) -> str:
    """`value` as an inline code span that is safe inside a markdown table
    cell: escaped by text(), `|` written as `\\|` (GitHub would otherwise
    end the cell there, even inside a code span), and fenced with more
    backticks than the longest backtick run in it."""
    s = text(value).replace("|", "\\|")
    longest = max((len(m) for m in re.findall(r"`+", s)), default=0)
    fence = "`" * (longest + 1)
    if s.startswith("`") or s.endswith("`") or longest:
        s = f" {s} "
    return f"{fence}{s}{fence}"


def md_cell(value) -> str:
    """Plain (non-code) table cell text: escaped, `|` escaped, and the
    characters markdown or HTML would interpret shown literally."""
    s = text(value)
    for ch in "\\`*_[]<>|":
        s = s.replace(ch, "\\" + ch)
    return s


def shell_arg(value: str) -> str:
    """`value` quoted as one argument for the local shell (POSIX quoting;
    cmd.exe quoting on Windows), so a suggested command printed with it
    can be pasted as is: an id containing `$(...)` or `"` stays a string.
    Control characters are escaped first, so the line cannot be altered on
    screen either."""
    s = text(value)
    if os.name == "nt":
        return subprocess.list2cmdline([s])
    return shlex.quote(s)
