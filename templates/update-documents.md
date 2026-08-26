# How to Update Project Documentation

## Purpose

This document is the **generic, language-agnostic methodology** for maintaining the
hierarchical project documentation database. It is the same in every project you apply
the incremental documentation pipeline to.

Everything that is **specific to one project** (language, runtime, path conventions,
branch name, area tags, framework idioms) lives in a separate file:

→ [`./project-conventions.md`](./project-conventions.md)

When you document, follow this methodology **and** the project conventions together. If
the conventions file is missing, infer defaults from the source tree and flag
uncertainties in the doc footer.

The documentation lives in `agent/project/` and maps the capabilities of the project
under review.

> **The docs folder layout is fixed.** The docs root contains *exactly*:
> `update-documents.md`, `project-conventions.md`, `PROJECT.md`, `functions/`, and
> `design/` — nothing else. Do not create any other files or subfolders, do not nest a
> copy of the docs root inside itself (no `agent/` or `project/` subfolder), and do not
> mirror the source tree's directory names (no `<source-root>/functions/...`).
> Inside `functions/` and `design/` two coexisting forms are allowed: **flat**
> (`<number>-<name>.md`) and **module-grouped** (`<module>.md` index plus a
> `<module>/` directory holding that module's numbered docs) — see *Module layout*
> below.

> **Path resolution:** the locations below are written relative to the *repository
> root* (`agent/project/...`) for human readers. When the pipeline's agent writes docs
> via its `write_doc` tool, paths resolve against the **DOCS ROOT itself** — pass
> `functions/03-foo.md`, **not** `agent/project/functions/03-foo.md`. The tool
> enforces the fixed layout and auto-strips accidental `agent/project/` prefixes.

> Two modes of operation:
> - **Incremental (per-commit)** — driven by the pipeline's `run.sh`, which replays
>   the project commit-by-commit (stateful replay) and invokes the built-in
>   `vibe-agent` CLI for each. See *Incremental mode* below.
> - **Snapshot bootstrap** — `run.sh --snapshot` skips the history entirely: a
>   planner request partitions the current tree into modules, one agent session
>   per module writes that module's initial docs, and the baseline jumps to the
>   snapshot commit (later runs replay only newer commits). Recommended for
>   histories of thousands+ commits.
> - **Manual** — a human or agent edits docs directly following the same rules.

## Core Goal

Build a **navigable map** of the project: a reader can find, by reading a short
description, the source code that implements a user-facing function or capability, and
understand how the pieces fit together.

This means:
- **Document functions and architecture**, not bug history, not formatting, not every
  refactor. The map must stay small and high-signal.
- Every documented function **traces to source files** with concrete paths.
- The map is **built up incrementally** as capabilities appear in the commit history.

---

## Documentation Structure (3 levels)

### Level 1 — `PROJECT.md`
**Location:** `agent/project/PROJECT.md`
**Purpose:** High-level overview and navigation hub.
**Contents:** project description; repository/module map; links to all function docs;
links to all design docs; key entry-point source references.
**Update when:** a new module/package/area is introduced; project direction changes; a
new function/design doc is added (to keep navigation complete).

### Level 2 — Function Documentation
**Location:** `agent/project/functions/*.md` (flat) or `agent/project/functions/<module>/*.md` (module-grouped)
**Naming:** `<number>-<function-name>.md` (e.g. `01-cli.md`, `08-tools.md`). Numbers keep
the index stable; reuse the lowest free number for new files. Each module directory
carries its own numbering from 01.
**Purpose:** Describe each **user-facing capability / function** — *what* it does.
**Granularity:** one doc per capability CLUSTER, not per function symbol — related
functions share a doc; only standalone marquee features get their own.
**Template:**
```markdown
# <Function Name> Function

## Description
Brief description of what this function does from the user's perspective.

## Key Features
- Feature 1
- Feature 2

## Related Documentation
### Technical Details
- [Related Design Document](../design/<number>-<name>.md) - Design overview
### Source Files
- src/path/to/file.ext - Main implementation
- src/path/to/other.ext - Supporting code
### Related Functions
- [Related Function](./<number>-<name>.md) - Connection description

## Implementation Notes
Brief implementation details relevant for developers.

---
*Last updated: YYYY-MM-DD*
*Areas: <area>*
```
**Update when:** a new function is implemented; function behavior changes; source files
for this function are added/removed.

### Level 3 — Design Documentation
**Location:** `agent/project/design/*.md`
**Naming:** `<number>-<topic>.md` (e.g. `01-architecture.md`, `02-session-runtime.md`).
**Purpose:** Technical design — *how* and *why* — with source references.
**Template:**
```markdown
# <Topic> Design

## Overview
Brief overview of the design area.

## Architecture / Components
### Component Name
**File:** src/path/to/file.ext
**Purpose:** What this component does
**Features:**
- Feature 1
**API / Interface:**
```<lang>
// code example / interface shape — copied VERBATIM from the source file
```

## Design Decisions
Key decisions and rationale.

## Source Files
### Category
- src/path/to/file.ext - Description

---
*Last updated: YYYY-MM-DD*
*Areas: <area>*
```
**Update when:** architecture changes; new components; design patterns change; source
organization changes.

> **Source path style:** cite source files as paths **relative to the repository root**
> — exactly as they appear in the commit worktree (e.g. `src/...`, `cmd/.../main.go`,
> `testData/...`). The worktree root IS the repository root. Never prefix paths with the
> workspace/source-root folder name from `config.json` (`source_root` is where the clone
> lives in the workspace, not part of the repo); such paths do not resolve and fail
> validation. Every identifier and signature must be copied **verbatim** from a source
> file read in the current session — the repository at the commit under review is the
> only source of truth, not prior knowledge of the project (other versions, forks,
> upstream articles).

---

## Incremental Mode (per-commit, stateful replay)

This is the primary mode for large codebases. The pipeline checks out each commit into a
disposable git worktree and asks the agent: *“does this commit introduce or materially
change a user-facing function or a significant piece of architecture? If yes, update the
docs; if no, do nothing.”* The whole point is a **fresh, small context per commit**, so
nothing gets missed and the agent never has to swallow the entire codebase at once.

### Step 1 — Classify the commit
Look at the commit's subject, message body, and **diff** (`git -C <worktree> show <sha>`),
plus the state of the tree at that commit. Decide which bucket it falls into:

**DOCUMENT** (update docs) when the commit:
- adds a new user-facing function, command, screen/route, API endpoint, or capability;
- introduces a new module/package, service, or subsystem worth a design note;
- materially changes the behavior or interface of an existing documented function;
- adds a significant architectural pattern or cross-cutting mechanism.

**SKIP** (no doc change) when the commit is only:
- a bug fix (`fix:`) with no new capability;
- refactoring, renaming, formatting, lint, style, build/CI, dependency bumps;
- tests, docs-only changes, chores, typos, perf micro-tweaks;
- layout/UI polish that adds no function.

When in doubt, lean toward **SKIP** — the map must stay high-signal. A real new function
is almost always obvious from the diff.

> The runner pre-filters obvious noise by commit-message regex (`commit_skip_regex` in
> config), but the agent is the **final judge** and must still re-classify from the actual
> diff, because commit messages are unreliable.

### Step 2 — If DOCUMENT: update idempotently
1. **Check existing docs first.** Read `PROJECT.md` and the relevant `functions/` and
   `design/` files. Decide whether this is a **new** doc or an **update** to an existing
   one. Never duplicate an already-documented function.
2. **Create or update** the function doc and/or design doc per the templates, citing the
   source files **as they exist at this commit** in the worktree.
3. **Update `PROJECT.md` navigation** — add links for any new doc; refresh the module map
   if a new module/package appeared.
4. **Bump timestamps** (`*Last updated: YYYY-MM-DD*`) in every file you actually modify.
5. **Tag areas** (`*Areas: ...*`) per the conventions file.
6. Be **surgical**: touch only docs that correspond to real changes in this commit. Do not
   rewrite untouched areas. Do not fabricate.

### Step 3 — Emit a verdict
After deciding, end by calling the `finish` tool exactly once (the runner writes the
artifact files; the agent's verdict is recorded in `vibedocing/verdicts/<sha>.json`
plus a one-line `<sha>.txt` for the bash loop):
- `NO_DOC` — classified as skip, nothing written.
- `DOC_UPDATED <comma-separated relative doc paths>` — docs were created/changed.
In `classify-only` (dry-run) mode, decide and emit the verdict **without writing any docs**.

### Renames, moves and deletions (path hygiene)
The first user message always includes a rename-aware **NAME STATUS** (`git diff
--name-status -M`) and, when existing docs cite paths renamed/deleted by the commit, a
precomputed **STALE DOC REFERENCES** worklist. Then:
- A commit that renames/moves/deletes files cited in docs is **DOCUMENT** (path-fix
  pass), even when it otherwise looks like a refactor.
- Use the `search_docs` tool to find **every** doc mentioning the old path; replace
  with the new path (R) or remove/rewrite the reference (D). Also fix navigation
  links in `PROJECT.md` that point to renamed docs.
- Rationale: a documentation map that still points at pre-rename paths is worse than
  no map at all — the reader cannot find the code.

### Automated validation (quality gate)
After every `DOC_UPDATED` verdict the pipeline validates the docs map mechanically:
1. **layout** — only the fixed docs-root entries exist (`PROJECT.md`,
   `update-documents.md`, `project-conventions.md`, `functions/`, `design/`);
2. **naming** — `functions/` and `design/` files match `<number>-<name>.md`, numbers
   unique per directory (gaps are warned);
3. **links** — every relative markdown link resolves to an existing doc;
4. **paths** — cited repository paths (anything path-shaped with a slash and a file
   extension) exist in the worktree at this commit;
5. **stale refs** — no doc cites a path renamed/deleted by this commit;
6. **orphan nav** — every `functions/` and `design/` doc is linked from `PROJECT.md`
   (warning-level; the pipeline also re-adds missing navigation links itself
   after each processed commit, so coverage cannot drift for long);
7. **hub sections** — `PROJECT.md` keeps BOTH fixed navigation sections
   (`## Function Documentation` and `## Technical Design Documents`); never
   delete or rename a section heading — the pipeline re-creates a dropped
   section itself, and a missing section is a validation error.

Problems are fed back to the agent for up to `validation.rounds` repair rounds
(config). If deterministic errors remain in `strict` mode (default), the verdict
becomes `ERROR` and the commit is requeued — broken docs are never published. Audit
an existing docs map any time with `run.sh --validate [sha]`.

### Handling the "giant initial commit" (INITIAL SNAPSHOT mode)
Some projects begin with one massive commit containing the whole codebase. The
pipeline detects root commits (no parent) automatically and switches the agent to
**INITIAL SNAPSHOT** mode:
- the diffstat is replaced by a **TREE DIGEST** (real directories + file counts from
  `git ls-tree`), and the step budget is raised (`llm.max_steps_initial`);
- the agent must build the module/package map from the digest and the worktree —
  never from prior knowledge of the project (older versions, forks, upstream docs);
  every package, class and entry point it names must be verified in the worktree;
- coverage is allowed to be partial: document entry points, top-level architecture
  and the most prominent user-facing functions; later commits refine the map
  idempotently. A correct partial map beats an inventive complete one;
- to chunk a huge first pass further, set `scope` in config to a subdirectory
  (pathspec filter on which commits are processed) and run multiple passes.

---

## What counts as a "function" or "capability" (language-agnostic heuristics)

Because this pipeline is portable across languages, use these discovery heuristics to
decide what deserves a Level-2 function doc:

- **CLI commands / subcommands** — anything a user invokes (e.g. `foo build`, `foo serve`).
- **HTTP / RPC / GraphQL endpoints & route handlers.**
- **Public API surface** — exported entry points of a library/SDK.
- **UI screens, pages, routes, major components** (web/desktop/mobile).
- **Event handlers, background jobs, schedulers, queues, webhook receivers.**
- **Extension points** — plugins, hooks, middleware, MCP/tool integrations.
- **Config-gated features** — capabilities switched on by config/env.
- **Persistence / storage services** and their key operations.
- **Auth, permissions, and identity flows.**

A "function" is something a user or integrator would **name and look up**. An internal
helper class is not a function (it belongs in a design doc, if anywhere).

---

## Documentation Principles

1. **Developer-centric** — include full repository-root-relative source paths, short
   interface/API snippets copied verbatim from the source, and links to authoritative
   convention notes.
2. **Traceability** — every function links to source; every design links to
   implementation; every doc carries an `*Areas:*` tag and a timestamp.
3. **Ground truth** — the repository at the commit under review is the only source;
   no identifiers from memory, no facts from other versions or forks of the project.
4. **Hierarchy** — `PROJECT.md` navigates; function docs say *what*; design docs say
   *how/why*. Detail increases top → bottom.
5. **Maintainability** — consistent templates; one topic per file; bump timestamps on
   edit.
6. **High signal / low noise** — document capabilities and architecture, not history.
7. **Idempotency** — re-running over the same commits must refine, never duplicate.

## File Organization
```
<repository root>/
└── agent/project/                 # the docs root (DOCS ROOT for the pipeline agent)
    ├── update-documents.md        # this generic methodology
    ├── project-conventions.md     # per-project specifics (language, paths, branch, areas)
    ├── PROJECT.md                 # Level 1: navigation hub
    ├── functions/                 # Level 2: function docs (only *.md)
    │   ├── 01-cli.md              #   flat form
    │   ├── gpu.md                 #   module index (module-grouped form)
    │   └── gpu/                   #   the gpu module's numbered docs
    └── design/                    # Level 3: technical design docs (same two forms)
```

No other files or subfolders belong under the docs root (module directories under
`functions/` / `design/` are the one sanctioned nesting level).

## Module layout (optional, recommended for large projects)

When the workspace config sets `"modules": true` (bootstrap enables it automatically
for histories of 2000+ commits), new docs are grouped per module:

- `functions/<module>.md` is the module **index**: a short overview plus a
  `## Documents` section linking every doc of the module.
- The module's docs live in `functions/<module>/<number>-<name>.md`, numbered
  per module directory from 01.
- `PROJECT.md`'s navigation sections link **module indexes** (and any legacy flat
  docs), never individual module docs: hub → index → docs. The pipeline heals
  both tiers deterministically after every processed commit.
- Flat docs created before the switch stay valid; do not renumber them.

## Snapshot bootstrap mode

`run.sh --snapshot [REF]` documents the **current tree** at REF (default HEAD)
instead of replaying history: one planner request partitions the tree into
modules, then one agent session per module writes that module's initial docs
(module index + capability-cluster function docs + at most a couple of design
docs). The committed baseline then jumps to REF, and subsequent regular runs
document only newer commits. An interrupted snapshot resumes where it stopped
(finished modules are skipped on re-run).

## DO / DON'T
**DO:** update timestamps; link related docs; use repository-root-relative source paths;
keep docs in sync with code (including renames and deletions); tag areas; check existing
docs before creating new ones; copy signatures and option keys verbatim from the source.
**DON'T:** duplicate content across docs; put implementation detail in function docs;
leave broken links; cite files that no longer exist at the commit under review; prefix
paths with the workspace folder name; invent identifiers from prior knowledge of the
project; forget `PROJECT.md` navigation; use vague filenames; document bug fixes or
formatting; fabricate changes that aren't in the commit.

---
*Last updated: 2026-07-17*
*Document: update-documents.md (generic, portable)*
