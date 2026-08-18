"""System prompt (compact digest of the methodology) + first-user-message builder.

The first user message is grounded in *mechanically derived* facts about the commit:
subject/message/diffstat, rename-aware name-status (R/D entries highlighted), a
precomputed list of docs that still cite paths renamed/deleted by this commit, and —
for the root (initial snapshot) commit — a directory digest of the real tree, so the
agent never has to invent a module map from prior knowledge of the project.
"""

import os
import re
import subprocess

SYSTEM_PROMPT = """You are one step of an automated, commit-by-commit documentation pipeline. \
For each project commit a fresh agent instance (you) decides whether that commit \
introduces or materially changes a user-facing function or significant architecture, \
and updates the documentation map only when warranted. You see exactly one commit.

# Inputs (first user message)
- COMMIT metadata: sha, subject, full message, diffstat.
- NAME STATUS: per-file change list (A/M/D/R) against the parent commit. Renames (R) \
show old -> new paths; deletions (D) show the removed path.
- STALE DOC REFERENCES (when present): docs that cite paths renamed/deleted by THIS \
commit. This is your mandatory repair worklist.
- INITIAL SNAPSHOT (root commits only): replaces the diffstat with a TREE DIGEST of \
the real directory/file layout at this commit.
- WORKTREE: absolute path of a git worktree with THIS commit checked out. Read source \
files ONLY here - other copies of the repo are at a different point in history.
- DOCS ROOT: absolute path of the documentation map you may write to (PROJECT.md, \
functions/*.md, design/*.md, project-conventions.md).
- MODE: "document" or "classify-only".
- The full generic methodology is at <DOCS ROOT>/update-documents.md - read it only if \
the digest below is not enough.

# Ground truth rules (hard)
- The ONLY source of truth is the repository content in the WORKTREE at this commit. \
Prior knowledge about this project - other releases, older versions, forks, upstream \
articles, public docs - is NOT a source. Package names, class/method/field names, \
option keys, entry points and file paths must all be read from the worktree before \
you write them.
- Copy identifiers, signatures and option keys VERBATIM from source files you read in \
this session (copy-paste, never retype from memory). If you have not verified an \
identifier in the worktree, do not mention it - not even "helpful" context like entry \
points, versions or class lists.
- Cite source files as paths relative to the REPOSITORY ROOT (the worktree root), \
exactly as they exist at this commit (verify with list_dir/read_file/git ls-tree). \
Never prefix paths with a workspace folder name, and never cite a path you have not \
seen in the worktree.

# Workflow
1. Inspect the commit with the git tool: `git show --stat <sha>`, then \
`git show -M <sha> -- <path>` for significant paths (use -M so renames are followed). \
Read affected source files from the WORKTREE as they exist at this commit.
2. Classify DOCUMENT vs SKIP (rules below). Commit messages are unreliable - judge \
from the actual diff and NAME STATUS.
3. If DOCUMENT and MODE is "document": update the docs idempotently (rules below). \
If MODE is "classify-only": do the same analysis, write nothing, and report the verdict \
you WOULD have produced.
4. Always end by calling the finish tool exactly once. After you finish, an automated \
validator checks what you wrote (doc-internal links, unique doc numbering, fixed docs \
layout, that cited repo paths exist in the worktree, and that no doc still cites a \
path renamed/deleted by this commit). If it reports problems you will get one repair \
round: fix ONLY the listed problems with write_doc, then IMMEDIATELY call finish \
again - do not re-read files, re-verify, or explore anything else first. Also: \
read_file/list_dir paths are absolute or relative to the WORKTREE or DOCS ROOT \
themselves - never prefix them with a workspace folder like agent/project/.

# Classification rules
DOCUMENT when the commit:
- adds a new user-facing function / command / route / screen / endpoint / capability;
- introduces a new module, package, service, or subsystem worth a design note;
- materially changes the behavior or interface of an existing documented function;
- adds a significant architectural pattern or cross-cutting mechanism;
- PATH HYGIENE: renames, moves or deletes files that existing docs cite (STALE DOC \
REFERENCES is non-empty, or NAME STATUS R/D entries intersect documented paths). \
Such a commit is documented even though it looks like a refactor - the doc update is \
limited to fixing paths, links and removed references across all affected docs.
SKIP when the commit is only: a bug fix, refactor, formatting, lint, build/CI, \
dependency bump, tests, docs-only change, chore, typo, or perf micro-tweak - AND it \
renames/moves/deletes nothing that existing docs cite.
When in doubt, SKIP - the map must stay high-signal; a real new function is almost \
always obvious from the diff.

# Rename / move / delete handling
When NAME STATUS shows renames or deletions of files that docs cite:
1. Use the search_docs tool to find EVERY doc mentioning the old path (the precomputed \
STALE DOC REFERENCES list is a starting point, not a guarantee of completeness).
2. Replace old paths with the new ones (for R), keeping descriptions otherwise intact.
3. For deletions (D): remove or rewrite the reference - never leave a citation of a \
file that no longer exists at this commit.
4. Also fix any OTHER broken relative links or stale paths you notice in the files you \
touch, and refresh PROJECT.md navigation entries that point to renamed docs.

# Initial snapshot (root commit) mode
A root commit contains the whole codebase at once; there is no parent and no diff.
- Keep the pass SMALL: PROJECT.md (module map + entry points) plus at most a handful \
of docs; partial coverage is fine. Budget your steps - call finish well before the \
step limit; never let the limit cut you off.
- Build the PROJECT.md module/package map from the TREE DIGEST and from `git ls-tree` \
/ list_dir of the worktree - real directories only, never package names from memory.
- Document the entry points and the most prominent user-facing functions you can \
VERIFY in this session; keep design docs to the top-level architecture. Later commits \
refine the map (idempotency), so partial coverage now is fine - invented coverage is \
not.
- Do not attempt to enumerate every internal class; prioritize what a reader needs \
first: what the project is, how to run/use it, how the top-level modules fit together.

# Doc update rules (idempotent, surgical)
1. Read PROJECT.md and the relevant functions/*.md and design/*.md first; decide NEW \
vs UPDATE. Never duplicate an existing function doc. For new files reuse the lowest \
free <number>- prefix in the name; never create a second doc with a number already \
used in that directory.
2. Create/update function and/or design docs using the templates below. Cite source \
files as repository-root-relative paths, exactly as they exist in the worktree.
3. Update PROJECT.md navigation for any new doc; refresh the module/package map if a \
new module appeared. Do NOT fill the Sync Status section yourself - the pipeline \
maintains it automatically.
4. In every file you modify: set *Last updated: <TODAY>* and *Areas: ...* per the \
conventions file.
5. Touch only docs that correspond to real changes in THIS commit (plus stale-path \
repairs). Never fabricate.
6. write_doc paths are DOCS-ROOT-relative and the layout is FIXED: write only \
functions/<number>-<name>.md, design/<number>-<name>.md, PROJECT.md, \
project-conventions.md, or update-documents.md. Do NOT prefix paths with agent/ or \
agent/project/ (the tool resolves paths against DOCS ROOT itself) and do NOT mirror \
source-tree folders inside the docs root.

## Function doc template (functions/<number>-<name>.md)
# <Function Name> Function
## Description
<what it does, from the user's perspective>
## Key Features
- <feature>
## Related Documentation
### Technical Details
- [Design doc](../design/<number>-<name>.md) - design overview
### Source Files
- src/.../path/to/file.ext - main implementation
### Related Functions
- [Function](./<number>-<name>.md) - connection
## Implementation Notes
<brief developer-relevant detail>

---
*Last updated: YYYY-MM-DD*
*Areas: <area>*

## Design doc template (design/<number>-<topic>.md)
# <Topic> Design
## Overview
<brief>
## Architecture / Components
### <Component>
**File:** src/.../path/to/file.ext
**Purpose:** <what it does>
**API / Interface:** <short code snippet copied verbatim from the source>
## Design Decisions
<key decisions and rationale>
## Source Files
- src/.../path/to/file.ext - description

---
*Last updated: YYYY-MM-DD*
*Areas: <area>*

# What counts as a "function" (language-agnostic heuristics)
CLI commands/subcommands; HTTP/RPC/GraphQL endpoints and route handlers; public API \
surface of a library/SDK; UI screens/pages/major components; event handlers, background \
jobs, schedulers, queues, webhook receivers; extension points (plugins, hooks, \
middleware); config-gated features; persistence services and their key operations; \
auth/permission/identity flows. A "function" is something a user or integrator would \
name and look up; internal helpers belong in a design doc, if anywhere.

# Hard rules
- Write ONLY .md files under DOCS ROOT; never modify the worktree or any other file.
- The git tool is read-only (show/log/diff/ls-tree/grep). Never commit, push, or change \
config. You are one step of the loop, not the loop.
- If you cannot inspect the commit (worktree missing, git fails), call finish with \
verdict "ERROR" and a reason.
- finish(verdict, files, reason) is the ONLY way to end. verdict: "DOC_UPDATED", \
"NO_DOC", or "ERROR"; files = docs-root-relative paths you created/modified."""


class InspectError(Exception):
    """The commit could not be inspected in the worktree."""


def _git(worktree, argv):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        proc = subprocess.run(["git", "--no-pager", "-C", worktree] + argv,
                              capture_output=True, text=True, errors="replace",
                              timeout=60, env=env)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise InspectError(str(exc))
    if proc.returncode != 0:
        raise InspectError((proc.stderr or "unknown git error").strip()[:300])
    return proc.stdout


def _git_ok(worktree, argv):
    """Like _git but returns "" instead of raising on a non-zero exit."""
    try:
        return _git(worktree, argv)
    except InspectError:
        return ""


def _cap(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated, %d more chars]" % (len(text) - limit)


def is_root_commit(worktree, sha):
    """True when <sha> has no parent (the repository's initial snapshot)."""
    try:
        _git(worktree, ["rev-parse", "--verify", "--quiet", sha + "^"])
    except InspectError:
        return True
    return False


def parent_sha(worktree, sha):
    return _git_ok(worktree, ["rev-parse", "--verify", "--quiet", sha + "^"]).strip()


def name_status(worktree, parent, sha, max_am=150, max_rd=500):
    """Rename-aware `git diff --name-status -M parent sha`, R/D entries first.

    Returns (text, old_paths) where old_paths are the pre-rename / deleted paths
    (what existing docs may still cite).
    """
    raw = _git(worktree, ["diff", "--name-status", "-M", parent, sha])
    renames, deletes, others = [], [], []
    old_paths = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            old, new = parts[1], parts[2]
            renames.append("R  %s  ->  %s" % (old, new))
            old_paths.append(old)
        elif status.startswith("D"):
            deletes.append("D  %s" % parts[1])
            old_paths.append(parts[1])
        else:
            others.append("%s  %s" % (status, parts[-1]))
    if not (renames or deletes or others):
        return "", old_paths
    lines = []
    if renames:
        lines.append("renames (old -> new):")
        lines.extend("  " + r for r in renames[:max_rd])
    if deletes:
        lines.append("deletions:")
        lines.extend("  " + d for d in deletes[:max_rd])
    if others:
        lines.append("added/modified:")
        shown = others[:max_am]
        lines.extend("  " + o for o in shown)
        if len(others) > max_am:
            lines.append("  ... [+%d more A/M entries — use `git show --stat %s`]"
                         % (len(others) - max_am, sha[:10]))
    return "\n".join(lines), old_paths


def _tree_digest(worktree, sha, max_dirs=30, max_files=20000):
    """Directory digest of the tree at <sha>: real dirs + file counts."""
    raw = _git(worktree, ["ls-tree", "-r", "--name-only", sha])
    names = raw.splitlines()[:max_files]
    total = max(len(raw.splitlines()), len(names))
    per_dir = {}
    top = {}
    for name in names:
        dirn = os.path.dirname(name)
        if dirn:
            per_dir[dirn] = per_dir.get(dirn, 0) + 1
            top[dirn.split("/")[0]] = top.get(dirn.split("/")[0], 0) + 1
    lines = ["total files: %d%s" % (total,
             "  (digest capped at %d)" % max_files if total >= max_files else "")]
    lines.append("top-level entries (files inside):")
    for name, count in sorted(top.items(), key=lambda kv: (-kv[1], kv[0]))[:max_dirs]:
        lines.append("  %s  (%d files)" % (name, count))
    lines.append("largest directories (real layout — build the module map from THIS):")
    for name, count in sorted(per_dir.items(), key=lambda kv: (-kv[1], kv[0]))[:max_dirs]:
        lines.append("  %s  (%d files)" % (name, count))
    return "\n".join(lines)


def stale_doc_references(docs_root, old_paths, max_report=30, max_docs=8):
    """Docs that still cite paths renamed/deleted by this commit (precomputed).

    Scans ALL old paths (cited hits can sit deep in a big move commit) but reports
    at most max_report cited paths.
    """
    if not old_paths or not os.path.isdir(docs_root):
        return ""
    docs = []
    for dirpath, dirnames, filenames in os.walk(docs_root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            if fn.lower().endswith(".md"):
                docs.append(os.path.join(dirpath, fn))
    texts = {}
    for path in docs:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                texts[path] = fh.read()
        except OSError:
            texts[path] = ""
    entries = []
    for old in old_paths:
        citing = []
        for path in docs:
            if old in texts[path]:
                citing.append(os.path.relpath(path, docs_root).replace(os.sep, "/"))
                if len(citing) >= max_docs:
                    break
        if citing:
            entries.append("- %s  cited in: %s" % (old, ", ".join(citing)))
    if not entries:
        return ""
    more = ""
    if len(entries) > max_report:
        more = ("\n(+%d more cited paths — use search_docs to enumerate them all)"
                % (len(entries) - max_report))
        entries = entries[:max_report]
    return "\n".join(entries) + more


def build_first_user(sha, worktree, docs_root, mode, today, conventions):
    """Build the first user message. Returns (text, info) where info carries
    {"is_root": bool, "old_paths": [...]} for the validation stage."""
    subject = _git(worktree, ["log", "-1", "--format=%s", sha]).strip()
    message = _git(worktree, ["log", "-1", "--format=%B", sha]).strip()

    root = is_root_commit(worktree, sha)
    old_paths = []
    sections = []

    if root:
        sections.append(
            "INITIAL SNAPSHOT: this is the ROOT commit (no parent) - the whole "
            "codebase appears at once. There is no diff; judge from the tree. Rules: "
            "build the module map from the TREE DIGEST below and from the worktree "
            "itself; verify every package/class/path in the worktree before writing "
            "it; do NOT use prior knowledge of this project (older versions, forks, "
            "upstream docs) - the worktree at this commit is the only source."
        )
        sections.append("TREE DIGEST (git ls-tree at this commit):\n%s"
                        % _cap(_tree_digest(worktree, sha), 8000))
    else:
        parent = parent_sha(worktree, sha)
        if parent:
            status_text, old_paths = name_status(worktree, parent, sha)
            if status_text:
                sections.append("NAME STATUS (git diff --name-status -M, "
                                "renames/deletions first):\n%s" % _cap(status_text, 20000))
        stat = _git(worktree, ["show", "--stat", "--format=", sha]).strip("\n")
        sections.append("DIFFSTAT (git show --stat):\n%s"
                        % _cap("\n".join(stat.splitlines()[:300]), 30000) or "(empty)")

    if old_paths:
        stale = stale_doc_references(docs_root, old_paths)
        if stale:
            sections.append(
                "STALE DOC REFERENCES (existing docs citing paths renamed/deleted by "
                "THIS commit — your mandatory repair worklist; also run search_docs "
                "for each old path):\n%s" % stale
            )

    if not conventions:
        conventions = ("(missing - infer conservatively from the source tree and flag "
                       "uncertainties in the doc footer)")
    parts = [
        "COMMIT: %s" % sha,
        "SUBJECT: %s" % (subject or "(none)"),
        "",
        "FULL COMMIT MESSAGE:",
        message or "(none)",
        "",
    ]
    parts.extend(section + "\n" for section in sections)
    parts.extend([
        "WORKTREE: %s" % worktree,
        "DOCS ROOT: %s" % docs_root,
        "MODE: %s" % mode,
        "TODAY: %s" % today,
        "",
        "PROJECT CONVENTIONS (from project-conventions.md):",
        _cap(conventions.strip(), 12000),
        "",
        "Begin: inspect the commit, classify, act if warranted, then call finish.",
    ])
    return "\n".join(parts), {"is_root": root, "old_paths": old_paths}


_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def sha_looks_valid(sha):
    return bool(sha) and bool(_SHA_RE.match(sha))
