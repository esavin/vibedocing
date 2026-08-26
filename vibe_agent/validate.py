"""Post-write documentation validation (all checks are mechanical and language-agnostic).

Rationale: an external audit of generated docs found broken internal links, duplicate
doc numbering, paths prefixed with a workspace folder that is not part of the
repository, references to files renamed/deleted later, identifiers written from
memory, and docs that silently lost their PROJECT.md navigation entry. All of these
are mechanically checkable, so the pipeline now validates after every doc-writing
commit and feeds the problems back to the agent for repair.

Docs layout - two coexisting forms (a workspace may migrate from one to the other):
  flat     functions/<number>-<name>.md                  design/<number>-<name>.md
  modular  functions/<module>.md  (module INDEX)         design/<module>.md
           functions/<module>/<number>-<name>.md         design/<module>/<number>-<name>.md
The modular form scales to thousands of docs: PROJECT.md links module indexes,
each index links its module's docs. Numbering is per-directory (top level or
module dir), so two modules may both use 01-.

Checks:
  1. layout - the docs root contains only the fixed top-level entries
              (PROJECT.md, update-documents.md, project-conventions.md, functions/,
               design/, dotfiles like .vibedocing.json).
  2. naming - files in functions/ and design/ match <number>-<name>.md (or are a
               module index <module>.md with a matching <module>/ directory);
               numbers are unique per directory (duplicates are errors; numbering
               gaps are warnings).
  3. links  - every relative markdown link resolves to an existing file under the
              docs root (http(s)/mailto/anchor targets are skipped).
  4. paths  - repository-path-like references (contain "/" and a file extension, no
              placeholder markers) resolve inside the worktree = the repository at the
              commit being documented. Catches stale prefixes such as "<clone>/src/..."
              and citations of files that no longer exist.
  5. stale  - no doc still cites a path renamed/deleted by the commit under review
              (old paths come from the rename-aware name-status of this commit).
  6. orphans - every functions/ and design/ doc is reachable: flat docs and module
               indexes from PROJECT.md, module docs from their module index (or
               PROJECT.md). Warnings only: the pipeline's hub reconciliation
               (vibe_agent/hub.py) re-adds missing links itself after each
               processed commit, so this is a drift signal, not a repair task
               for the agent.
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

# module directory / index slug: lowercase, digits, dashes, underscores
MODULE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def parse_doc_path(rel):
    """Parse a docs-root-relative path under functions/ or design/.

    Returns None for anything else, else a dict:
      functions/01-x.md     -> {"top": "functions", "module": None,  "file": "01-x.md",
                                "index": False}
      functions/gpu.md      -> {"top": "functions", "module": "gpu", "file": "gpu.md",
                                "index": True}   (module INDEX file)
      functions/gpu/01-x.md -> {"top": "functions", "module": "gpu", "file": "01-x.md",
                                "index": False}  (doc inside module dir)
    """
    parts = [p for p in str(rel).replace("\\", "/").split("/")
             if p not in ("", ".")]
    if len(parts) == 2:
        top, name = parts[0].lower(), parts[1]
        if top not in DOCS_TOP_DIRS or not name.lower().endswith(".md"):
            return None
        if _NUMBER_RE.match(name):
            return {"top": top, "module": None, "file": name, "index": False}
        stem = name[:-3].lower()
        if stem and MODULE_SLUG_RE.match(stem):
            return {"top": top, "module": stem, "file": stem + ".md", "index": True}
        return None
    if len(parts) == 3:
        top, module, name = parts[0].lower(), parts[1].lower(), parts[2]
        if (top not in DOCS_TOP_DIRS or not MODULE_SLUG_RE.match(module)
                or not name.lower().endswith(".md") or not _NUMBER_RE.match(name)):
            return None
        return {"top": top, "module": module, "file": name, "index": False}
    return None


def docs_inventory(docs_root):
    """Structural inventory of the docs map (flat + modular forms).

    Returns {top: {"flat": [numbered file names], "modules": {slug: [doc names]}}}
    for both docs directories. Anything that does not parse (weird names, stray
    files) is simply absent here - check_naming reports it.
    """
    inv = {}
    for top in sorted(DOCS_TOP_DIRS):
        base = os.path.join(docs_root, top)
        entry = {"flat": [], "modules": {}}
        if os.path.isdir(base):
            for name in sorted(os.listdir(base)):
                if name.startswith("."):
                    continue
                path = os.path.join(base, name)
                if os.path.isdir(path):
                    if MODULE_SLUG_RE.match(name):
                        entry["modules"][name] = sorted(
                            fn for fn in os.listdir(path)
                            if fn.lower().endswith(".md")
                            and _NUMBER_RE.match(fn))
                elif name.lower().endswith(".md") and _NUMBER_RE.match(name):
                    entry["flat"].append(name)
        inv[top] = entry
    return inv

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


def _check_numbered_set(directory, names, errors, warnings):
    """Uniqueness/gap checks for one directory's <number>-<name>.md files."""
    numbers = {}
    for name in names:
        match = _NUMBER_RE.match(name)
        if not match:
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
    for number, dupes in sorted(numbers.items()):
        if len(dupes) > 1:
            errors.append(
                "naming: duplicate number %02d in %s/: %s. Keep ONE of these "
                "files (merge the content if both have value) and DELETE the "
                "others with write_doc({\"path\": \"%s/<file>\", "
                "\"delete\": true}). Do NOT create yet another numbered file "
                "for this topic."
                % (number, directory, ", ".join(dupes), directory))
    if numbers:
        highest = max(numbers)
        if highest > len(numbers):
            warnings.append(
                "naming: numbering gap in %s/ - highest number %03d but only %d "
                "numbered docs; reuse the lowest free number for new docs"
                % (directory, highest, len(numbers)))


def check_naming(docs_root, errors, warnings):
    for directory in sorted(DOCS_TOP_DIRS):
        path = os.path.join(docs_root, directory)
        if not os.path.isdir(path):
            continue
        flat = []
        module_dirs = []
        for name in sorted(os.listdir(path)):
            if name.startswith("."):
                continue
            full = os.path.join(path, name)
            if os.path.isdir(full):
                if MODULE_SLUG_RE.match(name):
                    module_dirs.append(name)
                else:
                    errors.append(
                        "naming: %s/%s is not a valid module directory name "
                        "(lowercase letters/digits/dashes/underscores)"
                        % (directory, name))
                continue
            if not name.lower().endswith(".md"):
                errors.append("naming: %s/%s is not a .md file" % (directory, name))
                continue
            match = _NUMBER_RE.match(name)
            if match:
                flat.append(name)
            elif MODULE_SLUG_RE.match(name[:-3].lower()) and len(name) > 3:
                # module index: needs its module directory (healed by hub.py)
                if not os.path.isdir(os.path.join(path, name[:-3].lower())):
                    warnings.append(
                        "naming: %s/%s looks like a module index but the "
                        "module directory %s/%s/ does not exist"
                        % (directory, name, directory, name[:-3].lower()))
            else:
                errors.append(
                    "naming: %s/%s is neither a <number>-<name>.md doc nor a "
                    "module index (<module>.md)" % (directory, name))
        _check_numbered_set(directory, flat, errors, warnings)
        for module in module_dirs:
            mpath = os.path.join(path, module)
            mdocs = []
            for name in sorted(os.listdir(mpath)):
                if name.startswith("."):
                    continue
                if not name.lower().endswith(".md") or not _NUMBER_RE.match(name):
                    errors.append(
                        "naming: %s/%s/%s must follow the <number>-<name>.md "
                        "pattern (module directories hold numbered docs only; "
                        "the module index lives at %s/%s.md)"
                        % (directory, module, name, directory, module))
                    continue
                mdocs.append(name)
            if not os.path.isfile(os.path.join(path, module + ".md")):
                warnings.append(
                    "naming: module directory %s/%s/ has no index file - "
                    "create %s/%s.md (the pipeline re-creates a skeleton "
                    "itself)" % (directory, module, directory, module))
            _check_numbered_set("%s/%s" % (directory, module), mdocs,
                                errors, warnings)


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


# fixed methodology files use example paths ("e.g. src/path/to/file.ext") by
# design - the source-path check targets agent-written capability docs
PATH_CHECK_EXEMPT = {"update-documents.md", "project-conventions.md"}


def check_paths(docs_root, worktree, problems, severity):
    if not worktree or not os.path.isdir(worktree) or severity == "off":
        return
    for path in _iter_md_files(docs_root):
        rel = _rel(path, docs_root)
        if rel in PATH_CHECK_EXEMPT:
            continue
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
    """Reverse direction of check_links: navigation coverage. A flat doc and a
    module INDEX must be reachable from PROJECT.md; a doc inside a module
    directory must be reachable from its module index (or PROJECT.md). The
    agent rewrites these files per commit in a small context and old entries
    fall out (orphan drift); this reports the drift. Warnings only -
    vibe_agent/hub.py heals them."""
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
    for directory, entry in docs_inventory(docs_root).items():
        for name in entry["flat"]:
            if "%s/%s" % (directory, name) not in linked:
                warnings.append(
                    "%s/%s: not linked from PROJECT.md (navigation coverage; "
                    "the pipeline re-adds missing links itself after this "
                    "commit)" % (directory, name))
        index_linked = {}
        for module, names in entry["modules"].items():
            ipath = os.path.join(docs_root, directory, module + ".md")
            targets = set()
            if os.path.isfile(ipath):
                for match in _LINK_RE.finditer(_read(ipath)):
                    target = match.group(2).strip()
                    if not target or target.startswith(_SKIP_PREFIXES):
                        continue
                    target = target.split("#", 1)[0].split()[0]
                    if not target:
                        continue
                    rel = os.path.relpath(os.path.normpath(
                        os.path.join(docs_root, directory, target)), docs_root)
                    targets.add(rel.replace(os.sep, "/"))
            index_linked[module] = targets
            if "%s/%s.md" % (directory, module) not in linked:
                warnings.append(
                    "%s/%s.md: module index not linked from PROJECT.md "
                    "(navigation coverage; the pipeline re-adds missing links "
                    "itself after this commit)" % (directory, module))
            for name in names:
                rel = "%s/%s/%s" % (directory, module, name)
                if rel not in linked and rel not in targets:
                    warnings.append(
                        "%s: not linked from its module index %s/%s.md (the "
                        "pipeline re-adds missing links itself after this "
                        "commit)" % (rel, directory, module))


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
        "with edit_doc (targeted text replacements) or write_doc (new docs only), "
        "and search_docs to locate every occurrence, then call "
        "finish again with verdict DOC_UPDATED listing every file you modified "
        "(across all rounds). Never rewrite a whole existing doc to fix a path - "
        "edit_doc is the right tool and a lossy rewrite is refused.",
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
            "grouped renames in NAME STATUS). With edit_doc, replace each cited "
            "old path with its NEW location in the worktree (find=old path, "
            "replace=new path), or remove the citation if the file is gone - "
            "never keep a path that does not exist at this commit.")
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
