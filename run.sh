#!/usr/bin/env bash
#
# vibedocing/run.sh — incremental, commit-by-commit documentation pipeline (portable).
#
# STATEFUL REPLAY: for each project commit (oldest -> newest), check it out into a
# disposable git worktree and invoke `opencode run --command vibe-commit` headlessly. The
# agent gets a FRESH, SMALL context per commit, classifies it (DOCUMENT vs SKIP), and
# updates the docs map under agent/project/.
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
# Requires: git, jq, opencode. (optional: timeout)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_DIR="$SCRIPT_DIR"
WORK_DIR="$(cd "$PIPELINE_DIR/.." && pwd)"
CONFIG="$PIPELINE_DIR/config.json"
DOCS_ROOT="$WORK_DIR/agent/project"
SYNC="$DOCS_ROOT/.vibedocing.json"          # COMMITTED — source of truth for baseline
PROGRESS="$PIPELINE_DIR/progress.json"    # gitignored — processed[]/counters (fast resume)
WALK_LOG="$PIPELINE_DIR/walk.log"
VERDICTS="$PIPELINE_DIR/verdicts"
RUN_LOGS="$PIPELINE_DIR/logs"
TREES="$WORK_DIR/.vibe-trees"

declare -A SUBJ SHORT PROC
CUR_TREE=""; SERVE_PID=""; SERVE_LOG=""

die() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }
need git; need jq; need opencode

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
CMD_NAME="$(jstr '.commit_command')"; CMD_NAME="${CMD_NAME:-vibe-commit}"
USE_WORKTREE="$(jbool '.use_worktree')"
SKIP_MERGES="$(jbool '.skip_merges')"
CFG_MODEL="$(jstr '.model')"
CFG_AGENT="$(jstr '.agent')"
AUTO="$(jbool '.auto')"
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

# ---- install command + skill into .opencode/ from canonical copies ----
install_into_opencode() {
  local src="$PIPELINE_DIR/opencode"
  mkdir -p "$WORK_DIR/.opencode/command" "$WORK_DIR/.opencode/skills"
  [ -d "$src/command" ] && cp -f "$src"/command/* "$WORK_DIR/.opencode/command/" 2>/dev/null || true
  [ -d "$src/skills" ] && cp -rf "$src"/skills/* "$WORK_DIR/.opencode/skills/" 2>/dev/null || true
}

# ---- committed sync state (.vibedocing.json) ----
init_sync() {
  [ -f "$SYNC" ] || cat > "$SYNC" <<JSON
{"project":"$PROJECT","source_root":"$SOURCE_REL","branch":"$BRANCH","baseline":"","documented_commits":0,"last_synced":null}
JSON
}
sync_set_baseline() { # <sha>
  jq --arg b "$1" --arg t "$(date -Iseconds)" \
    '.baseline=$b | .last_synced=$t' "$SYNC" > "$SYNC.tmp" && mv "$SYNC.tmp" "$SYNC"
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

# ---- auto-commit helpers ----
commit_docs() { # <short> <subject>
  [ "$AUTO_COMMIT" = true ] || return 0
  gitw add agent/project
  gitw diff --cached --quiet >/dev/null 2>&1 && return 0
  local msg; msg="$(sanitize "$2")"
  gitw commit -q -m "docs(${PROJECT}): ${msg}" -m "project commit ${1}" || echo "(commit skipped: nothing staged)"
}
commit_baseline_if_dirty() { # <short>
  [ "$AUTO_COMMIT" = true ] || return 0
  gitw add "$SYNC" 2>/dev/null || gitw add agent/project/.vibedocing.json 2>/dev/null || true
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

# ---- cleanup on exit / interrupt: free any in-flight worktree, prune, stop serve ----
cleanup() {
  if [ -n "${CUR_TREE:-}" ]; then free_tree "$CUR_TREE" 2>/dev/null || true; CUR_TREE=""; fi
  [ -n "${SOURCE_DIR:-}" ] && git -C "$SOURCE_DIR" worktree prune 2>/dev/null || true
  if [ -n "${SERVE_PID:-}" ]; then kill "$SERVE_PID" 2>/dev/null || true; fi
  [ -n "${SERVE_LOG:-}" ] && [ -f "$SERVE_LOG" ] && rm -f "$SERVE_LOG" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---- run one commit ----
run_one() { # <sha>
  local sha="$1" short subject path args verdict rc kind extra=""
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
  [ "${CLASSIFY_ONLY:-0}" = 1 ] && extra="classify-only"
  args="$sha $path"; [ -n "$extra" ] && args="$args $extra"

  local oc=(run --command "$CMD_NAME")
  [ "$AUTO" = true ] && oc+=(--auto)
  local model="${OVERRIDE_MODEL:-$CFG_MODEL}"; [ -n "$model" ] && oc+=(--model "$model")
  [ -n "$CFG_AGENT" ] && oc+=(--agent "$CFG_AGENT")
  [ -n "$ATTACH" ] && oc+=(--attach "$ATTACH")
  oc+=("$args")

  echo "[$short] PROCESS       $subject"
  rc=0
  if [ -n "$RUN_TIMEOUT" ] && [ "$RUN_TIMEOUT" != 0 ] && command -v timeout >/dev/null 2>&1; then
    timeout "${RUN_TIMEOUT}s" opencode "${oc[@]}" > "$RUN_LOGS/$sha.log" 2>&1 || rc=$?
  else
    opencode "${oc[@]}" > "$RUN_LOGS/$sha.log" 2>&1 || rc=$?
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

  sync_set_baseline "$sha"
  case "$kind" in
    updated) save_progress --arg b "$sha" '.processed += [$b] | .updated += 1'; commit_docs "$short" "$subject";;
    skipped) save_progress --arg b "$sha" '.processed += [$b] | .skipped += 1';;
    *)       save_progress --arg b "$sha" --arg f "$short" '.processed += [$b] | .failed += 1 | .failures += [$f]';;
  esac
  echo "[$short] -> $kind ($verdict)"
}

# ---- CLI ----
read -r -d '' USAGE <<'EOF' || true
Usage: run.sh [options]

  (no args)       Run the walk from the committed baseline up to project HEAD.
  --setup         (re)install command+skill into .opencode/ and exit.
  --list          Show commit list + SKIP/PROCESS decisions, then exit (no agent calls).
  --dry-run       Invoke the agent in classify-only mode (no doc writes, no commits).
  --reset-baseline  Reset the committed baseline to the project's first commit and exit.
  --limit N       Process at most N commits this run.
  --range X       Process commits in range X (e.g. A..B). Overrides baseline.
  --sha S         Process a single commit (ignores baseline).
  --in-place      Checkout each commit in the source clone instead of a worktree.
  --stop-on-fail  Halt on the first failed commit (default: record and continue).
  --no-commit     Do not git-commit doc changes this run (overrides config auto_commit).
  --attach U      Attach each run to a running 'opencode serve' at URL U.
  --model M       Override the model for this run.
  --serve         Start 'opencode serve' in the background for the run, then stop it.
  --help          Show this help.
EOF

DO_LIST=0; RESET_BASE=0; DO_SETUP=0; LIMIT=0; RANGE=""; SINGLE=""; ATTACH=""; OVERRIDE_MODEL=""
STOP_ON_FAIL=0; CLASSIFY_ONLY=0; SERVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --setup) DO_SETUP=1;;
    --list) DO_LIST=1;;
    --dry-run) CLASSIFY_ONLY=1;;
    --reset-baseline) RESET_BASE=1;;
    --limit) LIMIT="${2:?--limit needs N}"; shift;;
    --range) RANGE="${2:?--range needs A..B}"; shift;;
    --sha) SINGLE="${2:?--sha needs SHA}"; shift;;
    --in-place) USE_WORKTREE=false;;
    --stop-on-fail) STOP_ON_FAIL=1;;
    --no-commit) AUTO_COMMIT=false;;
    --attach) ATTACH="${2:?--attach needs URL}"; shift;;
    --model) OVERRIDE_MODEL="${2:?--model needs M}"; shift;;
    --serve) SERVE=1;;
    --help|-h) echo "$USAGE"; exit 0;;
    *) die "unknown arg: $1";;
  esac
  shift
done
export USE_WORKTREE STOP_ON_FAIL CLASSIFY_ONLY ATTACH OVERRIDE_MODEL

install_into_opencode
[ "$DO_SETUP" = 1 ] && { echo "Installed command + skill into .opencode/"; exit 0; }

init_sync; init_progress

[ "$RESET_BASE" = 1 ] && { sync_set_baseline ""; rm -f "$PROGRESS"; init_progress; echo "Baseline reset to start."; exit 0; }

[ -d "$SOURCE_DIR/.git" ] || die "source_root '$SOURCE_DIR' is not a git repository (set source_root in config.json)"

# ---- commit list ----
rl=(--reverse)
[ "$SKIP_MERGES" = true ] && rl+=(--no-merges)
if [ -n "$SINGLE" ]; then SHAS=("$SINGLE")
elif [ -n "$RANGE" ]; then mapfile -t SHAS < <(g rev-list "${rl[@]}" "$RANGE")
else
  BASELINE="$(jq -r '.baseline // ""' "$SYNC")"
  if [ -z "$BASELINE" ]; then mapfile -t SHAS < <(g rev-list "${rl[@]}" HEAD)
  else mapfile -t SHAS < <(g rev-list "${rl[@]}" "${BASELINE}..HEAD"); fi
fi
[ "${#SHAS[@]}" -gt 0 ] || { echo "No new commits to process (baseline is at HEAD)."; exit 0; }

load_processed
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

# ---- optional managed server ----
if [ "$SERVE" = 1 ] && [ -z "$ATTACH" ]; then
  SERVE_PORT="${SERVE_PORT:-4096}"
  SERVE_LOG="$(mktemp -t vibe-serve.XXXXXX.log)" || die "mktemp failed"
  echo "Starting opencode serve on port $SERVE_PORT (log: $SERVE_LOG) …"
  opencode serve --port "$SERVE_PORT" >"$SERVE_LOG" 2>&1 &
  SERVE_PID=$!; ATTACH="http://localhost:$SERVE_PORT"
  sleep 3
fi

{
  echo "=== vibe-walk started $(date -Iseconds) ==="
  echo "project=$PROJECT source=$SOURCE_REL branch=$BRANCH commits=${#SHAS[@]} worktree=$USE_WORKTREE classify=$CLASSIFY_ONLY commit=$AUTO_COMMIT"
} | tee -a "$WALK_LOG"

count=0; last_short=""
for sha in "${SHAS[@]}"; do
  [[ -v PROC["$sha"] ]] && continue
  [ "$LIMIT" -gt 0 ] && [ "$count" -ge "$LIMIT" ] && { echo "Reached --limit $LIMIT; stopping." | tee -a "$WALK_LOG"; break; }
  count=$((count+1)); last_short="${SHORT[$sha]}"
  run_one "$sha" 2>&1 | tee -a "$WALK_LOG"
done

# persist any trailing baseline advance that wasn't captured by a doc commit
commit_baseline_if_dirty "${last_short:-none}"

echo "=== summary ===" | tee -a "$WALK_LOG"
echo "sync baseline: $(jq -r '.baseline' "$SYNC")"
jq '{processed:(.processed|length), updated, skipped, failed, failures, last_run}' "$PROGRESS"
gitw log --oneline -5 2>/dev/null | sed 's/^/  /'
