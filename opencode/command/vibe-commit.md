---
description: One step of the incremental documentation pipeline. Classifies a single commit (stateful replay) and updates project docs only if it introduces or materially changes a user-facing function or significant architecture. Invoked headlessly by vibedocing/run.sh.
---

# Document one commit (incremental pipeline)

You are one step of an automated, commit-by-commit documentation pipeline. You receive
exactly one commit, checked out in a disposable git worktree, and you must decide whether
it warrants a documentation change — then either make that change or do nothing.

## Inputs
- `$1` — the commit SHA under review.
- `$2` — absolute path to a git worktree that has **this commit checked out** (the source
  tree exactly as it was at this commit). Read source files from here.
- `$3` — optional mode token. If it equals `classify-only`, decide and emit the verdict
  **without writing any documentation**.

The documentation you may edit lives under `agent/project/` in the **project root** (your
current working directory), NOT in the worktree. The worktree is read-only context.

## Methodology (binding)
Follow these two files exactly:
- @agent/project/update-documents.md — the generic methodology and the 3-level structure.
- @agent/project/project-conventions.md — this project's specifics (language, paths,
  branch, area tags, commit-message conventions). If it is missing, infer conservatively.

## What to do

### 1. Inspect the commit
Run, in the worktree, to see what changed at this commit:
- `git -C "$2" show --stat "$1"` — file-level summary.
- `git -C "$2" log -1 --format='%H%n%an%n%ad%n%s%n%n%b' "$1"` — the full commit message.
- `git -C "$2" show "$1" -- <path>` — the actual diff for any path that looks significant.

Read the affected source files **as they exist at this commit** from the worktree when you
need detail. Do NOT read source from anywhere except the worktree (`$2`) — other copies are
at a different point in history.

### 2. Classify → DOCUMENT or SKIP
Apply the *Incremental mode → Step 1* rules from `update-documents.md`:

DOCUMENT when the commit adds a new user-facing function / command / route / screen /
endpoint / capability, introduces a new module or subsystem, or materially changes the
behavior or interface of an existing documented function.

SKIP when the commit is only a bug fix, refactor, rename, formatting, lint, build/CI,
dependency bump, tests, docs-only, chore, typo, or perf micro-tweak.

When in doubt, **SKIP**. The map must stay high-signal. Use the language-agnostic
"what counts as a function" heuristics in `update-documents.md`.

### 3a. If DOCUMENT (and not classify-only)
Update the docs **idempotently** per *Step 2* of the methodology:
1. Read `agent/project/PROJECT.md` and existing `agent/project/functions/*.md` and
   `agent/project/design/*.md` to decide NEW vs UPDATE. Never duplicate.
2. Create/update the function doc and/or design doc using the templates, citing source
   files as paths relative to the source root, exactly as they exist in the worktree.
3. Update `PROJECT.md` navigation for any new doc; refresh the module/package map if a new
   module appeared.
4. Bump `*Last updated: YYYY-MM-DD*` (today) in every file you modify.
5. Tag `*Areas: ...*` per `project-conventions.md`.
6. Be surgical: only docs corresponding to real changes in THIS commit. No fabrication.

### 3b. If SKIP
Make no documentation changes.

### 4. Emit the verdict (always, last)
Write exactly one line to `vibedocing/verdicts/$1.txt` (create the directory if
needed):
- If you changed docs: `VERDICT: DOC_UPDATED <comma-separated paths relative to repo root>`
  (list only the doc files you created/modified under `agent/project/`).
- If you skipped: `VERDICT: NO_DOC`
- In `classify-only` mode: emit the verdict you WOULD have produced, but make no doc edits.

Then print the same single line as your final message. Do not print anything else of
substance — the runner parses this line.

## Hard rules
- Only edit files under `agent/project/`. Never touch the worktree or any source code.
- Only read source from the worktree (`$2`). The repo-root source (if any) is at HEAD and
  will mislead you about this historical commit.
- Never `git add`, `git commit`, or `git push` anything.
- Never run the pipeline script yourself; you are a single step, not the loop.
- If `$2` is missing or `git -C "$2" show "$1"` fails, write
  `VERDICT: ERROR cannot-inspect-commit` and stop.
