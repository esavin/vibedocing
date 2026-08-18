"""Deterministic maintenance of PROJECT.md's Sync Status section.

The external audit found "Baseline commit: (none), Last synced: (never)" in a fully
documented project: the hub's sync fields were left to the LLM, which never filled
them. The pipeline (run.sh) now maintains them itself after every processed commit:

    python3 -m vibe_agent.hub --docs-root agent/project --baseline <sha> \
        --label "<short sha> (<subject>)" --date YYYY-MM-DD

Idempotent: rewrites the marker bullets in place; no-op when the markers are absent.
"""

import argparse
import os
import re
import sys

HUB = "PROJECT.md"
_BASELINE_RE = re.compile(r"^(\s*-\s*\*\*[Bb]aseline commit:\*\*.*)$")
_SYNCED_RE = re.compile(r"^(\s*-\s*\*\*[Ll]ast synced:\*\*.*)$")


def update_sync_status(docs_root, baseline, label="", today=""):
    path = os.path.join(docs_root, HUB)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return False
    changed = False
    if baseline:
        text = "`%s`%s" % (baseline[:12], (" — %s" % label) if label else "")
        synced = "%s%s" % (today or "n/a",
                           (" (through `%s`)" % baseline[:12]))
    else:
        text = "_(none yet — first run documents from the beginning)_"
        synced = "_(never)_"
    for index, line in enumerate(lines):
        if _BASELINE_RE.match(line):
            new = "- **Baseline commit:** %s" % text
            if lines[index] != new:
                lines[index] = new
                changed = True
        elif _SYNCED_RE.match(line):
            new = "- **Last synced:** %s" % synced
            if lines[index] != new:
                lines[index] = new
                changed = True
    if changed:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="vibe-agent-hub",
        description="Fill PROJECT.md Sync Status (baseline / last synced).")
    parser.add_argument("--docs-root", required=True)
    parser.add_argument("--baseline", default="", help="last processed commit sha")
    parser.add_argument("--label", default="", help="short sha + commit subject")
    parser.add_argument("--date", default="", help="YYYY-MM-DD")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    changed = update_sync_status(os.path.realpath(args.docs_root),
                                 args.baseline, args.label, args.date)
    print("hub: %s" % ("updated" if changed else "no change (markers not found)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
