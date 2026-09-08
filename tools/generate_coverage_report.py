#!/usr/bin/env python
"""Generates docs/coverage.html — the CdpBrowser coverage heatmap report.

Reproduce everything with:

    make coverage   # pytest --cov + coverage-run robot + combine + this script

Consumes two artifacts:
- coverage.json  (combined pytest + robot runs, branch coverage)
- results/output.xml  (the Robot acceptance run)

Renders, in the zfs-heatmap style (tables + badges + explicit boundaries):
1. Module x test-method matrix — how each module is exercised
   (unit-pure / unit-mock / real-chrome / robot-atest), derived from the
   curated METHOD_MATRIX below, not from coverage numbers.
2. Combined branch-coverage table per module (real numbers).
3. Gap classification: filled / env-constrained / defensive-only /
   subprocess-isolated — with reasons, mirroring the support boundary.
4. Bugs found through this cross-validation round.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Curated data — the honest part the tooling cannot derive.
# ---------------------------------------------------------------------------

#: module -> {method: cell}, cell = "ok" | "partial" | "no" | "n/a"
METHOD_MATRIX = {
    "cdp/transport.py": {
        "unit-pure": "no", "unit-mock": "ok", "real-chrome": "ok", "robot-atest": "ok",
    },
    "cdp/chrome.py": {
        "unit-pure": "ok", "unit-mock": "no", "real-chrome": "ok", "robot-atest": "ok",
    },
    "cdp/errors.py": {
        "unit-pure": "ok", "unit-mock": "ok", "real-chrome": "ok", "robot-atest": "n/a",
    },
    "locators.py": {
        "unit-pure": "ok", "unit-mock": "no", "real-chrome": "ok", "robot-atest": "ok",
    },
    "keys.py": {
        "unit-pure": "ok", "unit-mock": "no", "real-chrome": "ok", "robot-atest": "ok",
    },
    "polling.py": {
        "unit-pure": "ok", "unit-mock": "no", "real-chrome": "ok", "robot-atest": "ok",
    },
    "promises.py": {
        "unit-pure": "no", "unit-mock": "ok", "real-chrome": "ok", "robot-atest": "ok",
    },
    "page.py": {
        "unit-pure": "partial", "unit-mock": "partial", "real-chrome": "ok", "robot-atest": "ok",
    },
    "bridge.js": {
        "unit-pure": "no", "unit-mock": "no", "real-chrome": "ok", "robot-atest": "ok",
    },
    "recorder.js": {
        "unit-pure": "no", "unit-mock": "no", "real-chrome": "ok", "robot-atest": "no",
    },
    "recorder.py": {
        "unit-pure": "ok", "unit-mock": "no", "real-chrome": "ok", "robot-atest": "ok",
    },
    "library.py": {
        "unit-pure": "partial", "unit-mock": "ok", "real-chrome": "ok", "robot-atest": "ok",
    },
    "listener.py": {
        "unit-pure": "partial", "unit-mock": "no", "real-chrome": "no", "robot-atest": "ok",
    },
    "visual.py": {
        "unit-pure": "ok", "unit-mock": "no", "real-chrome": "ok", "robot-atest": "ok",
    },
}

#: Remaining-gap classification with reasons (the "why not 100%" honesty).
GAPS = [
    ("✅ filled", "__init__.py guards", "Robot-missing import guards verified by subprocess tests (test_packaging.py) — isolated processes, so their lines are outside the combined measurement by design."),
    ("🟡 env-constrained", "chrome.py kill fallbacks (SIGKILL path, stderr diagnostics 408-441)", "Forcing a Chrome process to ignore SIGTERM deterministically is not reproducible in CI; covered by defensive coding + manual verification."),
    ("🟡 env-constrained", "chrome.py needs_no_sandbox under root", "Requires euid 0; CI never runs as root. Pure function is unit-tested for non-root."),
    ("⬜ defensive-only", "transport.py close-error branches (234-264)", "Unsubscribes/cleanups when the loop is already dead — defensive paths that only trigger on transport-internal races."),
    ("⬜ defensive-only", "promises.py dispatcher matcher/response exception swallowing", "Logged-not-raised paths; the error-completion contract itself IS tested (test_gapfill_core)."),
    ("⬜ defensive-only", "library.py evidence artifact fallbacks (940-953, 1087)", "Artifact-save failures must never flip a verdict — deliberately quiet, exercised only with a poisoned filesystem."),
    ("⬜ subprocess-isolated", "__init__.py / CdpBrowser.py shim guard lines", "The guards run in blocked-robot subprocesses (test_packaging.py); coverage of subprocesses is intentionally not wired."),
    ("⬜ defensive-only", "page.py frame-context race retries (1386-1430)", "Short race windows after scope entry — polling retries whose happy path is covered; the retry branch needs an artificially slowed browser."),
    ("🟡 env-constrained", "listener.py failure-capture paths (90-112)", "Only reachable when a Robot test FAILS with a live browser; atest is green by definition. Verified manually by breaking a suite once."),
]

#: Bugs found through cross-validation in this round (record → replay push).
BUGS = [
    ("Recorder", "Runtime.addBinding was callable but silent", "bindingCalled events are only delivered after Runtime.enable — recording saw nothing until the domain was enabled at start.", "start_recording sends Runtime.enable first"),
    ("Recorder", "Arm flag died across navigation", "window.__cdpbRecOn was set on the current document only; the first recorded navigation silently stopped recording.", "flag registered as its own tracked new-document script, removed on stop"),
    ("Recorder", "Duplicate fill after ENTER", "The change event fired on Enter-commit re-emitted the identical fill.", "lastEmittedFill key dedupe in flushFill"),
    ("Recorder", "Robot cell quoting corrupted values", "Values with spaces were wrapped in double quotes — Robot keeps quotes literal, so replays filled \"Jane Doe\" with quotes.", "escape only Robot syntax characters ($ @ & # \\), never quote"),
    ("Recorder", "Programmatic blob-anchor double recording", "downloadBlob helpers click a transient <a download> — the synthetic click was recorded with a dead structural selector.", "recorder skips blob:/data: download anchors"),
    ("Dialogs", "clearBrowserCoins absent at browser endpoint", "Network.clearBrowserCookies exists only via page session on Chrome 131 — browser-level send raised -32601.", "routed through the session"),
    ("Downloads", "Broker session binding hid browser-level events", "Browser.downloadProgress arrives without sessionId; a session-bound broker arm never matched.", "download broker is created unbound"),
    ("Shadow DOM", "ShadowRoot has no evaluate()", "Deep XPath first called root.evaluate (absent), then document.evaluate with a #document-fragment context (NotSupportedError).", "context = root.firstElementChild + // → .// rewrite"),
    ("Scrolling", "Wheel applied asynchronously", "Reading scrollY right after dispatch saw the pre-scroll value — one-event-stale reads.", "scroll_by polls briefly for the metrics to settle"),
    ("Keys", "Special keys ignored (rawKeyDown) + snake_case wire fields", "Chrome ignores keyDown without text for special keys, and the CDP wire needs camelCase field names.", "down-type rule + transport-boundary rename"),
    ("Dialogs", "Lazy coordinator lost un-armed dialogs", "Dialog subscription was created on first arm — dialogs opened without one were lost and the backstop never fired.", "coordinator starts eagerly at attach + prime()"),
]

CELL_BADGES = {
    "ok": '<span class="b-done">✓</span>',
    "partial": '<span class="b-part">◐</span>',
    "no": '<span class="b-todo">·</span>',
    "n/a": '<span class="b-na">—</span>',
}


def load_coverage(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for fname, info in sorted(data["files"].items()):
        short = fname.split("cdpbrowser/")[-1]
        rows.append(
            {
                "module": short,
                "statements": info["summary"]["num_statements"],
                "missing": info["summary"]["missing_lines"],
                "branches": info["summary"]["num_branches"],
                "partial": info["summary"]["num_partial_branches"],
                "coverage": info["summary"]["percent_covered"],
            }
        )
    rows.sort(key=lambda r: r["coverage"])
    return {"total": data["totals"]["percent_covered"], "rows": rows}


def load_robot(path: Path) -> dict:
    root = ET.parse(path).getroot()
    stat = root.find(".//statistics/total/stat")
    suites = len(list(root.iter("suite"))) - 1  # minus the root suite
    return {
        "pass": int(stat.get("pass")),
        "fail": int(stat.get("fail")),
        "suites": suites,
    }


def cov_badge(pct: float) -> str:
    if pct >= 95:
        cls = "b-done"
    elif pct >= 85:
        cls = "b-part"
    else:
        cls = "b-todo"
    return f'<span class="{cls}">{pct:.0f}%</span>'


def main() -> int:
    cov_path = ROOT / "coverage.json"
    robot_path = ROOT / "results" / "output.xml"
    if not cov_path.is_file():
        print("coverage.json not found — run `make coverage` first", file=sys.stderr)
        return 1
    if not robot_path.is_file():
        print("results/output.xml not found — run the robot suite first", file=sys.stderr)
        return 1

    cov = load_coverage(cov_path)
    robot = load_robot(robot_path)

    matrix_rows = []
    for module, cells in METHOD_MATRIX.items():
        badges = "".join(
            f"<td class='num'>{CELL_BADGES[cells.get(m, 'n/a')]}</td>"
            for m in ("unit-pure", "unit-mock", "real-chrome", "robot-atest")
        )
        matrix_rows.append(f"<tr><td><code>{module}</code></td>{badges}</tr>")
    matrix_html = "\n".join(matrix_rows)

    cov_rows = []
    for row in cov["rows"]:
        cov_rows.append(
            "<tr>"
            f"<td><code>{row['module']}</code></td>"
            f"<td class='num'>{row['statements']}</td>"
            f"<td class='num'>{row['missing']}</td>"
            f"<td class='num'>{row['branches']}</td>"
            f"<td class='num'>{row['partial']}</td>"
            f"<td class='num'>{cov_badge(row['coverage'])}</td>"
            "</tr>"
        )
    cov_html = "\n".join(cov_rows)

    gaps_html = "\n".join(
        f"<tr><td>{cls}</td><td><code>{where}</code></td><td>{reason}</td></tr>"
        for cls, where, reason in GAPS
    )
    bugs_html = "\n".join(
        f"<tr><td>{area}</td><td><b>{bug}</b></td><td>{detail}</td><td><code>{fix}</code></td></tr>"
        for area, bug, detail, fix in BUGS
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CdpBrowser — Test Coverage Heatmap (layer x method)</title>
<style>
:root{{--bg:#f6f8fa;--card:#fff;--border:#d8dee4;--text:#1f2328;--muted:#57606a;--accent:#0969da}}
*{{box-sizing:border-box}}
body{{margin:0;padding:28px 16px;background:var(--bg);color:var(--text);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.wrap{{max-width:1120px;margin:0 auto}}
h1{{font-size:24px;margin:0 0 6px}}
h2{{font-size:19px;border-bottom:2px solid var(--accent);padding-bottom:6px;margin:36px 0 12px}}
.meta{{color:var(--muted);font-size:13.5px;margin:0 0 4px}}
table{{border-collapse:collapse;background:var(--card);font-size:13.5px;width:100%}}
th,td{{border:1px solid var(--border);padding:6px 11px;text-align:left;vertical-align:top}}
th{{background:#f0f3f6;font-size:12.5px;color:#424a53;white-space:nowrap}}
td.num,th.num{{text-align:center;font-variant-numeric:tabular-nums}}
.b-done{{display:inline-block;min-width:2em;text-align:center;padding:1px 7px;border-radius:999px;font-weight:700;font-size:12px;background:#dafbe1;color:#116329}}
.b-part{{display:inline-block;min-width:2em;text-align:center;padding:1px 7px;border-radius:999px;font-weight:700;font-size:12px;background:#fff8c5;color:#4d2d00}}
.b-todo{{display:inline-block;min-width:2em;text-align:center;padding:1px 7px;border-radius:999px;font-weight:700;font-size:12px;background:#ffebe9;color:#82071e}}
.b-na{{color:#8b949e}}
.note{{color:var(--muted);font-size:12.5px;margin:8px 0 0}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 22px;min-width:150px}}
.card .n{{font-size:26px;font-weight:800;color:var(--accent)}}
.card .l{{font-size:12.5px;color:var(--muted)}}
code{{background:#eff2f5;border-radius:4px;padding:0 5px;font-size:12.5px}}
footer{{margin-top:36px;padding-top:14px;border-top:1px solid var(--border);color:var(--muted);font-size:12.5px}}
</style>
</head>
<body>
<div class="wrap">

<h1>CdpBrowser — Test Coverage Heatmap</h1>
<p class="meta">Layer &times; method matrix &middot; combined pytest + Robot branch coverage &middot; gap classification &amp; cross-validation findings</p>

<div class="cards">
  <div class="card"><div class="n">{len(cov['rows'])}</div><div class="l">modules measured</div></div>
  <div class="card"><div class="n">{cov['total']:.0f}%</div><div class="l">combined branch coverage</div></div>
  <div class="card"><div class="n">{robot['pass']}</div><div class="l">Robot acceptance tests passing</div></div>
  <div class="card"><div class="n">{len(BUGS)}</div><div class="l">bugs found via cross-validation</div></div>
</div>

<h2>1. Module &times; test-method matrix</h2>
<p class="note">How each layer is exercised. <span class="b-done">✓</span> exercised &middot;
<span class="b-part">◐</span> partial &middot; <span class="b-todo">·</span> not applicable
to that method &middot; <span class="b-na">—</span> not applicable.</p>
<table>
<tr><th>Module</th><th class="num">unit-pure</th><th class="num">unit-mock</th><th class="num">real-chrome</th><th class="num">robot-atest</th></tr>
{matrix_html}
</table>
<p class="note">bridge.js / recorder.js have no line coverage of their own — they are
exercised through the Python layers that inject them; their truth lives in the
real-chrome and robot-atest columns.</p>

<h2>2. Combined branch coverage (pytest + Robot under coverage)</h2>
<table>
<tr><th>Module</th><th class="num">Stmts</th><th class="num">Miss</th><th class="num">Branches</th><th class="num">Partial</th><th class="num">Cover</th></tr>
{cov_html}
</table>
<p class="note">Measured as <code>coverage run -m pytest</code> + <code>coverage run -m robot</code>,
combined — keyword-adapter lines exercised only by Robot runs are counted, not
hidden. Reproduce with <code>make coverage</code>.</p>

<h2>3. Remaining gaps — classified, with reasons</h2>
<table>
<tr><th>Class</th><th>Where</th><th>Why it is not covered / why that is acceptable</th></tr>
{gaps_html}
</table>

<h2>4. Bugs found through cross-validation</h2>
<p class="note">Real defects this suite (and its recording replay round) surfaced —
each row is regression-pinned by a test.</p>
<table>
<tr><th>Area</th><th>Bug</th><th>Symptom &rarr; cause</th><th>Fix</th></tr>
{bugs_html}
</table>

<footer>
Generated by <code>tools/generate_coverage_report.py</code> from coverage.json +
results/output.xml &middot; regenerate with <code>make coverage</code>.
</footer>

</div>
</body>
</html>
"""
    out = ROOT / "docs" / "coverage.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({len(html)} bytes, total {cov['total']:.1f}%)")

    markdown = render_markdown(cov, robot)
    md_out = ROOT / "docs" / "coverage.md"
    md_out.write_text(markdown, encoding="utf-8")
    print(f"wrote {md_out} ({len(markdown)} bytes)")
    return 0


def render_markdown(cov: dict, robot: dict) -> str:
    """The same report as GitHub-Flavored Markdown (diff-friendly)."""
    md_badges = {"ok": "✅", "partial": "◐", "no": "·", "n/a": "—"}

    lines = [
        "# CdpBrowser — Test Coverage Heatmap",
        "",
        "Layer × method matrix · combined pytest + Robot branch coverage ·"
        " gap classification & cross-validation findings.",
        "",
        f"- **{len(cov['rows'])}** modules measured — **{cov['total']:.0f}%**"
        " combined branch coverage",
        f"- **{robot['pass']}** Robot acceptance tests passing",
        f"- **{len(BUGS)}** bugs found via cross-validation (each pinned by a"
        " regression test)",
        "",
        "> Regenerate with `make coverage` (runs pytest and the Robot suite"
        " under coverage, combines, and renders this file plus"
        " `docs/coverage.html`).",
        "",
        "## 1. Module × test-method matrix",
        "",
        "| Module | unit-pure | unit-mock | real-chrome | robot-atest |",
        "| --- | :-: | :-: | :-: | :-: |",
    ]
    for module, cells in METHOD_MATRIX.items():
        row = " | ".join(md_badges[cells.get(m, "n/a")] for m in
                        ("unit-pure", "unit-mock", "real-chrome", "robot-atest"))
        lines.append(f"| `{module}` | {row} |")
    lines += [
        "",
        "✅ exercised · ◐ partial · · not used by that method · — n/a.",
        "`bridge.js` / `recorder.js` have no line coverage of their own —"
        " they are exercised through the Python layers that inject them.",
        "",
        "## 2. Combined branch coverage (pytest + Robot under coverage)",
        "",
        "| Module | Stmts | Miss | Branches | Partial | Cover |",
        "| --- | ---: | ---: | ---: | ---: | :-: |",
    ]
    for row in cov["rows"]:
        pct = f"{row['coverage']:.0f}%"
        icon = "🟢" if row["coverage"] >= 95 else ("🟡" if row["coverage"] >= 85 else "🔴")
        lines.append(
            f"| `{row['module']}` | {row['statements']} | {row['missing']} |"
            f" {row['branches']} | {row['partial']} | {icon} {pct} |"
        )
    lines += [
        "",
        "The Robot suite runs under coverage as well, so lines exercised"
        " only through keywords are counted.",
        "",
        "## 3. Remaining gaps — classified, with reasons",
        "",
        "| Class | Where | Why it is not covered / why that is acceptable |",
        "| --- | --- | --- |",
    ]
    for cls, where, reason in GAPS:
        lines.append(f"| {cls} | `{where}` | {reason} |")
    lines += [
        "",
        "## 4. Bugs found through cross-validation",
        "",
        "Defects found while building the suite. Each row is pinned by a"
        " regression test.",
        "",
        "| Area | Bug | Symptom → cause | Fix |",
        "| --- | --- | --- | --- |",
    ]
    for area, bug, detail, fix in BUGS:
        lines.append(f"| {area} | **{bug}** | {detail} | `{fix}` |")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
