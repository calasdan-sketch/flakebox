"""Printing untrusted text safely.

Test ids, failure messages, commits and branches come from JUnit files and
the command line - from whoever wrote the test or pushed the branch. Printed
raw, a control character can rewrite the terminal (an ESC sequence can
clear the screen, retitle the window or hide the lines before it), and a
bidi override or zero-width character can make one id look like another.
Everything flakebox prints in its text formats goes through `text()`;
markdown cells also go through `md_code()`, and suggested commands through
`shell_arg()`, which refuses (returns None) any value it cannot quote so
that pasting the command is certainly harmless. JSON output is already
safe: `json.dumps` escapes every control and non-ASCII character.
"""

from __future__ import annotations

import os
import re
import shlex

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


# Characters that are literal inside double quotes in BOTH cmd.exe and
# PowerShell. Not in the list, and so never put in a Windows command:
# `"` (ends the quotes), `%` and `!` (cmd variable expansion), `$` and the
# backtick (PowerShell expansion and escapes), `\` (escapes a closing quote
# for the C runtime), and every control character.
_WINDOWS = os.name == "nt"   # tests patch this to try the Windows branch
_WINDOWS_SAFE = re.compile(r"[A-Za-z0-9_.:/\[\]\- ,=+@#~]+")


def shell_arg(value: str, windows: bool | None = None) -> str | None:
    """`value` quoted as one argument for the local shell, or None when it
    cannot be written so that pasting it is certainly harmless. Callers
    print another way to do the same thing for None.

    - POSIX: shlex.quote - single quotes, which the shell never expands
      inside, so any value is safe.
    - Windows (`windows=True`, default: os.name == "nt"): there is no
      quoting that is safe in both cmd.exe and PowerShell for every
      character, so only values made of _WINDOWS_SAFE characters are
      quoted (in double quotes); anything else is None.
    - A value with a control, bidi or zero-width character is None on
      both: printed escaped, it would no longer be the same value."""
    if text(value) != value:
        return None
    if windows is None:
        windows = _WINDOWS
    if windows:
        if not _WINDOWS_SAFE.fullmatch(value):
            return None
        return f'"{value}"'
    return shlex.quote(value)
