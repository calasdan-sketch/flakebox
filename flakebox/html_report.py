"""A static HTML flake-trend page: one file, no script, no network. Failures
per recorded run, and for each flaky test a strip of its recent outcomes."""

from __future__ import annotations

import html

from . import store as store_mod

STRIP = 40      # outcomes shown per test
RUNS = 60       # runs shown in the failures-per-run chart

_CLASS = {"passed": "p", "failed": "f", "error": "f", "retried": "r",
          "skipped": "s"}

CSS = """
:root{--bg:#fff;--fg:#1d2330;--mut:#5b6475;--line:#d9dde5;--p:#2e9d62;
--f:#d6453d;--r:#d99a1f;--s:#b4bac6}
@media (prefers-color-scheme:dark){:root{--bg:#14171d;--fg:#e6e9ef;
--mut:#9aa3b2;--line:#2c323d;--p:#3fb97a;--f:#ef6a62;--r:#e6ad3a;--s:#5b6475}}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,sans-serif}main{max-width:900px;margin:0 auto}
h1{font-size:1.4rem;margin:0 0 4px}h2{font-size:1.1rem;margin:28px 0 8px}
.mut{color:var(--mut)}table{border-collapse:collapse;width:100%}
td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;
vertical-align:middle}code{font-size:.85em;word-break:break-all}
.strip{display:flex;gap:2px}.strip i{width:7px;height:16px;border-radius:2px;
background:var(--s)}i.p{background:var(--p)}i.f{background:var(--f)}
i.r{background:var(--r)}i.s{background:var(--s);opacity:.5}
.bars{display:flex;align-items:flex-end;gap:2px;height:80px;
border-bottom:1px solid var(--line)}.bars b{flex:1;min-width:3px;
background:var(--f);border-radius:2px 2px 0 0}
"""


def _run_failures(conn, n: int = RUNS) -> list[int]:
    """Failed/error/retried results per run, oldest of the last `n` first."""
    ids = store_mod.recent_run_ids(conn, n)
    out = []
    for rid in ids:
        (c,) = conn.execute(
            "SELECT COUNT(*) FROM results WHERE run_id = ? AND outcome IN "
            "('failed','error','retried')", (rid,)).fetchone()
        out.append(c)
    return out[::-1]


def render(conn, analysis: dict, title: str = "Flakebox trend") -> str:
    e = html.escape
    t = analysis["totals"]
    fails = _run_failures(conn)
    top = max(fails, default=0) or 1
    bars = "".join(
        f'<b style="height:{max(3, round(100 * c / top))}%" '
        f'title="{c} failing"></b>' if c else
        '<b style="height:2px;opacity:.3" title="0 failing"></b>'
        for c in fails)
    rows = []
    for s in analysis["flaky"]:
        hist = store_mod.test_history(conn, s["test_id"], STRIP)[::-1]
        cells = "".join(
            f'<i class="{_CLASS.get(h["outcome"], "s")}" '
            f'title="{e(h["outcome"])}"></i>' for h in hist)
        q = " (quarantined)" if s["quarantined"] else ""
        rows.append(
            f'<tr><td><code>{e(s["test_id"])}</code>{q}</td>'
            f'<td>{s["flake_rate"]:.0%}</td>'
            f'<td>{s["failures"]}/{s["passes"]}</td>'
            f'<td><div class="strip">{cells}</div></td></tr>')
    table = ("<table><tr><th>test</th><th>flake rate</th><th>fails/passes</th>"
             "<th>last outcomes (oldest to newest)</th></tr>"
             + "".join(rows) + "</table>") if rows else \
        '<p class="mut">No flaky tests detected.</p>'
    broken = "".join(f'<li><code>{e(s["test_id"])}</code> '
                     f'({s["failures"]} failures)</li>'
                     for s in analysis["broken"])
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{e(title)}</title><style>{CSS}</style></head><body><main>'
        f'<h1>{e(title)}</h1>'
        f'<p class="mut">{t["flaky"]} of {t["active"]} active tests are '
        f'flaky ({t["flaky_percent"]:g}%). Built from the runs recorded in '
        f'the database; nothing is fetched.</p>'
        f'<h2>Failing results per run (last {len(fails)} runs)</h2>'
        f'<div class="bars">{bars}</div>'
        f'<h2>Flaky tests</h2>{table}'
        + (f'<h2>Consistently failing</h2><ul>{broken}</ul>' if broken else '')
        + '<p class="mut">Green passed, red failed, amber failed then passed '
          'on retry, grey skipped.</p></main></body></html>\n')
