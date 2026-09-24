#!/usr/bin/env python3
"""Generate docs/cdp-coverage.html — CDP universe coverage heatmap.

Inputs (produced by `make universe-report` / hurdle universe --json):
  universe-commands.json, universe-events.json
Universe metadata (source/version) is read from universes/*.json.

Output is a self-contained HTML file (inline CSS/JS, no external assets).
"""

from __future__ import annotations

import datetime
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = {
    "commands": ROOT / "universe-commands.json",
    "events": ROOT / "universe-events.json",
}
UNIVERSE_FILES = {
    "commands": ROOT / "universes" / "cdp-commands.json",
    "events": ROOT / "universes" / "cdp-events.json",
}
OUT = ROOT / "docs" / "cdp-coverage.html"

STATUS_LABEL = {"covered": "covered", "wildcard": "wildcard", "allowed": "reviewed", "gap": "GAP"}


def load_reports():
    data = {}
    for kind, path in REPORTS.items():
        with open(path, encoding="utf-8") as fh:
            data[kind] = json.load(fh)
    meta = {}
    for kind, path in UNIVERSE_FILES.items():
        try:
            with open(path, encoding="utf-8") as fh:
                meta[kind] = json.load(fh).get("meta", {})
        except OSError:
            meta[kind] = {}
    return data, meta


def domain_rows(data):
    """Unified per-domain row model across both universes."""
    rows = {}
    for kind in ("commands", "events"):
        for item in data[kind]["items"]:
            row = rows.setdefault(
                item["domain"],
                {
                    "domain": item["domain"],
                    "commands": {"covered": 0, "wildcard": 0, "allowed": 0, "gap": 0},
                    "events": {"covered": 0, "wildcard": 0, "allowed": 0, "gap": 0},
                    "reason": "",
                },
            )
            row[kind][item["status"]] += 1
            if item.get("allow_reason"):
                row["reason"] = item["allow_reason"]
    for row in rows.values():
        row["review"] = review_class(row)
    order = {"used": 0, "deferred": 1, "excluded": 2}
    return sorted(rows.values(), key=lambda r: (order[r["review"]], r["domain"]))


def review_class(row):
    used = (
        row["commands"]["covered"]
        + row["events"]["covered"]
        + row["commands"]["wildcard"]
        + row["events"]["wildcard"]
    )
    if used > 0:
        return "used"
    if "partially used" in row["reason"]:
        return "deferred"
    return "excluded"


def _wildcard_total(row):
    return row["commands"]["wildcard"] + row["events"]["wildcard"]


def kind_cell(stats):
    total = sum(stats.values())
    if total == 0:
        return '<td class="num dim">—</td>'
    used = stats["covered"] + stats["wildcard"]
    pct = 100 * used // total if total else 0
    cls = "full" if used == total else ("some" if used else "none")
    bar = (
        '<span class="bar"><i style="width:%d%%" class="%s"></i></span>'
        % (pct, cls)
    )
    return (
        '<td class="num"><span class="pct %s">%d/%d</span>%s</td>'
        % (cls, used, total, bar)
    )


REVIEW_BADGE = {
    "used": '<span class="badge used">used</span>',
    "deferred": '<span class="badge deferred">deferred</span>',
    "excluded": '<span class="badge excluded">not exposed</span>',
}


def summary_cards(data):
    cards = []
    for kind, label in (("commands", "CDP commands"), ("events", "CDP events")):
        items = data[kind]["items"]
        counts = {"covered": 0, "wildcard": 0, "allowed": 0, "gap": 0}
        for it in items:
            counts[it["status"]] += 1
        total = len(items)
        cards.append(
            '<div class="card"><h3>%s</h3><div class="big">%d<span class="dim">/%d</span></div>'
            "<div>covered <b>%d</b> · wildcard <b>%d</b> · reviewed <b>%d</b> · gap <b>%d</b></div></div>"
            % (
                html.escape(label),
                counts["covered"] + counts["wildcard"],
                total,
                counts["covered"],
                counts["wildcard"],
                counts["allowed"],
                counts["gap"],
            )
        )
    return "".join(cards)


def used_items_section(rows, data):
    used_domains = [r for r in rows if r["review"] == "used"]
    parts = []
    for row in used_domains:
        chunks = []
        for kind, label in (("commands", "commands"), ("events", "events")):
            names = [
                it["name"]
                for it in data[kind]["items"]
                if it["domain"] == row["domain"] and it["status"] in ("covered", "wildcard")
            ]
            if names:
                chips = "".join(
                    '<code class="%s">%s</code>'
                    % ("wc" if n.endswith("*") else "", html.escape(n))
                    for n in sorted(names)
                )
                chunks.append('<div class="chiprow"><span class="kind">%s</span>%s</div>' % (label, chips))
        parts.append(
            '<details open><summary><code>%s</code> <span class="dim">%s</span></summary>%s</details>'
            % (html.escape(row["domain"]), "used", "".join(chunks))
        )
    return "".join(parts) or '<p class="dim">No used domains.</p>'


def build_html():
    data, meta = load_reports()
    rows = domain_rows(data)
    generated = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    src_commands = meta["commands"].get("browser_version", "?")
    fetched = meta["commands"].get("fetched_at", "?")
    n_used = sum(1 for r in rows if r["review"] == "used")
    n_def = sum(1 for r in rows if r["review"] == "deferred")
    n_ex = sum(1 for r in rows if r["review"] == "excluded")

    table_rows = []
    for row in rows:
        table_rows.append(
            "<tr><td><code>%s</code></td>%s%s<td>%s</td><td class=\"dim reason\">%s</td></tr>"
            % (
                html.escape(row["domain"]),
                kind_cell(row["commands"]),
                kind_cell(row["events"]),
                REVIEW_BADGE[row["review"]],
                html.escape(row["reason"]),
            )
        )

    doc = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>cdpbrowser — CDP universe coverage</title>
<style>
:root{color-scheme:light}
body{font:14px/1.5 -apple-system,'Segoe UI',Roboto,sans-serif;margin:0;background:#f8fafc;color:#0f172a}
header{background:#0f172a;color:#e2e8f0;padding:22px 28px}
header h1{margin:0 0 4px;font-size:20px}
header .sub{color:#94a3b8;font-size:12px}
main{max-width:1080px;margin:0 auto;padding:24px 28px 48px}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0 26px}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 18px;min-width:220px;box-shadow:0 1px 2px rgba(15,23,42,.05)}
.card h3{margin:0;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#64748b}
.card .big{font-size:30px;font-weight:700;margin:6px 0}
.dim{color:#94a3b8}
table{border-collapse:collapse;width:100%%;background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid #eef2f7;text-align:left;vertical-align:middle}
th{background:#f1f5f9;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#475569}
td.num{white-space:nowrap;width:190px}
.pct{font-weight:600;margin-right:8px;display:inline-block;min-width:52px}
.pct.full{color:#15803d}.pct.some{color:#a16207}.pct.none{color:#64748b}
.bar{display:inline-block;width:90px;height:8px;background:#eef2f7;border-radius:4px;overflow:hidden;vertical-align:middle}
.bar i{display:block;height:100%%}
.bar i.full{background:#16a34a}.bar i.some{background:#facc15}.bar i.none{background:#cbd5e1}
.badge{font-size:11px;padding:2px 8px;border-radius:999px;white-space:nowrap}
.badge.used{background:#dcfce7;color:#166534}
.badge.deferred{background:#fef9c3;color:#854d0e}
.badge.excluded{background:#f1f5f9;color:#64748b}
code{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;background:#f1f5f9;padding:1px 5px;border-radius:4px}
details{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:8px 12px;margin:8px 0}
summary{cursor:pointer;font-weight:600}
.chiprow{margin:6px 0 2px}
.chiprow .kind{font-size:11px;color:#64748b;display:inline-block;width:76px;text-transform:uppercase}
code.wc{background:#dcfce7}
.reason{font-size:12px;max-width:330px}
h2{font-size:16px;margin:30px 0 10px}
.note{background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:10px 14px;font-size:13px;margin:14px 0}
footer{color:#94a3b8;font-size:12px;margin-top:36px;border-top:1px solid #e2e8f0;padding-top:14px}
</style></head><body>
<header>
<h1>cdpbrowser — CDP universe coverage</h1>
<div class="sub">protocol source: ChromeDevTools/devtools-protocol (browser_version %(src)s, fetched %(fetched)s) · generated %(generated)s</div>
</header>
<main>
<div class="cards">%(cards)s</div>
<div class="note"><b>Gate policy:</b> every item is used, wildcard-covered, or explicitly reviewed
(allowlist). Newly added protocol items cannot appear silently — <code>hurdle universe
cdp-commands --strict</code> / <code>cdp-events --strict</code> enforce this in CI.
Review status: <b>%(n_used)d</b> used · <b>%(n_def)d</b> deferred (partially used) · <b>%(n_ex)d</b> not exposed.</div>
<h2>Domain matrix</h2>
<table>
<thead><tr><th>Domain</th><th>Commands</th><th>Events</th><th>Review</th><th>Reason</th></tr></thead>
<tbody>%(rows)s</tbody>
</table>
<h2>Used domains — covered items</h2>
%(used)s
<footer>Regenerate: <code>make universe-report</code> · inputs: hurdle universe --json ·
generated by <code>tools/generate_universe_report.py</code></footer>
</main></body></html>
""" % {
        "src": html.escape(str(src_commands)),
        "fetched": html.escape(str(fetched)),
        "generated": generated,
        "cards": summary_cards(data),
        "rows": "".join(table_rows),
        "used": used_items_section(rows, data),
        "n_used": n_used,
        "n_def": n_def,
        "n_ex": n_ex,
    }
    return doc


def main():
    # attach helper (kept outside the row dict to stay JSON-clean)
    globals()["__noop"] = None
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build_html(), encoding="utf-8")
    print("wrote %s (%d bytes)" % (OUT, OUT.stat().st_size))


if __name__ == "__main__":
    main()
