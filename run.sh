#!/usr/bin/env bash
#
# vibedocing/run.sh — incremental, commit-by-commit documentation pipeline (portable).
#
# STATEFUL REPLAY: for each project commit (oldest -> newest), check it out into a
# disposable git worktree and invoke the built-in `vibe-agent` CLI headlessly
# (python3 -m vibe_agent — stdlib-only, speaks any OpenAI-compatible API). The
# agent gets a FRESH, SMALL context per commit, classifies it (DOCUMENT vs SKIP),
# and updates the docs map under agent/project/.
#
# The outer loop lives HERE (in bash), outside every agent call — by design.
#
# Restart-after-sync: the last fully-processed project commit is stored (committed) in
# agent/project/.vibedocing.json. After you pull new changes into the project, re-running
# documents only baseline..HEAD.
#
# Auto-commit: when the agent changes docs, this script commits them to the workspace git
# repo (the project folder and this tooling folder are gitignored). One commit per
# documented project commit, plus a trailing baseline commit if needed.
#
# Requires: git, jq, python3 (>=3.8), an OpenAI-compatible LLM endpoint configured in
# config.json (llm section) or via env (VIBE_MODEL, VIBE_BASE_URL, VIBE_API_KEY).
# (optional: timeout)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_DIR="$SCRIPT_DIR"
WORK_DIR="$(cd "$PIPELINE_DIR/.." && pwd)"
CONFIG="$PIPELINE_DIR/config.json"
DOCS_ROOT="$WORK_DIR/agent/project"
DOCS_ROOT_REL="agent/project"
SYNC="$DOCS_ROOT/.vibedocing.json"          # COMMITTED — source of truth for baseline
PROGRESS="$PIPELINE_DIR/progress.json"    # gitignored — processed[]/counters (fast resume)
WALK_LOG="$PIPELINE_DIR/walk.log"
VERDICTS="$PIPELINE_DIR/verdicts"
RUN_LOGS="$PIPELINE_DIR/logs"
TREES="$WORK_DIR/.vibe-trees"

# make `python3 -m vibe_agent` importable regardless of the caller's cwd
export PYTHONPATH="$PIPELINE_DIR${PYTHONPATH:+:$PYTHONPATH}"

declare -A SUBJ SHORT PROC
CUR_TREE=""

die() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }
need git; need jq; need python3

jstr() { local v; v="$(jq -r "$1" "$CONFIG")"; [ "$v" = "null" ] && v=""; printf '%s' "$v"; }
jbool() { jq -e "$1 // false" "$CONFIG" >/dev/null 2>&1 && echo true || echo false; }

# strip newlines/quotes/non-printable so config-derived strings are safe to embed in
# commit messages (no option injection, no multiline -m).
sanitize() { printf '%s' "$1" | tr '\n\r' '  ' | tr -d '\047\140"\000' | sed 's/[^[:print:]]//g' | cut -c1-80 | sed 's/[[:space:]]*$//'; }

# ---- config ----
PROJECT="$(jstr '.project')";        PROJECT="${PROJECT:-project}"; PROJECT="$(sanitize "$PROJECT")"
SOURCE_REL="$(jstr '.source_root')"; SOURCE_REL="${SOURCE_REL:-.}"
if [[ "$SOURCE_REL" == /* ]]; then SOURCE_DIR="$SOURCE_REL"; else SOURCE_DIR="$WORK_DIR/$SOURCE_REL"; fi
BRANCH="$(jstr '.source_branch')";   BRANCH="${BRANCH:-main}"
SKIP_REGEX="$(jstr '.commit_skip_regex')"
SCOPE="$(jstr '.scope')"
CFG_MODEL="$(jstr '.llm.model')"
DOCS_ROOT_REL="$(jstr '.docs_root')"; DOCS_ROOT_REL="${DOCS_ROOT_REL:-agent/project}"
DOCS_ROOT="$WORK_DIR/$DOCS_ROOT_REL"
USE_WORKTREE="$(jbool '.use_worktree')"
SKIP_MERGES="$(jbool '.skip_merges')"
RUN_TIMEOUT="$(jstr '.run_timeout_seconds')"; RUN_TIMEOUT="${RUN_TIMEOUT:-0}"
AUTO_COMMIT="$(jbool '.auto_commit')"
GIT_NAME="$(jstr '.git_author_name')";   GIT_NAME="${GIT_NAME:-vibedocing}"
GIT_EMAIL="$(jstr '.git_author_email')"; GIT_EMAIL="${GIT_EMAIL:-vibedocing@local}"

mkdir -p "$VERDICTS" "$RUN_LOGS" "$DOCS_ROOT/functions" "$DOCS_ROOT/design"

# ---- workspace git ----
gitw() { git -C "$WORK_DIR" "$@"; }
ensure_git_repo() {
  [ -d "$WORK_DIR/.git" ] || die "no git repo at $WORK_DIR — run vibedocing/bootstrap.sh first"
  gitw config user.name >/dev/null 2>&1 || gitw config user.name "$GIT_NAME"
  gitw config user.email >/dev/null 2>&1 || gitw config user.email "$GIT_EMAIL"
}
ensure_git_repo

# ---- committed sync state (.vibedocing.json) ----
init_sync() {
  [ -f "$SYNC" ] || cat > "$SYNC" <<JSON
{"project":"$PROJECT","source_root":"$SOURCE_REL","branch":"$BRANCH","baseline":"","documented_commits":0,"last_synced":null}
JSON
}
sync_set_baseline() { # <sha> ("" = no baseline / rolled back before the root)
  jq --arg b "$1" --arg t "$(date -Iseconds)" \
    '.baseline=$b | .last_synced=$t' "$SYNC" > "$SYNC.tmp" && mv "$SYNC.tmp" "$SYNC"
  # keep PROJECT.md's Sync Status truthful (deterministic, not LLM-maintained).
  # NB: "${SUBJ[$1]:-}" with an EMPTY $1 is a fatal expansion error ("bad array
  # subscript") under set -u — || true cannot catch it — hence the explicit guard.
  local label=""
  if [ -n "$1" ]; then label="${SUBJ[$1]:-}"; fi
  python3 -m vibe_agent.hub --docs-root "$DOCS_ROOT" --baseline "$1" \
    --label "$label" --date "$(date +%F)" >/dev/null 2>&1 || true
}

# ---- gitignored progress (processed[]/counters) ----
init_progress() {
  [ -f "$PROGRESS" ] || printf '%s\n' \
    '{"processed":[],"failures":[],"updated":0,"skipped":0,"failed":0,"last_run":null}' > "$PROGRESS"
}
save_progress() { local e="$1"; shift; jq "$e" "$@" "$PROGRESS" > "$PROGRESS.tmp" && mv "$PROGRESS.tmp" "$PROGRESS"; }
load_processed() {
  PROC=()
  while IFS= read -r s; do [ -n "$s" ] && PROC["$s"]=1; done < <(jq -r '.processed[]?' "$PROGRESS")
}
requeue_failed() { # print full SHAs of commits recorded as failed on a previous run
  local s f
  while IFS= read -r s; do
    [ -n "$s" ] || continue
    f="$(g rev-parse --verify --quiet "$s^{commit}" 2>/dev/null || true)"
    [ -n "$f" ] && printf '%s\n' "$f"
  done < <(jq -r '.failures[]?' "$PROGRESS" 2>/dev/null)
}

# ---- auto-commit helpers ----
commit_docs() { # <short> <subject>
  [ "$AUTO_COMMIT" = true ] || return 0
  gitw add "$DOCS_ROOT"
  gitw diff --cached --quiet >/dev/null 2>&1 && return 0
  local msg; msg="$(sanitize "$2")"
  gitw commit -q -m "docs(${PROJECT}): ${msg}" -m "project commit ${1}" || echo "(commit skipped: nothing staged)"
}
commit_baseline_if_dirty() { # <short>
  [ "$AUTO_COMMIT" = true ] || return 0
  gitw add "$SYNC" "$DOCS_ROOT/PROJECT.md" 2>/dev/null || true
  gitw diff --cached --quiet >/dev/null 2>&1 && return 0
  gitw commit -q -m "docs(${PROJECT}): baseline @${1}" || true
}

# ---- git helpers on the source clone ----
g() { git -C "$SOURCE_DIR" "$@"; }
load_meta() { # <sha...>
  [ "$#" -gt 0 ] || return 0
  local batch=()
  while [ "$#" -gt 0 ]; do
    batch+=("$1"); shift
    # chunk so we never approach ARG_MAX on repos with tens of thousands of commits
    if [ "${#batch[@]}" -ge 1000 ] || [ "$#" -eq 0 ]; then
      while IFS=$'\t' read -r h sh s; do [ -n "$h" ] || continue; SUBJ["$h"]="$s"; SHORT["$h"]="$sh"; done \
        < <(g log --no-walk=unsorted --format='%H%x09%h%x09%s' "${batch[@]}")
      batch=()
    fi
  done
}

# ---- worktree lifecycle ----
make_tree() { # <sha>
  local sha="$1" path
  [ -n "${SHORT[$sha]:-}" ] || die "no short sha loaded for $sha (call load_meta first)"
  if [ "$USE_WORKTREE" = true ]; then
    path="$TREES/${SHORT[$sha]}"; rm -rf "$path"
    g worktree add --detach "$path" "$sha" >/dev/null 2>&1 || die "worktree add failed for ${SHORT[$sha]}"
    echo "$path"
  else
    [ -z "$(g status --porcelain)" ] || die "in-place mode needs a clean source clone; commit/stash first"
    g checkout -q "$sha" || die "checkout failed for ${SHORT[$sha]}"
    echo "$SOURCE_DIR"
  fi
}
free_tree() { # <path>
  local path="$1"
  [ "$USE_WORKTREE" = true ] || { g checkout -q "$BRANCH" 2>/dev/null || true; return 0; }
  [ "$path" != "$SOURCE_DIR" ] || return 0
  g worktree remove --force "$path" 2>/dev/null || rm -rf "$path"
}

# ---- cleanup on exit / interrupt: free any in-flight worktree, prune ----
cleanup() {
  if [ -n "${CUR_TREE:-}" ]; then free_tree "$CUR_TREE" 2>/dev/null || true; CUR_TREE=""; fi
  [ -n "${SOURCE_DIR:-}" ] && git -C "$SOURCE_DIR" worktree prune 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---- run one commit ----
run_one() { # <sha>
  local sha="$1" short subject path args verdict rc kind extra="" parent=""
  short="${SHORT[$sha]}"; subject="${SUBJ[$sha]}"

  if [ -n "$SKIP_REGEX" ] && [[ $subject =~ $SKIP_REGEX ]]; then
    echo "[$short] SKIP (regex)  $subject"
    printf 'VERDICT: NO_DOC(regex)\n' > "$VERDICTS/$sha.txt"
    sync_set_baseline "$sha"
    save_progress --arg b "$sha" '.processed += [$b] | .skipped += 1'
    return 0
  fi

  path="$(make_tree "$sha")"
  CUR_TREE="$path"

  local ag=(python3 -m vibe_agent --config "$CONFIG" --sha "$sha" --worktree "$path"
            --docs-root "$DOCS_ROOT" --docs-root-rel "$DOCS_ROOT_REL"
            --verdicts-dir "$VERDICTS")
  [ "${CLASSIFY_ONLY:-0}" = 1 ] && ag+=(--classify-only)
  local model="${OVERRIDE_MODEL:-$CFG_MODEL}"; [ -n "$model" ] && ag+=(--model "$model")

  echo "[$short] PROCESS       $subject"
  rc=0
  if [ -n "$RUN_TIMEOUT" ] && [ "$RUN_TIMEOUT" != 0 ] && command -v timeout >/dev/null 2>&1; then
    timeout "${RUN_TIMEOUT}s" "${ag[@]}" > "$RUN_LOGS/$sha.log" 2>&1 || rc=$?
  else
    "${ag[@]}" > "$RUN_LOGS/$sha.log" 2>&1 || rc=$?
  fi
  free_tree "$path"
  CUR_TREE=""

  verdict="$(head -1 "$VERDICTS/$sha.txt" 2>/dev/null || true)"
  case "$verdict" in
    VERDICT:\ DOC_UPDATED*) kind=updated;;
    VERDICT:\ NO_DOC*)      kind=skipped;;
    *)                      kind=failed; verdict="${verdict:-NO_VERDICT(rc=$rc)}";;
  esac

  if [ "$kind" = failed ] && [ "${STOP_ON_FAIL:-0}" = 1 ]; then
    echo "[$short] FAILED: $verdict — STOP_ON_FAIL; see logs/$sha.log" | tee -a "$WALK_LOG"
    die "stopping at $short"
  fi

  case "$kind" in
    updated) sync_set_baseline "$sha"
             save_progress --arg b "$sha" '.processed += [$b] | .updated += 1'; commit_docs "$short" "$subject";;
    skipped) sync_set_baseline "$sha"
             save_progress --arg b "$sha" '.processed += [$b] | .skipped += 1';;
    *)       # failure is NOT "processed": roll the baseline back to the parent so
             # the next run's rev-list range includes this commit again
             parent="$(g rev-parse --verify --quiet "$sha^" 2>/dev/null || true)"
             sync_set_baseline "${parent:-}"
             save_progress --arg b "$sha" --arg f "$short" '.processed += [$b] | .failed += 1 | .failures += [$f]'
             echo "[$short]    analyze: logs/$sha.log  $( [ -f "$VERDICTS/$sha.transcript.jsonl" ] && echo "verdicts/$sha.transcript.jsonl (python3 -m vibe_agent.transcript <file>)" )";;
  esac
  echo "[$short] -> $kind ($verdict)"
}

# ---- CLI ----
read -r -d '' USAGE <<'EOF' || true
Usage: run.sh [options]

  (no args)       Run the walk from the committed baseline up to project HEAD.
  --list          Show commit list + SKIP/PROCESS decisions, then exit (no agent calls).
  --dry-run       Invoke the agent in classify-only mode (no doc writes, no commits).
  --validate [S]  Validate existing docs against the tree at commit S (default HEAD):
                  links, numbering, layout, source paths, stale references. No agent.
  --reset-baseline  Reset the committed baseline to the project's first commit and exit.
  --limit N       Process at most N commits this run.
  --range X       Process commits in range X (e.g. A..B). Overrides baseline.
  --sha S         Process a single commit (ignores baseline).
  --in-place      Checkout each commit in the source clone instead of a worktree.
  --stop-on-fail  Halt on the first failed commit (default: record and continue).
  --no-commit     Do not git-commit doc changes this run (overrides config auto_commit).
  --model M       Override the model for this run (or export VIBE_MODEL).
  --help          Show this help.
EOF

DO_LIST=0; RESET_BASE=0; LIMIT=0; RANGE=""; SINGLE=""; OVERRIDE_MODEL=""
STOP_ON_FAIL=0; CLASSIFY_ONLY=0; VALIDATE=0; VALIDATE_REF=""
while [ $# -gt 0 ]; do
  case "$1" in
    --list) DO_LIST=1;;
    --dry-run) CLASSIFY_ONLY=1;;
    --validate) VALIDATE=1
                if [ $# -ge 2 ] && [[ "$2" != -* ]]; then VALIDATE_REF="$2"; shift; fi;;
    --reset-baseline) RESET_BASE=1;;
    --limit) LIMIT="${2:?--limit needs N}"; shift;;
    --range) RANGE="${2:?--range needs A..B}"; shift;;
    --sha) SINGLE="${2:?--sha needs SHA}"; shift;;
    --in-place) USE_WORKTREE=false;;
    --stop-on-fail) STOP_ON_FAIL=1;;
    --no-commit) AUTO_COMMIT=false;;
    --model) OVERRIDE_MODEL="${2:?--model needs M}"; shift;;
    --help|-h) echo "$USAGE"; exit 0;;
    *) die "unknown arg: $1";;
  esac
  shift
done
export USE_WORKTREE STOP_ON_FAIL CLASSIFY_ONLY OVERRIDE_MODEL

init_sync; init_progress

[ "$RESET_BASE" = 1 ] && { sync_set_baseline ""; rm -f "$PROGRESS"; init_progress; echo "Baseline reset to start."; exit 0; }

[ -d "$SOURCE_DIR/.git" ] || die "source_root '$SOURCE_DIR' is not a git repository (set source_root in config.json)"

# ---- standalone validation mode: audit the docs map without any agent calls ----
if [ "$VALIDATE" = 1 ]; then
  REF="${VALIDATE_REF:-HEAD}"
  FULL="$(g rev-parse --verify --quiet "$REF^{commit}" || true)"
  [ -n "$FULL" ] || die "commit not found for --validate: $REF"
  load_meta "$FULL"
  VPATH="$(make_tree "$FULL")"
  CUR_TREE="$VPATH"
  python3 -m vibe_agent.validate --docs-root "$DOCS_ROOT" --worktree "$VPATH" \
    --report "$VERDICTS/validate-${SHORT[$FULL]}.md" --sha "$FULL" \
    || { free_tree "$VPATH"; CUR_TREE=""; die "validation found errors (see above and $VERDICTS/validate-${SHORT[$FULL]}.md)"; }
  free_tree "$VPATH"; CUR_TREE=""
  echo "docs validation passed at ${SHORT[$FULL]}"
  exit 0
fi

# fail fast on an obviously missing model config (env VIBE_MODEL overrides)
[ -n "${OVERRIDE_MODEL:-}" ] || [ -n "$CFG_MODEL" ] || \
  die "no model configured: set llm.model in config.json or export VIBE_MODEL"

# ---- commit list ----
rl=(--reverse)
[ "$SKIP_MERGES" = true ] && rl+=(--no-merges)
# optional pathspec scope: process only commits touching the configured subdirectory
sc=()
[ -n "$SCOPE" ] && sc=(-- "$SCOPE")
if [ -n "$SINGLE" ]; then
  # normalize to the full SHA: SUBJ/SHORT are keyed by full SHAs and `set -u`
  # aborts on a missing key (e.g. when the user passes a short sha)
  FULL="$(g rev-parse --verify --quiet "$SINGLE^{commit}" || true)"
  [ -n "$FULL" ] || die "commit not found in source repo: $SINGLE"
  SHAS=("$FULL")
elif [ -n "$RANGE" ]; then mapfile -t SHAS < <(g rev-list "${rl[@]}" "$RANGE" ${sc[@]+"${sc[@]}"})
else
  BASELINE="$(jq -r '.baseline // ""' "$SYNC")"
  if [ -z "$BASELINE" ]; then mapfile -t SHAS < <(g rev-list "${rl[@]}" HEAD ${sc[@]+"${sc[@]}"})
  else mapfile -t SHAS < <(g rev-list "${rl[@]}" "${BASELINE}..HEAD" ${sc[@]+"${sc[@]}"}); fi
fi
load_processed

# ---- retry commits that failed on a previous run (LLM outage, timeouts, ...) ----
mapfile -t RETRY < <(requeue_failed)
if [ "${#RETRY[@]}" -gt 0 ]; then
  drops="$(printf '%s\n' "${RETRY[@]}" | jq -R . | jq -s -c .)"
  jq --argjson d "$drops" \
     '(.processed) |= map(select(. as $p | ($d | index($p)) == null)) | .failures = [] | .failed = 0' \
     "$PROGRESS" > "$PROGRESS.tmp" && mv "$PROGRESS.tmp" "$PROGRESS"
  for sha in "${RETRY[@]}"; do
    unset "PROC[$sha]" 2>/dev/null || true
    if [ "${#SHAS[@]}" -eq 0 ] || ! printf '%s\n' "${SHAS[@]}" | grep -qx "$sha"; then
      SHAS+=("$sha")
    fi
  done
  echo "requeuing ${#RETRY[@]} previously failed commit(s) for retry"
fi

[ "${#SHAS[@]}" -gt 0 ] || { echo "No new commits to process (baseline is at HEAD)."; exit 0; }

load_meta "${SHAS[@]}"

# ---- list mode ----
if [ "$DO_LIST" = 1 ]; then
  p=0; s=0; d=0
  printf '%-12s %-8s %s\n' SHORT DECISION SUBJECT
  for sha in "${SHAS[@]}"; do
    subj="${SUBJ[$sha]:-?}"; short="${SHORT[$sha]:-??????????}"
    if [[ -v PROC["$sha"] ]]; then dec="DONE"; d=$((d+1))
    elif [ -n "$SKIP_REGEX" ] && [[ $subj =~ $SKIP_REGEX ]]; then dec="SKIP"; s=$((s+1))
    else dec="PROCESS"; p=$((p+1)); fi
    printf '%-12s %-8s %s\n' "$short" "$dec" "$subj"
  done
  echo "---"; echo "range=${#SHAS[@]} PROCESS=$p SKIP=$s DONE=$d"
  exit 0
fi

{
  echo "=== vibe-walk started $(date -Iseconds) ==="
  echo "project=$PROJECT source=$SOURCE_REL branch=$BRANCH commits=${#SHAS[@]} worktree=$USE_WORKTREE classify=$CLASSIFY_ONLY commit=$AUTO_COMMIT model=${OVERRIDE_MODEL:-$CFG_MODEL}"
} | tee -a "$WALK_LOG"

count=0; last_short=""
for sha in "${SHAS[@]}"; do
  [[ -v PROC["$sha"] ]] && continue
  [ "$LIMIT" -gt 0 ] && [ "$count" -ge "$LIMIT" ] && { echo "Reached --limit $LIMIT; stopping." | tee -a "$WALK_LOG"; break; }
  count=$((count+1)); last_short="${SHORT[$sha]:-??????????}"
  run_one "$sha" 2>&1 | tee -a "$WALK_LOG"
done

# persist any trailing baseline advance that wasn't captured by a doc commit
commit_baseline_if_dirty "${last_short:-none}"

echo "=== summary ===" | tee -a "$WALK_LOG"
echo "sync baseline: $(jq -r '.baseline' "$SYNC")"
jq '{processed:(.processed|length), updated, skipped, failed, failures, last_run}' "$PROGRESS"
gitw log --oneline -5 2>/dev/null | sed 's/^/  /'
