---
name: vibe-document
description: Use when creating or updating the hierarchical project documentation under agent/project/ (PROJECT.md navigation hub, functions/*.md user-facing capabilities, design/*.md technical design). Covers both incremental per-commit documentation driven by agent/vibedocing/run.sh and ad-hoc/manual documentation requests. Loads the generic methodology and per-project conventions.
---

# Skill: Document the project

You maintain the project's navigable documentation **map** under `agent/project/`.

## Binding references (read these first)
- Methodology (generic, portable): `agent/project/update-documents.md`
- This project's specifics: `agent/project/project-conventions.md`
- Navigation hub you keep in sync: `agent/project/PROJECT.md`

## What "the map" is
A 3-level hierarchy:
1. `PROJECT.md` — overview + links to everything.
2. `functions/<number>-<name>.md` — one per **user-facing capability**: *what* it does,
   with source-file references.
3. `design/<number>-<name>.md` — technical design: *how/why*, with source references.

The goal: a reader finds, by description, the code that implements a function, and
understands how the pieces fit. **High signal, low noise.**

## Decision rule: document vs skip
Document **capabilities and architecture**, not history. Skip bug fixes, refactors,
formatting, build/CI, tests, dependency bumps, chores. When in doubt, skip — the map stays
small and useful. Use the language-agnostic "what counts as a function" heuristics in the
methodology (CLI commands, HTTP routes, public API, UI screens, jobs/queues, extension
points, config-gated features, persistence services, auth flows).

## When invoked incrementally (per-commit)
You are one step of `agent/vibedocing/run.sh`. A commit is checked out in a worktree.
Inspect `git -C <worktree> show <sha>`, classify DOCUMENT/SKIP, update docs idempotently
(never duplicate an existing function doc), and emit the verdict line to
`agent/vibedocing/verdicts/<sha>.txt` exactly as the `vibe-commit` command specifies.

## Always
- Cite full source-root-relative paths (prefix per `project-conventions.md`).
- Bump `*Last updated: YYYY-MM-DD*` and tag `*Areas: ...*` in every file you touch.
- Keep `PROJECT.md` navigation complete when docs are added/removed.
- Be surgical and idempotent.
