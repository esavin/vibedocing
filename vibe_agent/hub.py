"""Deterministic maintenance of PROJECT.md (Sync Status + navigation coverage).

Two LLM-drift failure modes in the hub are maintained by the pipeline, not by
the model:

- the external audit found "Baseline commit: (none), Last synced: (never)" in
  a fully documented project: the sync fields were left to the LLM, which never
  filled them. The pipeline (run.sh) now rewrites the marker bullets itself
  after every processed commit.
- orphan navigation drift: the agent rewrites the hub per commit from a small
  per-commit context and old entries fall out - docs exist on disk but are no
  longer reachable from PROJECT.md (a real run: 45 of 197 docs linked). After
  every processed commit the pipeline now re-adds a link entry for every
  functions/ or design/ doc the hub no longer lists and drops entries pointing
  to docs that no longer exist.

    python3 -m vibe_agent.hub --docs-root agent/project --baseline <sha> \
        --label "<short sha> (<subject>)" --date YYYY-MM-DD

Idempotent: rewrites the marker bullets in place; the navigation pass is a
no-op when the hub already covers every doc. Section headings are located by
the "function" / "design" substrings (## Function Documentation, ## Technical
Design Documents); without a matching heading there is nothing to anchor new
entries to, and the pass reports the orphans instead of guessing structure.
"""

import argparse
import os
import re
import sys

from .validate import _LINK_RE, _SKIP_PREFIXES, DOCS_TOP_DIRS

HUB = "PROJECT.md"
_BASELINE_RE = re.compile(r"^(\s*-\s*\*\*[Bb]aseline commit:\*\*.*)$")
_SYNCED_RE = re.compile(r"^(\s*-\s*\*\*[Ll]ast synced:\*\*.*)$")

# navigation sections are located by a substring of their heading text
_NAV_SECTIONS = (
    ("function", "functions"),
    ("design", "design"),
)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
_HR_RE = re.compile(r"^-{3,}\s*$")
_PLACEHOLDER_RE = re.compile(r"^_\(.*\)_$|^_To be populated\._$")


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


def _read_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return None


def _doc_title(path, fallback):
    """First '# ' heading of a doc (same convention as prompt.docs_overview)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh.read(4000).splitlines():
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return fallback


def _link_targets(line, docs_root):
    """Docs-root-relative paths of every markdown link target on this line."""
    targets = []
    for match in _LINK_RE.finditer(line):
        target = match.group(2).strip()
        if not target or target.startswith(_SKIP_PREFIXES):
            continue
        target = target.split("#", 1)[0].split()[0]
        if not target:
            continue
        rel = os.path.relpath(os.path.normpath(os.path.join(docs_root, target)),
                              docs_root)
        targets.append(rel.replace(os.sep, "/"))
    return targets


def reconcile_navigation(docs_root):
    """Guarantee navigation coverage of the hub: add a link entry for every
    functions/ or design/ doc not linked anywhere in PROJECT.md (entry text =
    the doc's first heading), drop entries whose target doc no longer exists.
    Returns a one-line change summary, '' when the hub is already complete."""
    lines = _read_lines(os.path.join(docs_root, HUB))
    if lines is None:
        return ""

    linked = {t for line in lines for t in _link_targets(line, docs_root)}
    missing = {}
    for directory in sorted(DOCS_TOP_DIRS):
        names = []
        dpath = os.path.join(docs_root, directory)
        if os.path.isdir(dpath):
            for name in sorted(os.listdir(dpath)):
                if not name.startswith(".") and name.lower().endswith(".md"):
                    names.append(name)
        missing[directory] = ["%s/%s" % (directory, name) for name in names
                              if "%s/%s" % (directory, name) not in linked]
    # map every line to the navigation section (docs directory) it sits in;
    # any heading or a --- separator closes the current section
    section_of = []
    current = None
    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading:
            text = heading.group(1).lower()
            current = None
            for key, directory in _NAV_SECTIONS:
                if key in text:
                    current = directory
                    break
        elif current is not None and _HR_RE.match(line):
            current = None
        section_of.append(current)

    out = []
    insert_at = {}
    added = dropped = 0
    for line, directory in zip(lines, section_of):
        if directory is None:
            out.append(line)
            continue
        if missing[directory] and _PLACEHOLDER_RE.match(line):
            continue  # first real entries replace the template placeholder
        own = [t for t in _link_targets(line, docs_root)
               if t.startswith(directory + "/")]
        if own and all(not os.path.isfile(os.path.join(docs_root, t))
                       for t in own):
            dropped += 1
            continue
        out.append(line)
        if _HEADING_RE.match(line):
            insert_at.setdefault(directory, len(out))
        elif line.strip():
            insert_at[directory] = len(out)

    unresolved = [d for d, docs in missing.items()
                  if docs and d not in insert_at]
    orphan_left = sum(len(missing[d]) for d in unresolved)
    blocks = []
    for directory, docs in missing.items():
        if not docs or directory not in insert_at:
            continue
        block = []
        for t in docs:
            name = t.split("/", 1)[1]
            title = _doc_title(os.path.join(docs_root, t),
                               os.path.splitext(name)[0])
            block.append("- [%s](%s)" % (title, t))
        blocks.append((insert_at[directory], block))
        added += len(block)
    # insert highest anchor first so earlier anchors keep their indices
    for index, block in sorted(blocks, key=lambda b: b[0], reverse=True):
        out[index:index] = block

    if not (added or dropped):
        if orphan_left:
            return ("navigation: %d orphan doc(s) left - no matching section "
                    "heading to anchor entries" % orphan_left)
        return ""
    with open(os.path.join(docs_root, HUB), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return "navigation: %+d link(s), -%d dead link(s)%s" % (
        added, dropped,
        (" (%d orphan doc(s) left - no matching section heading)" % orphan_left)
        if orphan_left else "")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="vibe-agent-hub",
        description="Maintain PROJECT.md: Sync Status (baseline / last synced) "
                    "and full navigation coverage.")
    parser.add_argument("--docs-root", required=True)
    parser.add_argument("--baseline", default="", help="last processed commit sha")
    parser.add_argument("--label", default="", help="short sha + commit subject")
    parser.add_argument("--date", default="", help="YYYY-MM-DD")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    docs_root = os.path.realpath(args.docs_root)
    nav = reconcile_navigation(docs_root)
    changed = update_sync_status(docs_root, args.baseline, args.label, args.date)
    print("hub: %s, %s" % ("sync: updated" if changed
                           else "sync: no change (markers not found)",
                           nav or "navigation: complete"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
