# vibedocing — incremental documentation pipeline (portable kit)

Generate a navigable documentation **map** for any git project, in any language, by
replaying its history commit-by-commit. For each commit, a purpose-built agent —
**vibe-agent**, ~500 lines of dependency-free Python speaking any OpenAI-compatible
API — in a fresh, small context decides whether it introduces or changes a
user-facing function or significant architecture, and updates the docs only when
warranted. Bug fixes, refactors, and formatting are skipped, so the map stays
high-signal.

The outer loop is a bash script (**outside** every agent call), so no single agent ever
holds the whole codebase in context. The agent itself is a minimal single-purpose loop
with six hard-guarded tools (read-only git, read/list, search-docs, write-doc, finish)
— no shell, no general-purpose system prompt — so nearly the whole context window is
spent on the actual commit.

## Quick start

```bash
# 1. make a working folder and clone this tooling into it
mkdir mywork && cd mywork
git clone https://github.com/esavin/vibedocing.git   # -> ./vibedocing/

# 2. clone the project you want to document, into the same folder
git clone <project-url> ./someproject

# 3. bootstrap (creates .gitignore, agent/project/, git repo, config)
./vibedocing/bootstrap.sh ./someproject

# 4. point the agent at your model (any OpenAI-compatible endpoint)
$EDITOR vibedocing/config.json        # llm.model, llm.base_url
export VIBE_API_KEY=...               # or whatever llm.api_key_env names

# 5. preview, then run
./vibedocing/run.sh --list | tail -1     # how many commits to process
./vibedocing/run.sh --limit 20           # document first 20 commits (auto-committed)
./vibedocing/run.sh                      # continue from baseline to HEAD
```

## What you get

```
mywork/                         <- workspace (its own git repo)
  .gitignore                    project folder + tooling are gitignored
  someproject/                  (gitignored) the project under analysis
  vibedocing/                 (gitignored) THIS solution (run.sh, bootstrap.sh, vibe_agent/, templates…)
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
2. `python3 -m vibe_agent` — fresh session — injects the commit metadata, a
   rename-aware name-status (renames/deletions first), a precomputed list of docs
   still citing paths renamed/deleted by this commit, and project conventions into a
   compact system+user prompt, then classifies DOCUMENT vs SKIP from the actual diff
   and the historical tree via six guarded tools (git/read_file/list_dir/search_docs/
   write_doc/finish). Root commits (giant initial snapshots) get a dedicated mode:
   a real tree digest instead of the diffstat and a bigger step budget.
3. If DOCUMENT: the agent edits `agent/project/` (function/design docs + PROJECT.md nav).
   Commits that rename/move/delete files cited in docs trigger a path-hygiene pass
   even when they look like refactors.
4. The docs are then **validated mechanically** (internal links, unique doc numbering,
   fixed layout, cited repo paths exist in the worktree, no stale renamed paths).
   Problems are fed back to the agent for repair rounds; in strict mode remaining
   errors block publication and the commit is requeued.
5. `run.sh` reads the agent's verdict (`vibedocing/verdicts/<sha>.json` + a one-line
   `.txt`), advances the committed baseline (`agent/project/.vibedocing.json`),
   auto-fills PROJECT.md's Sync Status, and **git-commits** the doc changes
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
--validate [SHA]     audit existing docs (links, numbering, paths) vs the tree at SHA
--limit N            process at most N commits
--range A..B         process a specific range
--sha S              process a single commit
--in-place           checkout in the source clone instead of a worktree
--no-commit          don't git-commit this run
--stop-on-fail       halt on the first failed commit
--model M            override the model (or export VIBE_MODEL)
```

See `GUIDE.md` for porting to a new project/language, performance tips, and troubleshooting.
Requires: `git`, `jq`, `python3` (>=3.8, stdlib only — no pip packages), and any
OpenAI-compatible LLM endpoint.
