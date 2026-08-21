"""Post-write documentation validation (all checks are mechanical and language-agnostic).

Rationale: an external audit of generated docs found broken internal links, duplicate
doc numbering, paths prefixed with a workspace folder that is not part of the
repository, references to files renamed/deleted later, identifiers written from
memory, and docs that silently lost their PROJECT.md navigation entry. All of these
are mechanically checkable, so the pipeline now validates after every doc-writing
commit and feeds the problems back to the agent for repair.

Checks:
 1. layout - the docs root contains only the fixed top-level entries
             (PROJECT.md, update-documents.md, project-conventions.md, functions/,
              design/, dotfiles like .vibedocing.json).
 2. naming - files in functions/ and design/ match <number>-<name>.md; numbers are
             unique per directory (duplicates are errors; numbering gaps are warnings).
 3. links  - every relative markdown link resolves to an existing file under the
             docs root (http(s)/mailto/anchor targets are skipped).
 4. paths  - repository-path-like references (contain "/" and a file extension, no
             placeholder markers) resolve inside the worktree = the repository at the
             commit being documented. Catches stale prefixes such as "<clone>/src/..."
             and citations of files that no longer exist.
 5. stale  - no doc still cites a path renamed/deleted by the commit under review
             (old paths come from the rename-aware name-status of this commit).
 6. orphans - every functions/ and design/ doc is linked from PROJECT.md (the
              reverse direction of the links check). Warnings only: the
              pipeline's hub reconciliation (vibe_agent/hub.py) re-adds missing
              links itself after each processed commit, so this is a drift
              signal, not a repair task for the agent.
 7. hub sections - PROJECT.md keeps BOTH fixed navigation sections (## Function
              Documentation, ## Technical Design Documents). A dropped section
              heading leaves every later doc of that level without an anchor
              (a real run lost the functions section at the root commit and
              all 255 documented commits then went to design/). Errors: the
              heading is trivial to restore in a repair round, and hub.py
              re-creates a missing section deterministically as a safety net.

Severities: errors are deterministic and block publication in strict mode; warnings
are heuristics recorded in the report. Usable standalone:

    python3 -m vibe_agent.validate --docs-root agent/project [--worktree <tree>]
            [--old-path p ...] [--path-check error|warn|off] [--report FILE]
"""

import argparse
import os
import re
import sys

DOCS_TOP_FILES = {"project.md", "update-documents.md", "project-conventions.md"}
DOCS_TOP_DIRS = {"functions", "design"}

_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
# navigation sections of PROJECT.md are located by a substring of their heading
# text - tolerates reasonable renames an agent might produce ("## Design Docs"
# still anchors the design section)
NAV_SECTION_KEYS = (("function", "functions"), ("design", "design"))

# at least one "/" and plain filename characters only
_PATH_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.@/\-])[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+")
_FILE_EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,4}$")
_NUMBER_RE = re.compile(r"^(\d{1,3})[-_]")
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(\s*<?([^)>]+?)>?\s*\)")
_SKIP_PREFIXES = ("http://", "https://", "mailto:", "ftp://", "#", "data:", "//")
_SKIP_TOKEN_CONTAINS = ("<", ">", "@@", "...", "*", "://")
_PLACEHOLDER_FIRST = {
    "path", "paths", "file", "files", "foo", "bar", "baz", "qux", "your", "some",
    "example", "examples", "name", "names", "placeholder", "of", "to",
}


def _iter_md_files(docs_root):
    if not os.path.isdir(docs_root):
        return
    for dirpath, dirnames, filenames in os.walk(docs_root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            if name.lower().endswith(".md"):
                yield os.path.join(dirpath, name)


def _rel(path, docs_root):
    return os.path.relpath(path, docs_root).replace(os.sep, "/")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def check_layout(docs_root, errors):
    allowed = DOCS_TOP_FILES | DOCS_TOP_DIRS
    for name in sorted(os.listdir(docs_root)):
        if name.startswith("."):
            continue  # .vibedocing.json and friends
        if name.lower() not in allowed:
            errors.append("layout: unexpected top-level entry '%s' (allowed: %s)"
                          % (name, ", ".join(sorted(allowed))))


def check_naming(docs_root, errors, warnings):
    for directory in sorted(DOCS_TOP_DIRS):
        path = os.path.join(docs_root, directory)
        if not os.path.isdir(path):
            continue
        numbers = {}
        count = 0
        for name in sorted(os.listdir(path)):
            if not name.lower().endswith(".md"):
                errors.append("naming: %s/%s is not a .md file" % (directory, name))
                continue
            if name.startswith("."):
                continue
            count += 1
            match = _NUMBER_RE.match(name)
            if not match:
                errors.append("naming: %s/%s lacks the <number>-<name>.md pattern"
                              % (directory, name))
                continue
            number = match.group(1)
            if len(number) < 2:
                errors.append(
                    "naming: %s/%s uses an unpadded number - the fixed layout "
                    "numbers docs with TWO digits. Re-save the doc as "
                    "'%s/%02d-%s' (a single write_doc to that path replaces "
                    "the unpadded file automatically)"
                    % (directory, name, directory, int(number),
                       name[match.end():]))
            numbers.setdefault(int(number), []).append(name)
        for number, names in sorted(numbers.items()):
            if len(names) > 1:
                errors.append(
                    "naming: duplicate number %02d in %s/: %s. Keep ONE of these "
                    "files (merge the content if both have value) and DELETE the "
                    "others with write_doc({\"path\": \"%s/<file>\", "
                    "\"delete\": true}). Do NOT create yet another numbered file "
                    "for this topic."
                    % (number, directory, ", ".join(names), directory))
        if numbers:
            highest = max(int(n) for n in numbers)
            if highest > len(numbers):
                warnings.append(
                    "naming: numbering gap in %s/ - highest number %03d but only %d "
                    "numbered docs; reuse the lowest free number for new docs"
                    % (directory, highest, len(numbers)))


def check_links(docs_root, errors):
    for path in _iter_md_files(docs_root):
        rel = _rel(path, docs_root)
        base = os.path.dirname(path)
        for match in _LINK_RE.finditer(_read(path)):
            target = match.group(2).strip()
            if not target or target.startswith(_SKIP_PREFIXES):
                continue
            target = target.split("#", 1)[0].strip()
            if not target or target.startswith(_SKIP_PREFIXES):
                continue
            resolved = os.path.normpath(os.path.join(base, target))
            if not os.path.exists(resolved):
                errors.append("%s: broken link '(%s)'" % (rel, target))


def _path_candidates(line):
    for match in _PATH_TOKEN_RE.finditer(line):
        token = match.group(0)
        if any(marker in token for marker in _SKIP_TOKEN_CONTAINS):
            continue
        if token.startswith("www.") or token.endswith(".md"):
            continue
        if not _FILE_EXT_RE.search(token):
            continue
        if token.split("/", 1)[0].lower() in _PLACEHOLDER_FIRST:
            continue
        yield token


def check_paths(docs_root, worktree, problems, severity):
    if not worktree or not os.path.isdir(worktree) or severity == "off":
        return
    for path in _iter_md_files(docs_root):
        rel = _rel(path, docs_root)
        seen = set()
        for line in _read(path).splitlines():
            for token in _path_candidates(line):
                if token in seen:
                    continue
                seen.add(token)
                if os.path.exists(os.path.join(worktree, token)):
                    continue
                problems.append(
                    "%s: path '%s' does not exist in the repository at this commit "
                    "(fix the prefix, update to the current path, or remove the "
                    "reference)" % (rel, token))


def check_stale(docs_root, old_paths, errors):
    if not old_paths:
        return
    for path in _iter_md_files(docs_root):
        rel = _rel(path, docs_root)
        text = _read(path)
        if not text:
            continue
        for old in old_paths:
            # boundary check: an old path EXTENDED with more filename
            # characters is a different, valid path - 'build.gradle' inside
            # 'build.gradle.kts' must NOT count as a stale citation
            if re.search(re.escape(old) + r"(?![\w.\-/])", text):
                errors.append("%s: still cites '%s' (renamed/deleted by this commit)"
                              % (rel, old))


def check_orphans(docs_root, warnings):
    """Reverse direction of check_links: every functions/ and design/ doc must
    be reachable from PROJECT.md, the navigation hub. The agent rewrites the
    hub per commit in a small context and old entries fall out (orphan drift);
    this reports the drift. Warnings only - vibe_agent/hub.py heals them."""
    hub = os.path.join(docs_root, "PROJECT.md")
    if not os.path.isfile(hub):
        return
    linked = set()
    for match in _LINK_RE.finditer(_read(hub)):
        target = match.group(2).strip()
        if not target or target.startswith(_SKIP_PREFIXES):
            continue
        target = target.split("#", 1)[0].split()[0]
        if not target:
            continue
        rel = os.path.relpath(os.path.normpath(os.path.join(docs_root, target)),
                              docs_root)
        linked.add(rel.replace(os.sep, "/"))
    for directory in sorted(DOCS_TOP_DIRS):
        path = os.path.join(docs_root, directory)
        if not os.path.isdir(path):
            continue
        for name in sorted(os.listdir(path)):
            if name.startswith(".") or not name.lower().endswith(".md"):
                continue
            if "%s/%s" % (directory, name) not in linked:
                warnings.append(
                    "%s/%s: not linked from PROJECT.md (navigation coverage; "
                    "the pipeline re-adds missing links itself after this "
                    "commit)" % (directory, name))


def check_hub_sections(docs_root, errors):
    """PROJECT.md must keep BOTH fixed navigation sections (functions + design).
    When the agent drops a section heading, every later doc of that level loses
    its anchor and the level silently migrates into the surviving section, so
    this is an error, not a warning."""
    hub = os.path.join(docs_root, "PROJECT.md")
    if not os.path.isfile(hub):
        return
    present = set()
    for line in _read(hub).splitlines():
        match = _HEADING_RE.match(line)
        if not match:
            continue
        text = match.group(1).lower()
        for key, directory in NAV_SECTION_KEYS:
            if key in text:
                present.add(directory)
    for _key, directory in NAV_SECTION_KEYS:
        if directory not in present:
            errors.append(
                "PROJECT.md: missing %s navigation section - restore its "
                "heading ('## Function Documentation' / '## Technical Design "
                "Documents'); docs in %s/ must be linked from there"
                % (directory, directory))


def validate_docs(docs_root, worktree=None, old_paths=(), path_check="error"):
    """Run all checks. Returns {"errors": [...], "warnings": [...]}."""
    errors, warnings = [], []
    check_layout(docs_root, errors)
    check_naming(docs_root, errors, warnings)
    check_links(docs_root, errors)
    path_problems = []
    check_paths(docs_root, worktree, path_problems, path_check)
    if path_check == "error":
        errors.extend(path_problems)
    elif path_check == "warn":
        warnings.extend(path_problems)
    check_stale(docs_root, old_paths, errors)
    check_orphans(docs_root, warnings)
    check_hub_sections(docs_root, errors)
    return {"errors": errors, "warnings": warnings}


def format_report(problems, sha=""):
    lines = ["# Validation report%s" % ((" for %s" % sha) if sha else ""), ""]
    lines.append("- errors: %d" % len(problems["errors"]))
    lines.append("- warnings: %d" % len(problems["warnings"]))
    lines.append("")
    if problems["errors"]:
        lines.append("## Errors (block publication in strict mode)")
        lines.extend("- %s" % item for item in problems["errors"])
        lines.append("")
    if problems["warnings"]:
        lines.append("## Warnings")
        lines.extend("- %s" % item for item in problems["warnings"])
        lines.append("")
    return "\n".join(lines)


def repair_message(problems, rounds_left):
    parts = [
        "VALIDATION FAILED - your docs have mechanical problems. Fix ALL of them now "
        "with write_doc (and search_docs to locate every occurrence), then call "
        "finish again with verdict DOC_UPDATED listing every file you modified "
        "(across all rounds).",
    ]
    if any("duplicate number" in item for item in problems["errors"]):
        parts.append(
            "DUPLICATE NUMBERING: two or more docs share a number. Merge their "
            "content into the single best file, then DELETE every redundant file "
            "with write_doc({\"path\": ..., \"delete\": true}). Creating yet "
            "another NEW numbered file for the topic is WRONG - it adds another "
            "duplicate.")
    if any("does not exist in the repository" in item for item in problems["errors"]):
        parts.append(
            "DEAD PATHS: a file was renamed or moved by this commit (see the "
            "grouped renames in NAME STATUS). Update each cited path to its NEW "
            "location in the worktree, or remove the citation - never keep a "
            "path that does not exist at this commit.")
    if problems["errors"]:
        parts.append("Errors (must fix):")
        parts.extend("- %s" % item for item in problems["errors"])
    if problems["warnings"]:
        parts.append("Warnings (verify and fix if genuine):")
        parts.extend("- %s" % item for item in problems["warnings"])
    parts.append("Repair rounds remaining after this one: %d. If a reported path is "
                 "intentionally not part of the repository, remove or rephrase the "
                 "reference instead of leaving it." % max(0, rounds_left - 1))
    return "\n".join(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="vibe-agent-validate",
        description="Validate the documentation map (links, numbering, layout, "
                    "source paths, stale references, hub coverage).")
    parser.add_argument("--docs-root", required=True)
    parser.add_argument("--worktree", default="",
                        help="worktree with the project at the commit being documented "
                             "(enables the source-path checks)")
    parser.add_argument("--old-path", action="append", default=[],
                        help="path renamed/deleted by the commit; docs citing it fail")
    parser.add_argument("--path-check", choices=["error", "warn", "off"],
                        default="error")
    parser.add_argument("--report", default="", help="also write the report to FILE")
    parser.add_argument("--sha", default="", help="commit for the report header")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    docs_root = os.path.realpath(args.docs_root)
    worktree = os.path.realpath(args.worktree) if args.worktree else None
    problems = validate_docs(docs_root, worktree, args.old_path, args.path_check)
    report = format_report(problems, args.sha)
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(report)
    print(report)
    return 1 if problems["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
