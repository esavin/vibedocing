# Guide — porting, performance, troubleshooting

## Requirements
`git`, `jq`, `python3` (>=3.8) on PATH — the agent is stdlib-only, no pip packages.
Optional `timeout` (per-run limits). Plus any OpenAI-compatible LLM endpoint.

## Porting to a new project / language
The pipeline is language-agnostic. Per-project specifics live in two files you edit:
- `agent/project/project-conventions.md` — stack, path prefix, area tags, commit style.
- `vibedocing/config.json` — `source_root`, `source_branch`, `commit_skip_regex`, …

`bootstrap.sh` auto-detects language/layout and fills these in as a starting point; correct
them if needed. Common `source_root` values (docs always cite **repository-root-relative**
paths like `src/...`, whatever the clone location):

| Project shape | source_root | path style in docs |
| --- | --- | --- |
| Project cloned inside workspace | `someproject` (its folder name) | `src/...` (no clone-folder prefix!) |
| Single repo (workspace IS the repo) | `.` | `src/...`, `cmd/...` |
| Project outside workspace | absolute path | `src/...` (no clone-folder prefix!) |

## Model / gateway configuration
The agent speaks the OpenAI-compatible chat-completions API. Configure in
`vibedocing/config.json` (`llm` section) or via environment:

| Setting | config.json | env override | default |
| --- | --- | --- | --- |
| model | `llm.model` | `VIBE_MODEL` (or `--model`) | — (required) |
| endpoint | `llm.base_url` | `VIBE_BASE_URL` | `https://api.openai.com/v1` |
| API key | `llm.api_key_env` names the env var | `VIBE_API_KEY` (fallback `OPENAI_API_KEY`) | — |

Known `base_url` values that work: OpenAI (default), OpenRouter
`https://openrouter.ai/api/v1`, DeepSeek `https://api.deepseek.com/v1`, Groq
`https://api.groq.com/openai/v1`, NeuralDeep `https://api.neuraldeep.ru/v1`
(models e.g. `qwen3.6-35b-a3b`, `gpt-oss-120b`; request-bucket limits — honor
`Retry-After`), vLLM/llama.cpp `http://host:8000/v1`,
Ollama `http://localhost:11434/v1` (no key needed), LiteLLM proxy, Anthropic's
OpenAI-compat endpoint `https://api.anthropic.com/v1`.

Other `llm` knobs: `max_steps` (tool-call rounds per commit, default 24),
`max_steps_initial` (step budget for the root/initial-snapshot commit, default 0 =
keep `max_steps`; 48 is a good value for projects born as one giant commit),
`max_steps_cap` (default 48 — doc-heavy commits get `+1` step per 8 changed files
on top of `max_steps`, capped here: a 34-file move commit needs more write rounds
than a one-liner), `request_timeout_seconds` (per HTTP request, default 180),
`retries` (default 5; exponential backoff, honors `Retry-After`), `temperature`
and `max_tokens` (omitted when null/0 — some strict gateways/models reject
explicit values), `log_transcript` (default true — write the full LLM interaction
log per commit, see Troubleshooting below).

Built-in loop guardrails (observed on weak models, fernflower runs): exact
duplicate tool calls are refused instead of executed (loops burned 5-7 steps on
one repeated `git show`); the last 5 steps carry deadline pressure and a hard
"call finish NOW" message; a budget that runs out mid-`write_doc` is extended
(up to 2×6 steps); a budget that runs out with docs already written gets ONE
finish-only grace round - and if even that passes without a finish call, the
verdict is synthesized from the docs actually written (still validated).
`write_doc` supports `{"append": true}` for docs too long for one call, and a
tool call whose JSON was cut off by the gateway's output token limit (qwen on
neuraldeep caps at 8000 tokens without setting `finish_reason`) gets a
dedicated split-and-append hint instead of a bare parse error. `write_doc`
also lints cited repo paths at write time and warns immediately, enforces
per-directory sequential numbering for new numbered docs (a refusal names the
exact expected path - weak models otherwise continue another directory's
counter and leave permanent gaps like functions/ 01-09 then 22-25), and
supports `{"delete": true}` to remove duplicate/obsolete docs during repair.

## Qwen models (thinking mode)
Qwen3.x reasoning models work best in agent loops with **thinking ON**: community
benchmarks (qwen-native-agents, run against neuraldeep) measured tool-call
correctness 75→100% and instruction following 69→85% for `qwen3.6` with thinking
enabled — vs the opposite for `gpt-oss` (reasoning *hurts* it). The switch is
backend-specific; on vLLM-based gateways (e.g. neuraldeep) pass it via `llm.extra_body`:

```json
"llm": {
  "base_url": "https://api.neuraldeep.ru/v1",
  "model": "qwen3.6-35b-a3b",
  "extra_body": { "chat_template_kwargs": { "enable_thinking": true } }
}
```

- `extra_body` keys are merged into the request payload without overriding the core
  fields (model/messages/tools/...). Keep it `{}` on strict gateways (OpenAI rejects
  unknown fields with HTTP 400).
- For `gpt-oss`-style models prefer `"extra_body": {"reasoning_effort": "low"}` or
  simply omit it.
- The agent strips reasoning from the conversation automatically (both the separate
  `reasoning_content` field and inline `<think>...</think>` blocks), so thoughts never
  bloat later steps — but they still consume output tokens on the producing step.
- Tool calling stays native OpenAI `tools` — no need for Qwen's NOUS format; measured
  difference is within 1% and any compliant gateway normalizes it.

## The "giant initial commit" (INITIAL SNAPSHOT mode)
Projects often begin with one massive commit containing the whole codebase. The
pipeline detects root commits (no parent) automatically: the agent gets a **tree
digest** (real directories + file counts) instead of the diffstat, a bigger step
budget (`llm.max_steps_initial`), and hard rules to build the module map from the
worktree only — never from prior knowledge of the project (older versions, forks,
upstream articles). Coverage may be partial (entry points + top-level architecture);
later commits refine the map idempotently. To chunk a huge first pass further, set
`scope` in config to a subdirectory (pathspec filter on which commits are processed)
and run multiple passes.

## Renames, moves, deletions (path hygiene)
Every commit's first message includes a rename-aware name-status; when existing docs
cite paths renamed/deleted by the commit, the agent gets a precomputed STALE DOC
REFERENCES worklist and must repair every doc (the `search_docs` tool finds all
occurrences). Such commits are DOCUMENT even though they look like refactors — a map
pointing at pre-rename paths is useless. A repo-wide path-existence check on every
doc pass additionally catches stale paths introduced by earlier skipped commits.

## Validation (quality gate)
- `naming` also enforces the canonical two-digit numbering: an unpadded
  `functions/1-x.md` is an error, and `1-x.md`/`01-x.md` count as duplicate
  numbers. `write_doc` normalizes unpadded names automatically (a write to
  `functions/1-x.md` lands as `functions/01-x.md` and replaces the unpadded
  twin), so the repair is a single re-save under the padded path.
After every DOC_UPDATED verdict the docs map is validated mechanically (config
`validation`):
- checks: fixed layout; `<number>-<name>.md` naming with unique numbers per
  directory; all relative markdown links resolve; path-shaped references resolve in
  the worktree; nothing cites paths renamed/deleted by this commit;
- problems go back to the agent for up to `validation.rounds` repair rounds; each
  repair round **extends the step budget by 6 steps** (validation feedback must
  never eat the steps the model needs to fix and re-finish), and in the last two
  steps of the budget a deadline note is appended to tool results telling the
  model to call `finish` now;
- validation runs whenever the session left docs on disk — including a model that
  wrote docs but finished with NO_DOC (dirty docs are never published silently);
- `mode: "strict"` (default) flips the verdict to ERROR if deterministic errors
  remain → the commit is requeued and retried, broken docs are not published;
  `"warn"` only records the report (`verdicts/<sha>.validation.md`); `"off"` disables;
- `path_check` (`error`|`warn`|`off`) downgrades the (heuristic) path-existence
  check if your project legitimately cites non-repo paths;
- audit an existing docs map any time: `./vibedocing/run.sh --validate [sha]`.

## Performance / cost
- Tune `commit_skip_regex` with `--list` first — a good filter avoids most agent calls.
- Re-running a project from scratch? `--reuse-verdicts` replays previous NO_DOC
  verdicts for free (see "Re-running from scratch").
- `--dry-run` validates classification cheaply before real writes.
- `--limit N` bounds a run; combine with automatic resume for overnight batches.
- Each agent call is a fresh short-lived process — there is no server to keep warm.
  A cheap/smaller model is often enough: the system prompt is ~1K tokens and the commit
  metadata + diffstat are pre-injected, so most SKIP verdicts cost a single round-trip.
- `run_timeout_seconds` (config) caps each agent call if `timeout` is available.
- `model` (config `llm.model`, env `VIBE_MODEL`, or `--model`) picks the model.

## Restart after sync
The committed `agent/project/.vibedocing.json` holds the baseline. After `git pull` in the
project, `run.sh` documents only `baseline..HEAD`. `--reset-baseline` restarts from zero.

## Re-running from scratch (verdict reuse)
`--reuse-verdicts DIR` replays NO_DOC verdicts from a previous run's `verdicts/`
directory: matching commits are marked SKIP without any agent calls, and their
`<sha>.txt`/`<sha>.json` artifacts are copied into the new run's verdicts dir for
traceability. Commits that don't exist in the source repo are ignored, so the same
folder can be shared across projects safely. DOC_UPDATED verdicts are never reused —
those commits must be re-documented because the docs map is rebuilt from scratch.
`--skip-list FILE` takes a plain list of hashes (one per line, `#` comments, short or
full SHAs) and force-skips them; it also lets you override commits a previous run
marked DOC_UPDATED. Preview with `--list` (they appear as `SKIP*`). Caveat: skip
decisions can depend on `project-conventions.md` and the model — don't reuse verdicts
after editing conventions and expect identical coverage.

### --doc-hints (reconsideration round)
Borderline commits legitimately flip between runs (sampling noise + the docs-map state
differs), and a missed DOC_UPDATED is much costlier than a missed SKIP. With
`--reuse-verdicts DIR --doc-hints`, commits the prior run documented get a second
chance: if the current agent finishes NO_DOC, the loop does NOT accept it yet — it
injects one extra user message containing the *content* of the prior run's docs
(capped: 6 files, 16 KB each, 48 KB total) and the prior finish reason, and the agent
must finish again: write the docs adapted to the current map (DOC_UPDATED) or reaffirm
NO_DOC with a justification. The round extends the step budget by 8, happens at most
once per commit, and is recorded in the verdict JSON (`"reconsidered": true`) and the
transcript (`"source": "reconsider"`). The prior docs are located automatically at
`<verdicts-dir>/../../<docs_root from the prior config.json>` — keep the prior
workspace intact, or pass a verdicts folder that still sits inside it. In
`--dry-run` mode the reconsideration asks for the verdict only (writes stay
disabled).

## Auto-commit
When `auto_commit` is true (default) and not `--dry-run`/`--no-commit`, each documented
project commit produces one workspace commit (`docs(<project>): <subject>`). A trailing
baseline commit is added if the baseline advanced without a doc change. The workspace git
identity defaults to `vibedocing <vibedocing@local>` (set in config / by bootstrap).

## Runtime files (gitignored)
- `vibedocing/progress.json` — processed[] + counters (fast in-walk resume).
- `vibedocing/walk.log`, `vibedocing/logs/<sha>.log`, `vibedocing/verdicts/<sha>.{txt,json}`.
- `vibedocing/verdicts/<sha>.transcript.jsonl` — full LLM interaction log for that
  commit: every model response (content, tool calls, `finish_reason`, per-step token
  usage) and every tool result exactly as it was fed back. Replay order = the exact
  message history the model saw. Disable with `llm.log_transcript: false`.
- `.vibe-trees/<short>/` — disposable worktrees.

## The agent (vibe_agent/)
`vibedocing/vibe_agent/` — Python package, stdlib-only:
- `cli.py` — argument parsing, verdict artifacts, validation wiring, error handling.
- `llm.py` — OpenAI-compatible HTTP client with retries (swap-in point for the `openai` SDK).
- `tools.py` — the six tools with **code-level guards**: git restricted to
  `show|log|diff|ls-tree|grep` (scrubbed GIT_* env, no pager, no `-c`/`--output`),
  reads confined to the worktree/docs root, `search_docs` (regex over the docs root),
  writes confined to `.md` under the docs root.
- `prompt.py` — compact system prompt + grounded first message (commit meta,
  rename-aware name-status, stale-doc worklist, tree digest for root commits,
  conventions).
- `agent.py` — the loop; ends only via the `finish` tool; supports validation-driven
  repair rounds.
- `validate.py` — the mechanical docs validator (also a standalone CLI).
- `hub.py` — deterministic PROJECT.md Sync Status maintenance.

You can run one step standalone (as `run.sh` does):
```bash
PYTHONPATH=vibedocing python3 -m vibe_agent --config vibedocing/config.json \
  --sha <sha> --worktree <worktree-path> --docs-root agent/project \
  --verdicts-dir vibedocing/verdicts [--classify-only] [--model M]
```

## Troubleshooting
- **`ERROR max_steps (N) reached without finish`** — the model never called the
  `finish` tool within its step budget. Diagnose from the recorded transcript:
  ```bash
  python3 -m vibe_agent.transcript vibedocing/verdicts/<sha>.transcript.jsonl
  ```
  The summary prints the context-growth curve (per-step `prompt_tokens`), a tool
  histogram, identical repeated calls, and cut responses. Read it as:
  - `finish_reason=length` on some responses → `llm.max_tokens` is too small: the
    tool-call JSON is truncated mid-way, the call is refused as invalid JSON, and
    the loop burns all steps. Raise `llm.max_tokens` (or remove the cap).
  - `prompt_tokens` climbing steeply toward the model's context window → context
    pressure, not model quality: the first message plus large tool results
    (`git show` on a big commit, `read_file` on huge files) crowd out the
    instructions. Mitigate by splitting the work (`scope` for the giant root
    commit), lowering `max_steps`, or using a model with a larger window.
  - identical tool calls repeated, plain-text answers that never call `finish`,
    or garbage tool arguments at a small token count → the model itself is too
    weak for tool loops; switch model (see the Qwen/gpt-oss notes above). The
    pipeline already compensates for the most common weak-model traits: docs-root
    prefixes on read paths are auto-stripped, repair rounds extend the step
    budget, text-only/empty replies get an immediate user nudge back to tools,
    exact duplicate calls are refused, the deadline is pushed hard in the last
    5 steps, and a budget that runs out mid-write is extended (up to 2×6 steps)
    with a final finish-only grace round when docs were written.
  Note: a genuinely *exceeded* context window surfaces differently — as
  `ERROR llm: HTTP 400 … context length …`, not as max_steps.
- **worktree add failed** — rerun (worktrees are force-removed first); or
  `git -C <source> worktree prune`.
- **Agent wrote no verdict** → recorded as `failed`; inspect `logs/<sha>.log`. Use
  `--stop-on-fail` to halt on the first one, or `--sha <sha> --dry-run` to test one commit.
- **`no model configured`** — set `llm.model` in `config.json`, or `export VIBE_MODEL=…`,
  or pass `--model`.
- **HTTP 401/403 in logs** — export the API key named by `llm.api_key_env`
  (`VIBE_API_KEY` by default).
- **HTTP 404 / "model not found"** — wrong `llm.model` or `llm.base_url` for that gateway.
- **HTTP 400 mentioning tools/function-calling** — that model or gateway does not support
  tool calls; pick another model (any current chat model works).
- **`ERROR llm: …` verdict** — the endpoint failed after retries (rate limit, outage,
  e.g. HTTP 503 "no available server" on busy gateways). HTTP-level retries are
  patient by design: they wait **1, 2, 4, 8, 16 minutes** between attempts
  (`retries` defaults to 5 — up to ~31 min total), riding out provider outages
  in place; an explicit `Retry-After` header wins, capped at 16 min. Connection
  errors keep the fast exponential curve (they are almost always transient).
  Note `run_timeout_seconds`, if set, caps the whole agent call and can cut the
  retry waits short — keep it `0` for unattended runs. Failed commits are
  retried automatically on the next run (requeued from `progress.json`; the
  baseline rolls back to their parent). For even longer outages raise
  `llm.retries`.
- **`ERROR validation failed …` verdict** — the agent's docs still had mechanical
  errors after `validation.rounds` repair rounds (see
  `verdicts/<sha>.validation.md`). The commit is requeued automatically; either
  re-run (the next attempt starts from the partially-fixed docs), fix the docs by
  hand, or — for legitimately unresolvable path references — set
  `validation.path_check: "warn"` or `validation.mode: "warn"` in config.
- **`source_root … is not a git repository`** — fix `source_root` in `config.json`.
- **Docs duplicating** — the agent should merge into existing files; reinforce the
  idempotency wording in `vibedocing/vibe_agent/prompt.py` if needed.
- **Commit fails (no identity)** — `run.sh` sets a local fallback; or set your own:
  `git -C . config user.name/email`.
