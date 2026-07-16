# vibedocing — incremental documentation pipeline (portable kit)

Generate a navigable documentation **map** for any git project, in any language, by
replaying its history commit-by-commit. For each commit, an opencode agent — in a fresh,
small context — decides whether it introduces or changes a user-facing function or
significant architecture, and updates the docs only when warranted. Bug fixes, refactors,
and formatting are skipped, so the map stays high-signal.

The outer loop is a bash script (**outside** every agent call), so no single agent ever
holds the whole codebase in context.

## Quick start

```bash
# 1. make a working folder and clone this tooling into it
mkdir mywork && cd mywork
git clone https://github.com/esavin/vibedocing.git   # -> ./vibedocing/

# 2. clone the project you want to document, into the same folder
git clone <project-url> ./someproject

# 3. bootstrap (creates .gitignore, agent/project/, git repo, config)
./vibedocing/bootstrap.sh ./someproject

# 4. preview, then run
./vibedocing/run.sh --list | tail -1     # how many commits to process
./vibedocing/run.sh --limit 20           # document first 20 commits (auto-committed)
./vibedocing/run.sh                      # continue from baseline to HEAD
```

## What you get

```
mywork/                         <- workspace (its own git repo)
  .gitignore                    project folder + tooling are gitignored
  someproject/                  (gitignored) the project under analysis
  vibedocing/                 (gitignored) THIS solution (run.sh, bootstrap.sh, templates…)
  .opencode/                    (gitignored) installed command + skill
  agent/project/                COMMITTED — the documentation map
    PROJECT.md                  navigation hub + Sync Status
    update-documents.md         generic methodology
    project-conventions.md      per-project specifics (you edit this)
    .vibedocing.json              last-processed commit (for restart-after-sync)
    functions/*.md              Level 2: user-facing capabilities
    design/*.md                 Level 3: technical design
```

## How it works (per commit)

1. `run.sh` checks the commit out into a disposable git worktree (stateful replay).
2. `opencode run --command vibe-commit "<sha> <worktree>" --auto` — fresh session — classifies
   DOCUMENT vs SKIP from the actual diff and the historical tree.
3. If DOCUMENT: the agent edits `agent/project/` (function/design docs + PROJECT.md nav).
4. `run.sh` reads the agent's verdict, advances the committed baseline
   (`agent/project/.vibedocing.json`), and **git-commits** the doc changes
   (`docs(<project>): <subject>`).

## Restart after upstream changes

The last fully-processed commit is stored (committed) in `agent/project/.vibedocing.json`.
When the project gets new commits:

```bash
git -C someproject pull        # sync new changes
./vibedocing/run.sh          # documents only baseline..HEAD (the new commits)
```

`--reset-baseline` starts over from the project's first commit.

## Common options

```
--list               show PROCESS/SKIP/DONE decisions, no agent calls
--dry-run            classify only (no doc writes, no commits)
--limit N            process at most N commits
--range A..B         process a specific range
--sha S              process a single commit
--in-place           checkout in the source clone instead of a worktree
--no-commit          don't git-commit this run
--stop-on-fail       halt on the first failed commit
--attach URL         attach to a running 'opencode serve' (faster for big batches)
--serve              manage an 'opencode serve' for the run
--model M            override the model
```

See `GUIDE.md` for porting to a new project/language, performance tips, and troubleshooting.
Requires: `git`, `jq`, `opencode`.
