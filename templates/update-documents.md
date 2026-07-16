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

The documentation lives in `agent/project/` and mirrors the real source tree of the
project under review.

> Two modes of operation:
> - **Incremental (per-commit)** — driven by `agent/vibedocing/run.sh`, which replays
>   the project commit-by-commit (stateful replay) and invokes the `vibe-commit` command
>   for each. See *Incremental mode* below.
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
**Location:** `agent/project/functions/*.md`
**Naming:** `<number>-<function-name>.md` (e.g. `01-cli.md`, `08-tools.md`). Numbers keep
the index stable; reuse the lowest free number for new files.
**Purpose:** Describe each **user-facing capability / function** — *what* it does.
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
- <source-root-relative>/path/to/file.ext - Main implementation
- <source-root-relative>/path/to/other.ext - Supporting code
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
**File:** <source-root-relative>/path/to/file.ext
**Purpose:** What this component does
**Features:**
- Feature 1
**API / Interface:**
```<lang>
// code example / interface shape
```

## Design Decisions
Key decisions and rationale.

## Source Files
### Category
- <source-root-relative>/path/to/file.ext - Description

---
*Last updated: YYYY-MM-DD*
*Areas: <area>*
```
**Update when:** architecture changes; new components; design patterns change; source
organization changes.

> **`<source-root-relative>`** means a path relative to the project's source root as
> configured in `agent/vibedocing/config.json` (`source_root`). The conventions file
> states the canonical prefix to use (e.g. `packages/<pkg>/src/...` or `src/...`).

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
After deciding, write exactly one line to the verdict file the runner gave you
(`agent/vibedocing/verdicts/<sha>.txt`):
- `VERDICT: NO_DOC` — classified as skip, nothing written.
- `VERDICT: DOC_UPDATED <comma-separated relative doc paths>` — docs were created/changed.
In `classify-only` (dry-run) mode, decide and emit the verdict **without writing any docs**.

### Handling the "giant initial commit"
Some projects begin with one massive commit containing the whole codebase. At that commit
the agent may need to document many things at once. Strategies (apply as needed):
- Focus on **entry points and top-level modules** for that commit; let later commits fill
  in detail (idempotency means later passes refine, not duplicate).
- If the diff is overwhelming, document the **module map** and the most prominent
  user-facing functions; mark the rest with a TODO footer for refinement.
- The pipeline can be **scoped** to a subdirectory via config (`scope`) to chunk a huge
  initial pass.

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

1. **Developer-centric** — include full source paths (relative to source root), short
   interface/API snippets, and links to authoritative convention notes.
2. **Traceability** — every function links to source; every design links to
   implementation; every doc carries an `*Areas:*` tag and a timestamp.
3. **Hierarchy** — `PROJECT.md` navigates; function docs say *what*; design docs say
   *how/why*. Detail increases top → bottom.
4. **Maintainability** — consistent templates; one topic per file; bump timestamps on
   edit.
5. **High signal / low noise** — document capabilities and architecture, not history.
6. **Idempotency** — re-running over the same commits must refine, never duplicate.

## File Organization
```
agent/project/
├── update-documents.md          # this generic methodology
├── project-conventions.md       # per-project specifics (language, paths, branch, areas)
├── PROJECT.md                   # Level 1: navigation hub
├── functions/                   # Level 2: function docs
└── design/                      # Level 3: technical design docs
```

## DO / DON'T
**DO:** update timestamps; link related docs; use full source-root-relative paths; keep
docs in sync with code; tag areas; check existing docs before creating new ones.
**DON'T:** duplicate content across docs; put implementation detail in function docs;
leave broken links; forget `PROJECT.md` navigation; use vague filenames; document bug
fixes or formatting; fabricate changes that aren't in the commit.

---
*Last updated: 2026-07-17*
*Document: update-documents.md (generic, portable)*
