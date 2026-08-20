"""The agent's toolset: six tools, hard-guarded in code (not in prompts).

Guards enforced here regardless of what the model asks for:
  - git         -> read-only subcommand allowlist, run with a scrubbed environment
                   (no GIT_DIR/GIT_WORK_TREE injection), no pager, timeout, capped output
  - read_file   -> only inside the worktree or the docs root, capped output, no binaries
  - list_dir    -> only inside the worktree or the docs root, capped entries
  - search_docs -> read-only regex search across the docs root (finds stale paths/links)
  - write_doc   -> only *.md under the docs root, and only in the fixed docs layout
                   (PROJECT.md, update-documents.md, project-conventions.md,
                   functions/*.md, design/*.md); accidental docs-root prefixes such
                   as 'agent/project/functions/x.md' are auto-stripped to
                   'functions/x.md'; refused in classify-only mode
  - finish      -> the only way to end the loop; verdict validated

Every execute() result is a JSON-serializable dict with an "ok" flag, so a
refused call becomes feedback the model can recover from.
"""

import json
import os
import re
import shlex
import subprocess

from .validate import _NUMBER_RE, _path_candidates

GIT_SUBCOMMANDS = {"show", "log", "diff", "ls-tree", "grep"}
GIT_FORBIDDEN_EXACT = {"-c", "--output", "--ext-diff", "--textconv",
                       "--open-files-in-pager"}
GIT_FORBIDDEN_PREFIXES = ("--output=", "-O", "--git-dir", "--work-tree")
GIT_TIMEOUT_SECONDS = 60
MAX_GIT_CHARS = 150_000
MAX_READ_CHARS = 80_000
MAX_LINE_CHARS = 2_000
MAX_LIST_ENTRIES = 3_000
MAX_LIST_CHARS = 40_000
MAX_SEARCH_MATCHES = 200
VERDICTS = {"DOC_UPDATED", "NO_DOC", "ERROR"}

# Fixed docs layout: the docs root may contain only these top-level files and
# exactly two subdirectories. This is enforced in code because models kept
# re-prepending the docs-root prefix ('agent/project/functions/x.md') or
# mirroring source-tree directories ('<source-root>/functions/x.md'), which
# produced nested duplicate documentation trees.
DOCS_TOP_FILES = {"project.md", "update-documents.md", "project-conventions.md"}
DOCS_TOP_DIRS = {"functions", "design"}
LAYOUT_ERROR = (
    "refused: the docs layout is fixed. Write only 'functions/<number>-<name>.md', "
    "'design/<number>-<name>.md', 'PROJECT.md', 'project-conventions.md', or "
    "'update-documents.md' - paths are relative to the docs root itself "
    "(no 'agent/project/' prefix, no nested or source-mirroring folders). Got: '%s'"
)
NUMBERING_HINT = ("<number> is always TWO digits with a hyphen: 01, 02, ... 10, 11 - "
                  "e.g. functions/01-cli.md")


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated, %d more chars]" % (len(text) - limit)


class ToolSet(object):
    def __init__(self, worktree, docs_root, classify_only=False, limits=None):
        self.worktree = os.path.realpath(str(worktree))
        self.docs_root = os.path.realpath(str(docs_root))
        self.classify_only = classify_only
        self.read_roots = [self.worktree, self.docs_root]
        # Output caps (config `limits` section; module constants are the
        # defaults, so a missing section reproduces the historical sizes).
        lim = limits if isinstance(limits, dict) else {}
        self.git_output_chars = max(200, int(lim.get("git_output_chars") or MAX_GIT_CHARS))
        self.read_file_chars = max(200, int(lim.get("read_file_chars") or MAX_READ_CHARS))
        self.list_dir_chars = max(200, int(lim.get("list_dir_chars") or MAX_LIST_CHARS))
        self.finish_result = None
        self.wrote_docs = False  # any successful write_doc call this session
        self.written_files = []  # docs-root-relative paths written/deleted
        # Workspace-relative prefixes of the docs root ('agent/project',
        # 'project', ...): models keep trying read_file('agent/project/x.md')
        # although paths must be docs-root-relative. write_doc already strips
        # these; read resolution must too, so the mistake stops costing steps.
        parts = self.docs_root.replace(os.sep, "/").rstrip("/").split("/")
        self._docs_prefixes = ["/".join(parts[-i:]) for i in (1, 2, 3)
                               if len(parts) >= i]

    # -- schema -------------------------------------------------------------

    def definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "git",
                    "description": (
                        "Run a read-only git subcommand (show, log, diff, ls-tree, grep) "
                        "in the commit worktree. Pass only the subcommand and its options; "
                        "'-C <worktree>' is added for you. "
                        'Example: {"args": ["show", "--stat", "<sha>"]}'
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "args": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": 'e.g. ["show", "<sha>", "--", "src/main.go"]',
                            }
                        },
                        "required": ["args"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": (
                        "Read a text file from the commit worktree or the docs root. "
                        "Use an absolute path, or a path relative to one of those roots. "
                        "Returns numbered lines."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "offset": {"type": "integer", "minimum": 1,
                                       "description": "1-indexed first line"},
                            "limit": {"type": "integer", "minimum": 1},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_dir",
                    "description": (
                        "List a directory in the commit worktree or the docs root. "
                        "Directories get a trailing '/'. Skips .git."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "recursive": {"type": "boolean"},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_docs",
                    "description": (
                        "Search all documentation files under the docs root with a "
                        "regex; returns 'file:line: text' matches. Use it to find "
                        "every doc that mentions a renamed/deleted source path, an "
                        "old doc filename, or any identifier before fixing references."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "regular expression, e.g. "
                                               "'src/old/pkg/|02-console-decompiler'",
                            },
                            "path_filter": {
                                "type": "string",
                                "description": "optional substring the doc's "
                                               "docs-root-relative path must contain",
                            },
                        },
                        "required": ["pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_doc",
                    "description": (
                        "Create or overwrite a documentation markdown file, "
                        "append a part to one (append: true - for docs too "
                        "long for a single call), or delete one (delete: true "
                        "- use it ONLY to remove a duplicate/obsolete doc "
                        "after merging its content into another doc). The path "
                        "is docs-root-relative (or absolute inside the docs "
                        "root) and must follow the fixed layout: "
                        "functions/<number>-<name>.md, "
                        "design/<number>-<name>.md, PROJECT.md, "
                        "project-conventions.md, or update-documents.md "
                        "(<number> = two digits: 01, 02, ... - unpadded names "
                        "are normalized automatically). A NEW "
                        "numbered doc MUST take the lowest free number in ITS "
                        "directory (checked at write time - the error names "
                        "the exact expected path); never continue another "
                        "directory's numbering. Never "
                        "prefix with agent/ or agent/project/ and never mirror "
                        "source-tree folders. Keep each call's content under "
                        "~150 lines: if a write gets cut off by the output "
                        "token limit, write the first half now and append the "
                        "rest with follow-up append calls. The result carries "
                        "a warning listing cited repository paths missing from "
                        "the worktree - fix them before finishing."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                            "append": {"type": "boolean",
                                       "description": "append content to the "
                                                      "existing doc instead "
                                                      "of overwriting"},
                            "delete": {"type": "boolean",
                                       "description": "delete the doc instead of "
                                                      "writing it"},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "finish",
                    "description": (
                        "End this step. The ONLY way to finish. verdict: DOC_UPDATED if you "
                        "created/modified docs (files = their docs-root-relative paths), "
                        "NO_DOC if nothing warranted a change, ERROR if you could not "
                        "inspect the commit."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "verdict": {"type": "string",
                                        "enum": ["DOC_UPDATED", "NO_DOC", "ERROR"]},
                            "files": {"type": "array", "items": {"type": "string"}},
                            "reason": {"type": "string"},
                        },
                        "required": ["verdict"],
                    },
                },
            },
        ]

    # -- dispatch -----------------------------------------------------------

    def execute(self, name, args):
        if not isinstance(args, dict):
            return {"ok": False, "error": "tool arguments must be a JSON object"}
        if name == "finish":
            return self._tool_finish(args)
        handler = {
            "git": self._tool_git,
            "read_file": self._tool_read_file,
            "list_dir": self._tool_list_dir,
            "search_docs": self._tool_search_docs,
            "write_doc": self._tool_write_doc,
        }.get(name)
        if handler is None:
            return {"ok": False, "error": "unknown tool: %s" % name}
        try:
            return handler(args)
        except Exception as exc:  # noqa: BLE001 - guards must never crash the loop
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    # -- path containment ---------------------------------------------------

    def _resolve_read(self, raw):
        """Resolve a read path inside worktree/docs_root, or None.

        Prefers candidates that actually exist (so a bare relative name finds a
        docs-root file even though the worktree is searched first). Relative
        paths carrying a docs-root prefix ('agent/project/functions/x.md') are
        retried with that prefix stripped, mirroring write_doc's normalization.
        """
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096:
            return None
        raw = raw.strip()
        if os.path.isabs(raw):
            candidates = [raw]
        else:
            candidates = [os.path.join(root, raw) for root in self.read_roots]
            stripped = self._strip_docs_prefix(raw)
            if stripped is not None:
                candidates.append(os.path.join(self.docs_root, stripped))
        contained = []
        for candidate in candidates:
            real = os.path.realpath(candidate)
            if any(real == root or real.startswith(root + os.sep)
                   for root in self.read_roots):
                contained.append(real)
        for real in contained:
            if os.path.exists(real):
                return real
        return contained[0] if contained else None

    def _strip_docs_prefix(self, rel):
        """'agent/project/functions/x.md' -> 'functions/x.md' (or None)."""
        parts = [p for p in rel.replace(os.sep, "/").split("/") if p]
        for prefix in sorted(self._docs_prefixes, key=len, reverse=True):
            plen = len(prefix.split("/"))
            if len(parts) > plen and "/".join(parts[:plen]) == prefix:
                return "/".join(parts[plen:])
        return None

    # -- tools --------------------------------------------------------------

    def _tool_git(self, args):
        raw = args.get("args")
        if isinstance(raw, str):
            argv = shlex.split(raw)
        elif isinstance(raw, list):
            argv = [str(item) for item in raw]
        else:
            argv = []
        argv = [item for item in argv if item != ""]
        if not argv:
            return {"ok": False,
                    "error": "args must be a non-empty array starting with a git subcommand"}
        subcommand = argv[0]
        if subcommand not in GIT_SUBCOMMANDS:
            return {"ok": False,
                    "error": "git subcommand '%s' not allowed; allowed: %s"
                             % (subcommand, ", ".join(sorted(GIT_SUBCOMMANDS)))}
        for item in argv[1:]:
            if item in GIT_FORBIDDEN_EXACT or item.startswith(GIT_FORBIDDEN_PREFIXES):
                return {"ok": False, "error": "git argument '%s' is not allowed" % item}
        # Scrub GIT_* env (GIT_DIR etc. could redirect to another repository).
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        try:
            proc = subprocess.run(
                ["git", "--no-pager", "-C", self.worktree] + argv,
                capture_output=True, text=True, errors="replace",
                timeout=GIT_TIMEOUT_SECONDS, env=env,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "git timed out after %ds" % GIT_TIMEOUT_SECONDS}
        output = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output": _truncate(output.rstrip(), self.git_output_chars),
        }

    def _tool_read_file(self, args):
        real = self._resolve_read(args.get("path"))
        if real is None:
            return {"ok": False,
                    "error": "path not found under the worktree or docs root"}
        if not os.path.isfile(real):
            return {"ok": False, "error": "not a file: %s (use list_dir)" % real}
        with open(real, "rb") as fh:
            head = fh.read(8192)
        if b"\x00" in head:
            return {"ok": False, "error": "binary file (contains NUL bytes)"}
        if head:
            try:
                ratio = head.decode("utf-8", "replace").count("\ufffd") / len(head.decode("utf-8", "replace"))
            except ZeroDivisionError:
                ratio = 0
            if ratio > 0.10:
                return {"ok": False, "error": "binary or non-UTF-8 file"}
        try:
            offset = int(args.get("offset") or 1)
            limit = min(int(args.get("limit") or 1200), 5000)
        except (TypeError, ValueError):
            return {"ok": False, "error": "offset/limit must be integers"}
        offset = max(1, offset)
        limit = max(1, limit)
        with open(real, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        selected = lines[offset - 1: offset - 1 + limit]
        numbered = []
        total = 0
        for index, line in enumerate(selected, start=offset):
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + "... [line truncated]"
            numbered.append("%d: %s" % (index, line))
            total += len(numbered[-1]) + 1
            if total > self.read_file_chars:
                numbered.append("... [output truncated at %d chars]" % self.read_file_chars)
                break
        return {
            "ok": True,
            "path": real,
            "total_lines": len(lines),
            "offset": offset,
            "count": len(selected),
            "more": offset - 1 + len(selected) < len(lines),
            "content": "\n".join(numbered),
        }

    def _tool_list_dir(self, args):
        real = self._resolve_read(args.get("path"))
        if real is None:
            return {"ok": False,
                    "error": "path not found under the worktree or docs root"}
        if not os.path.isdir(real):
            return {"ok": False, "error": "not a directory: %s" % real}
        recursive = bool(args.get("recursive", False))
        entries = []
        if recursive:
            current = None
            for current, dirs, files in os.walk(real):
                dirs[:] = sorted(d for d in dirs if d != ".git")
                rel = os.path.relpath(current, real)
                prefix = "" if rel == "." else rel.replace(os.sep, "/") + "/"
                for name in sorted(files):
                    entries.append(prefix + name)
                if len(entries) >= MAX_LIST_ENTRIES:
                    entries = entries[:MAX_LIST_ENTRIES]
                    entries.append("... [entry cap reached]")
                    break
        else:
            with os.scandir(real) as scan:
                items = sorted(scan, key=lambda e: (not e.is_dir(), e.name.lower()))
            for entry in items:
                if entry.name == ".git":
                    continue
                if entry.is_dir():
                    entries.append(entry.name + "/")
                else:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    entries.append("%s (%d bytes)" % (entry.name, size))
                if len(entries) >= MAX_LIST_ENTRIES:
                    entries.append("... [entry cap reached]")
                    break
        return {"ok": True, "path": real, "recursive": recursive,
                "entries": _truncate("\n".join(entries), self.list_dir_chars)}

    def _tool_search_docs(self, args):
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return {"ok": False, "error": "pattern must be a non-empty string"}
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return {"ok": False, "error": "invalid regex: %s" % exc}
        path_filter = str(args.get("path_filter") or "")
        matches = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(self.docs_root):
            dirnames[:] = sorted(d for d in dirnames if d != ".git")
            for name in sorted(filenames):
                if not name.lower().endswith(".md"):
                    continue
                absolute = os.path.join(dirpath, name)
                rel = os.path.relpath(absolute, self.docs_root).replace(os.sep, "/")
                if path_filter and path_filter not in rel:
                    continue
                try:
                    with open(absolute, "r", encoding="utf-8",
                              errors="replace") as fh:
                        lines = fh.read().splitlines()
                except OSError:
                    continue
                for number, line in enumerate(lines, start=1):
                    if rx.search(line):
                        matches.append("%s:%d: %s"
                                       % (rel, number, line.strip()[:MAX_LINE_CHARS]))
                        if len(matches) >= MAX_SEARCH_MATCHES:
                            truncated = True
                            break
                if truncated:
                    break
            if truncated:
                break
        result = {"ok": True, "pattern": pattern,
                  "matches": _truncate("\n".join(matches), self.list_dir_chars),
                  "count": len(matches)}
        if truncated:
            result["note"] = ("match cap reached; narrow the pattern or use "
                              "path_filter to see the rest")
        return result

    # -- docs layout --------------------------------------------------------

    @staticmethod
    def _fix_docs_layout(rel, docs_root):
        """Normalize a docs-root-relative path to the fixed layout, or None.

        Strips accidental docs-root prefixes (a model told docs live in
        'agent/project/' may write 'agent/project/functions/x.md' or
        'project/functions/x.md') and rejects anything outside the documented
        three-level structure.
        """
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if not parts:
            return None
        root_names = {p.lower() for p in docs_root.replace(os.sep, "/").split("/")
                      if p}
        while (len(parts) > 1
               and parts[0].lower() not in DOCS_TOP_DIRS
               and parts[0].lower() not in DOCS_TOP_FILES
               and parts[0].lower() in root_names):
            parts.pop(0)
        if len(parts) == 1:
            return parts[0] if parts[0].lower() in DOCS_TOP_FILES else None
        if len(parts) == 2 and parts[0].lower() in DOCS_TOP_DIRS:
            return parts[0].lower() + "/" + parts[1]
        return None

    def _tool_write_doc(self, args):
        if self.classify_only:
            return {"ok": False,
                    "error": "classify-only mode: writes are disabled; call finish with"
                             " the verdict you would have produced"}
        raw = args.get("path")
        if not isinstance(raw, str) or not raw.strip():
            return {"ok": False, "error": "path must be a non-empty string"}
        raw = raw.strip()
        if os.path.isabs(raw):
            target = os.path.realpath(raw)
        else:
            target = os.path.realpath(os.path.join(self.docs_root, raw))
        if target != self.docs_root and not target.startswith(self.docs_root + os.sep):
            return {"ok": False,
                    "error": "refused: path escapes the docs root %s" % self.docs_root}
        if not target.lower().endswith(".md"):
            return {"ok": False, "error": "refused: only .md files can be written"}
        rel = os.path.relpath(target, self.docs_root).replace(os.sep, "/")
        fixed = self._fix_docs_layout(rel, self.docs_root)
        if fixed is None:
            extra = (" " + NUMBERING_HINT
                     if rel.split("/", 1)[0].lower() in DOCS_TOP_DIRS else "")
            return {"ok": False, "error": LAYOUT_ERROR % rel + extra}
        note = None
        if fixed != rel:
            note = "path normalized from '%s'" % rel
            target = os.path.join(self.docs_root, *fixed.split("/"))
            rel = fixed
        # canonical two-digit numbering, enforced at write time: an unpadded
        # single-digit name (functions/1-x.md) is retargeted to the padded
        # spelling (functions/01-x.md), and a pre-existing unpadded twin of
        # the same doc is replaced after the write - the map never keeps both.
        unpadded_twin = None
        if "/" in rel and args.get("delete") is not True:
            directory, _, fname = rel.partition("/")
            match = _NUMBER_RE.match(fname)
            if match:
                number = int(match.group(1))
                if number < 1:
                    return {"ok": False,
                            "error": "numbering: doc numbers start at 01"}
                rest = fname[match.end():]
                if len(match.group(1)) < 2:
                    orig_rel = rel
                    rel = "%s/%02d-%s" % (directory, number, rest)
                    unpadded_twin = target
                    target = os.path.join(self.docs_root, *rel.split("/"))
                    note = ("%s; numbering normalized from '%s'"
                            % (note, orig_rel)) if note else \
                           ("numbering normalized from '%s'" % orig_rel)
                # per-directory sequential numbering, enforced at write time
                # for NEW files: a weak model continues whichever counter it
                # saw last (design/ numbering leaked into functions/ as 01-09
                # then 22-25, leaving 10-21 free forever). Overwriting an
                # EXISTING path is always allowed.
                if not os.path.exists(target):
                    siblings = {}
                    dpath = os.path.join(self.docs_root, directory)
                    if os.path.isdir(dpath):
                        for name in sorted(os.listdir(dpath)):
                            if name.startswith(".") or not name.lower().endswith(".md"):
                                continue
                            m2 = _NUMBER_RE.match(name)
                            if m2:
                                siblings.setdefault(int(m2.group(1)), name)
                    lowest = 1
                    while lowest in siblings:
                        lowest += 1
                    if number in siblings:
                        if siblings[number] == "%d-%s" % (number, rest):
                            # the unpadded twin of THIS doc: the write below
                            # replaces it under the canonical padded name
                            unpadded_twin = os.path.join(dpath, siblings[number])
                        else:
                            return {"ok": False,
                                    "error": "numbering: number %02d is already "
                                             "taken by %s/%s - do NOT create a "
                                             "second doc with it; update THAT "
                                             "file instead (or merge and delete "
                                             "the redundant one)"
                                             % (number, directory,
                                                siblings[number])}
                    if number > lowest:
                        new_rel = "%s/%02d-%s" % (directory, lowest, rest)
                        return {"ok": False,
                                "error": "numbering: %s has free numbers below "
                                         "%02d - write this doc as '%s' instead "
                                         "(per-directory sequential numbering)"
                                         % (directory, number, new_rel)}
        if args.get("delete") is True:
            # deleting a doc is allowed only inside functions/ or design/ and
            # only for merging duplicates/obsoletes - never the fixed files
            if rel.lower() in DOCS_TOP_FILES:
                return {"ok": False,
                        "error": "refused: %s is a fixed top-level doc - it "
                                 "cannot be deleted, only rewritten" % rel}
            if not os.path.isfile(target):
                return {"ok": False,
                        "error": "nothing to delete: '%s' does not exist" % rel}
            os.remove(target)
            self.wrote_docs = True
            if rel not in self.written_files:
                self.written_files.append(rel)
            return {"ok": True, "deleted": rel}
        content = args.get("content")
        if not isinstance(content, str):
            return {"ok": False, "error": "content must be a string"}
        append = args.get("append") is True
        if append and not os.path.isfile(target):
            return {"ok": False,
                    "error": "append: '%s' does not exist yet - write it first "
                             "without append, then append the following parts"
                             % rel}
        parent = os.path.dirname(target)
        if parent != self.docs_root and not parent.startswith(self.docs_root + os.sep):
            return {"ok": False, "error": "refused: parent escapes the docs root"}
        os.makedirs(parent, exist_ok=True)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as fh:
            if append and content and not content.startswith("\n"):
                fh.write("\n")
            fh.write(content)
        self.wrote_docs = True
        if rel not in self.written_files:
            self.written_files.append(rel)
        result = {"ok": True, "written": len(content.encode("utf-8")),
                  "path": rel}
        if unpadded_twin and os.path.isfile(unpadded_twin) \
                and os.path.abspath(unpadded_twin) != os.path.abspath(target):
            # the canonical write above replaced this doc's unpadded spelling
            try:
                os.remove(unpadded_twin)
                result["replaced"] = os.path.relpath(
                    unpadded_twin, self.docs_root).replace(os.sep, "/")
            except OSError:
                pass
        if append:
            result["appended"] = True
        if note:
            result["note"] = note
        # write-time path lint (same rules as the post-finish validator): a
        # dead repository path costs a repair round if left to the validator -
        # telling the model NOW usually fixes it in the next step
        missing = []
        seen = set()
        for line in content.splitlines():
            for token in _path_candidates(line):
                if token in seen:
                    continue
                seen.add(token)
                if not os.path.exists(os.path.join(self.worktree, token)):
                    missing.append(token)
                    if len(missing) >= 10:
                        break
            if len(missing) >= 10:
                break
        if missing:
            result["warning"] = (
                "cited path(s) not found in the worktree at this commit "
                "(fix or remove BEFORE finishing - they WILL fail validation): %s"
                % ", ".join(missing))
        return result

    def _tool_finish(self, args):
        verdict = args.get("verdict")
        if verdict not in VERDICTS:
            return {"ok": False,
                    "error": "verdict must be one of %s" % ", ".join(sorted(VERDICTS))}
        files = args.get("files") or []
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            files = []
        reason = args.get("reason") or ""
        if not isinstance(reason, str):
            reason = str(reason)
        self.finish_result = {
            "verdict": verdict,
            "files": files,
            "reason": reason,
        }
        return {"ok": True, "finished": True}
