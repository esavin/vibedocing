# Guide — porting, performance, troubleshooting

## Requirements
`git`, `jq`, `opencode` on PATH. Optional `timeout` (per-run limits).

## Porting to a new project / language
The pipeline is language-agnostic. Per-project specifics live in two files you edit:
- `agent/project/project-conventions.md` — stack, path prefix, area tags, commit style.
- `vibedocing/config.json` — `source_root`, `source_branch`, `commit_skip_regex`, …

`bootstrap.sh` auto-detects language/layout and fills these in as a starting point; correct
them if needed. Common `source_root` values:

| Project shape | source_root | path prefix in docs |
| --- | --- | --- |
| Project cloned inside workspace | `someproject` (its folder name) | `someproject/...` |
| Single repo (workspace IS the repo) | `.` | `src/...`, `cmd/...` |
| Project outside workspace | absolute path | `<that path>/...` |

## The "giant initial commit"
Some projects start with one massive commit. The agent documents the most prominent
entry points/modules there; later commits refine the map idempotently (never duplicate).
To chunk a huge first pass, set `scope` in config to a subdirectory and run multiple passes.

## Performance / cost
- Tune `commit_skip_regex` with `--list` first — a good filter avoids most agent calls.
- `--dry-run` validates classification cheaply before real writes.
- `--limit N` bounds a run; combine with automatic resume for overnight batches.
- Start `opencode serve` once and pass `--attach http://localhost:4096` (or `--serve`) to
  avoid per-run cold starts — big speedup on hundreds of commits.
- `run_timeout_seconds` (config) caps each agent call if `timeout` is available.
- `model` (config or `--model`) picks a cheaper model for the bulk walk.

## Restart after sync
The committed `agent/project/.vibedocing.json` holds the baseline. After `git pull` in the
project, `run.sh` documents only `baseline..HEAD`. `--reset-baseline` restarts from zero.

## Auto-commit
When `auto_commit` is true (default) and not `--dry-run`/`--no-commit`, each documented
project commit produces one workspace commit (`docs(<project>): <subject>`). A trailing
baseline commit is added if the baseline advanced without a doc change. The workspace git
identity defaults to `vibedocing <vibedocing@local>` (set in config / by bootstrap).

## Runtime files (gitignored)
- `vibedocing/progress.json` — processed[] + counters (fast in-walk resume).
- `vibedocing/walk.log`, `vibedocing/logs/<sha>.log`, `vibedocing/verdicts/<sha>.txt`.
- `.vibe-trees/<short>/` — disposable worktrees.

## Troubleshooting
- **worktree add failed** — rerun (worktrees are force-removed first); or
  `git -C <source> worktree prune`.
- **Agent wrote no verdict** → recorded as `failed`; inspect `logs/<sha>.log`. Use
  `--stop-on-fail` to halt on the first one, or `--sha <sha> --dry-run` to test one commit.
- **`source_root … is not a git repository`** — fix `source_root` in `config.json`.
- **Command missing in TUI** — `run.sh --setup` (or just `run.sh`, which auto-installs)
  copies the command + skill into `.opencode/`.
- **Docs duplicating** — the agent should merge into existing files; reinforce the
  idempotency rules in `opencode/command/vibe-commit.md` if needed.
- **Commit fails (no identity)** — `run.sh` sets a local fallback; or set your own:
  `git -C . config user.name/email`.
