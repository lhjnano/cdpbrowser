#!/usr/bin/env python3
"""Regenerate the vendored CDP protocol universes for `hurdle universe`.

Downloads the DevTools protocol definitions from the ChromeDevTools
devtools-protocol repository (master tip), extracts every non-deprecated
command and event as "Domain.name" strings, and writes:

    universes/cdp-commands.json
    universes/cdp-events.json

Each file is {"meta": {...}, "items": [...]} — the shape hurdle's
items_file loader expects (meta passes through to universe reports).
Items keep protocol file order: domains in declaration order, names in
declaration order within each domain.

The CI gates run against the committed snapshots; re-run this script
(or `make update-protocol`) only when adopting a newer protocol
revision. Deprecation is honored at both the domain and the
command/event level; experimental items are kept.

stdlib only.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

BASE_URL = (
    "https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol"
    "/master/json/"
)
SOURCES = ("browser_protocol.json", "js_protocol.json")
TIMEOUT = 30
USER_AGENT = "cdpbrowser-protocol-updater/1.0"


def fetch(name):
    req = urllib.request.Request(BASE_URL + name, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def collect(doc):
    """Return (version, domain_count, commands, events, excluded_counts)."""
    version = doc.get("version") or {}
    version_str = "%s.%s" % (version.get("major", "?"), version.get("minor", "?"))
    commands, events = [], []
    excluded = {"domains": 0, "commands": 0, "events": 0}
    domain_count = 0
    for dom in doc.get("domains", []):
        if dom.get("deprecated"):
            excluded["domains"] += 1
            continue
        domain_count += 1
        prefix = dom["domain"]
        for cmd in dom.get("commands", []):
            if cmd.get("deprecated"):
                excluded["commands"] += 1
                continue
            commands.append("%s.%s" % (prefix, cmd["name"]))
        for evt in dom.get("events", []):
            if evt.get("deprecated"):
                excluded["events"] += 1
                continue
            events.append("%s.%s" % (prefix, evt["name"]))
    return version_str, domain_count, commands, events, excluded


def assert_unique(seq):
    seen = set()
    for item in seq:
        if item in seen:
            sys.exit("duplicate item across protocol files: %s" % item)
        seen.add(item)


def write(path, items, meta):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "items": items}, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("wrote %s (%d items)" % (path, len(items)))


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "universes")
    os.makedirs(out_dir, exist_ok=True)

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    versions = {}
    commands, events = [], []
    domains_total = 0
    excluded = {"domains": 0, "commands": 0, "events": 0}

    for name in SOURCES:
        doc = fetch(name)
        version_str, domain_count, cmds, evts, excl = collect(doc)
        versions[name] = version_str
        domains_total += domain_count
        commands.extend(cmds)
        events.extend(evts)
        for key in excluded:
            excluded[key] += excl[key]
        print(
            "%s: version=%s domains=%d commands=%d events=%d (deprecated skipped:"
            " %d domains, %d commands, %d events)"
            % (
                name,
                version_str,
                domain_count,
                len(cmds),
                len(evts),
                excl["domains"],
                excl["commands"],
                excl["events"],
            )
        )

    assert_unique(commands)
    assert_unique(events)

    base_meta = {
        "source": "ChromeDevTools/devtools-protocol master",
        "fetched_at": fetched_at,
        "browser_version": versions["browser_protocol.json"],
        "js_protocol_version": versions["js_protocol.json"],
        "excluded_deprecated": excluded,
    }
    write(
        os.path.join(out_dir, "cdp-commands.json"),
        commands,
        dict(base_meta, counts={"domains": domains_total, "commands": len(commands)}),
    )
    write(
        os.path.join(out_dir, "cdp-events.json"),
        events,
        dict(base_meta, counts={"domains": domains_total, "events": len(events)}),
    )


if __name__ == "__main__":
    main()
