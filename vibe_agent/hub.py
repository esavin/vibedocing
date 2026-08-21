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
- dropped navigation sections: an agent rewrite of the hub can delete a whole
  section heading (a real run lost "## Function Documentation" in the root
  commit and every later capability doc then landed in design/). The
  reconciliation now re-creates a missing section from the canonical template
  (## Function Documentation / ## Technical Design Documents), anchored above
  the Sync Status section, with either the orphaned links or the template
  "_(none yet)_" placeholder.

    python3 -m vibe_agent.hub --docs-root agent/project --baseline <sha> \
        --label "<short sha> (<subject>)" --date YYYY-MM-DD

Idempotent: rewrites the marker bullets in place; the navigation pass is a
no-op when the hub already covers every doc and keeps both sections. Section
headings are located by the "function" / "design" substrings (## Function
Documentation, ## Technical Design Documents).
"""

import argparse
import os
import re
import sys

from .validate import (_HEADING_RE, _LINK_RE, _SKIP_PREFIXES, DOCS_TOP_DIRS,
                       NAV_SECTION_KEYS)

HUB = "PROJECT.md"
_BASELINE_RE = re.compile(r"^(\s*-\s*\*\*[Bb]aseline commit:\*\*.*)$")
_SYNCED_RE = re.compile(r"^(\s*-\s*\*\*[Ll]ast synced:\*\*.*)$")
_SYNC_HEADING_RE = re.compile(r"^#{1,6}\s+sync\s+status\s*$", re.IGNORECASE)

# canonical section blocks, kept in sync with templates/PROJECT.md; used to
# re-create a navigation section the agent dropped from the hub entirely
_SECTION_HEADING = {
    "functions": "## Function Documentation",
    "design": "## Technical Design Documents",
}
_SECTION_COMMENT = {
    "functions": "<!-- Links to `functions/<number>-<name>.md` are added here "
                 "as functions are documented. -->",
    "design": "<!-- Links to `design/<number>-<name>.md` are added here as "
              "designs are documented. -->",
}
_SECTION_PLACEHOLDER = "_(none yet)_"
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
    the doc's first heading), drop entries whose target doc no longer exists,
    and re-create a navigation section whose heading was dropped entirely
    (anchored above Sync Status, holding the orphaned links or the template
    placeholder). Returns a one-line change summary, '' when the hub is
    already complete."""
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
            for key, directory in NAV_SECTION_KEYS:
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

    # a directory whose section heading is missing entirely gets a canonical
    # section re-created (template order: functions, then design), anchored
    # right above the Sync Status section so new docs always have an anchor
    created = []
    sync_anchor = len(out)
    for index, line in enumerate(out):
        if _SYNC_HEADING_RE.match(line):
            sync_anchor = index
            back = sync_anchor
            while back > 0 and not out[back - 1].strip():
                back -= 1
            if back > 0 and _HR_RE.match(out[back - 1]):
                sync_anchor = back - 1  # keep a new section above the --- rule
            break
    else:
        if out and out[-1].strip():
            out.append("")  # separate appended sections from the last line
            sync_anchor = len(out)
    for rank, (_key, directory) in enumerate(NAV_SECTION_KEYS):
        if directory in insert_at:
            continue  # section heading present - entries anchor inside it
        block = [_SECTION_HEADING[directory], _SECTION_COMMENT[directory]]
        for t in missing[directory]:
            name = t.split("/", 1)[1]
            title = _doc_title(os.path.join(docs_root, t),
                               os.path.splitext(name)[0])
            block.append("- [%s](%s)" % (title, t))
        if missing[directory]:
            added += len(missing[directory])
        else:
            block.append(_SECTION_PLACEHOLDER)
        block.append("")  # blank line before whatever follows the section
        created.append(directory)
        # +rank keeps template order when both sections are created at the
        # same anchor: blocks insert highest-first, so design lands above the
        # anchor first and functions ends up before it
        blocks.append((sync_anchor + rank, block))

    unresolved = [d for d, docs in missing.items()
                  if docs and d not in insert_at and d not in created]
    orphan_left = sum(len(missing[d]) for d in unresolved)
    # insert highest anchor first so earlier anchors keep their indices
    for index, block in sorted(blocks, key=lambda b: b[0], reverse=True):
        out[index:index] = block

    if not (added or dropped or created):
        if orphan_left:
            return ("navigation: %d orphan doc(s) left - no matching section "
                    "heading to anchor entries" % orphan_left)
        return ""
    with open(os.path.join(docs_root, HUB), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    summary = "navigation: %+d link(s), -%d dead link(s)" % (added, dropped)
    if created:
        summary += ", created section(s): %s" % "/".join(created)
    if orphan_left:
        summary += (" (%d orphan doc(s) left - no matching section heading)"
                    % orphan_left)
    return summary


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
