# Project Conventions — @@PROJECT_NAME@@

> Per-project specifics for the documentation pipeline. The generic methodology lives in
> [`./update-documents.md`](./update-documents.md). **Edit this file** to match your
> project's stack — it is the main thing you customize per project. The `vibe-commit` agent
> reads both files on every run.

## Identity
- **Project name:** @@PROJECT_NAME@@
- **Source root** (matches `vibedocing/config.json` → `source_root`): `@@SOURCE_ROOT@@`
  (a folder containing the project's git repository, gitignored by the docs workspace).
- **Default branch:** `@@BRANCH@@`
- **Detected language:** `@@LANGUAGE@@`
- **Layout:** @@LAYOUT@@

## Stack / language conventions
<!-- Fill these in. Examples below; replace with what's true for YOUR project. -->
- **Language:** @@LANGUAGE@@. Reference the appropriate source file extensions.
- **Runtime / framework:** (e.g. Node+Bun, JVM, .NET, Django, Rails, Go stdlib …)
- **Path prefix** to use in doc source references: `@@SOURCE_ROOT@@/...`
- **Module / package shape:** (e.g. monorepo packages, single `src/`, Go modules …)
- **Authoritative rules:** link to the project's own contributing/AGENTS/architecture docs
  if they exist.

## Area tags
Documents are tagged `*Areas: ...*` by **package / subsystem**. Example:
`*Areas: @@PROJECT_NAME@@, <subsystem>*`.

## Commit-message conventions (used by the runner's skip filter)
<!-- If the project uses Conventional Commits, keep this. Otherwise describe its style. -->
- `feat(...)` / `add` / `implement` → almost always DOCUMENT.
- `fix|chore|style|test|docs|refactor|perf|build|ci|revert|bump` → SKIP unless a real new
  capability is visible in the diff.

## Notes for the documenting agent
- Cite source paths exactly as they appear at the commit being reviewed, prefixed with
  `@@SOURCE_ROOT@@/`.
- Include short interface/API snippets in design docs where helpful.

---
*Last updated: @@DATE@@*
