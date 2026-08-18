# @@PROJECT_NAME@@ — Project Documentation

> @@PROJECT_NAME@@ — @@LANGUAGE@@ project. Auto-generated navigation hub for the
> incremental documentation map maintained by the vibedocing pipeline.

- **Source root:** `@@SOURCE_ROOT@@`
- **Default branch:** `@@BRANCH@@`

## Repository / Module Map

<!-- The pipeline fills this in as it walks commits. Seed the top-level modules here if you
     know them; otherwise let the agent populate it. -->

_To be populated._

## Function Documentation

<!-- Links to `functions/<number>-<name>.md` are added here as functions are documented. -->

_(none yet)_

## Technical Design Documents

<!-- Links to `design/<number>-<name>.md` are added here as designs are documented. -->

_(none yet)_

---

## Sync Status

> The single source of truth for incremental re-runs. The pipeline advances `baseline` to
> the last fully-processed project commit and **fills the fields below automatically**
> after every run — do not edit them by hand. After you sync new upstream changes into
> `@@SOURCE_ROOT@@`, re-running the pipeline documents only `baseline..HEAD`.

- **Project source:** `@@SOURCE_ROOT@@`
- **Branch:** `@@BRANCH@@`
- **Baseline commit:** _(filled automatically by the pipeline)_
- **Last synced:** _(filled automatically by the pipeline)_

*Last updated: @@DATE@@*
