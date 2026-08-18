# Project Conventions — @@PROJECT_NAME@@

> Per-project specifics for the documentation pipeline. The generic methodology lives in
> [`./update-documents.md`](./update-documents.md). **Edit this file** to match your
> project's stack — it is the main thing you customize per project. The `vibe-agent`
> agent reads both files on every run.

## Identity
- **Project name:** @@PROJECT_NAME@@
- **Source root** (matches `source_root` in the pipeline `config.json`): `@@SOURCE_ROOT@@`
  (a folder containing the project's git repository, gitignored by the docs workspace).
- **Default branch:** @@BRANCH@@
- **Detected language:** @@LANGUAGE@@
- **Layout:** @@LAYOUT@@

## Stack / language conventions
<!-- Fill these in. Examples below; replace with what's true for YOUR project. -->
- **Language:** @@LANGUAGE@@. Reference the appropriate source file extensions.
- **Runtime / framework:** (e.g. Node+Bun, JVM, .NET, Django, Rails, Go stdlib …)
- **Module / package shape:** (e.g. monorepo packages, single `src/`, Go modules …)
- **Authoritative rules:** link to the project's own contributing/AGENTS/architecture docs
  if they exist.

## Source path style (IMPORTANT)
- Cite source files as paths **relative to the repository root** — exactly as they
  appear in the commit worktree (e.g. `src/main/java/...`, `testData/foo.txt`,
  `build.gradle.kts`'s directory entries). The worktree root IS the repository root.
- `@@SOURCE_ROOT@@` is only the folder where the clone lives in the workspace — it is
  **not** part of the repository. Never write `@@SOURCE_ROOT@@/src/...` or
  `<workspace-folder>/src/...` in docs: such paths do not resolve and fail validation.

## Ground truth rules (IMPORTANT)
- The **only** source of truth is the repository content at the commit under review.
  Do not use prior knowledge about this project — other releases, older versions,
  forks, upstream articles or public documentation — for package names, class/method
  names, option keys, entry points or file paths.
- Every identifier (class, method, field, constant, option key) you mention must have
  been **read from the worktree in the current session**; copy signatures and option
  keys **verbatim** (copy-paste, never retype from memory).
- If you cannot verify something in the worktree, do not write it — omit it or mark
  it explicitly as unverified in the doc footer.

## Area tags
Documents are tagged `*Areas: ...*` by **package / subsystem**. Example:
`*Areas: @@PROJECT_NAME@@, <subsystem>*`.

## Commit-message conventions (used by the runner's skip filter)
<!-- If the project uses Conventional Commits, keep this. Otherwise describe its style. -->
- `feat(...)` / `add` / `implement` → almost always DOCUMENT.
- `fix|chore|style|test|docs|refactor|perf|build|ci|revert|bump` → SKIP unless a real new
  capability is visible in the diff.
- **Exception — path hygiene:** a rename/move/refactor commit that renames or deletes
  files cited in existing docs is always DOCUMENT (fix the paths/links), even though it
  looks like a skip.

## Notes for the documenting agent
- Cite source paths exactly as they appear at the commit being reviewed
  (repository-root-relative — see "Source path style" above).
- Include short interface/API snippets in design docs where helpful — copied verbatim
  from the source file you read.
- When files are renamed or deleted, use the `search_docs` tool to find every doc that
  still cites the old path and fix them all; remove references to deleted files.

---
*Last updated: @@DATE@@*
