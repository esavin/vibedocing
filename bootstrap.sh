#!/usr/bin/env bash
#
# vibedocing/bootstrap.sh — one-time setup of a documentation workspace.
#
# Usage (from the working folder where you unzipped the kit and cloned the project):
#     ./vibedocing/bootstrap.sh <path-to-project>
#
# What it does:
#   1. Creates .gitignore (project folder + this tooling folder + runtime dirs).
#   2. Creates agent/project/ (functions/, design/) seeded from templates.
#   3. Initializes a git repo in the CURRENT folder (if absent) and makes an initial commit.
#   4. Writes vibedocing/config.json tuned to your project.
#   5. Installs the opencode command + skill into .opencode/.
#
# After bootstrap, run:  ./vibedocing/run.sh --list   then   ./vibedocing/run.sh
#
set -euo pipefail

[ $# -ge 1 ] || { echo "Usage: $0 <path-to-project>" >&2; exit 1; }
PROJECT_ARG="$1"

PIPELINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(pwd)"
TPL="$PIPELINE_DIR/templates"
TODAY="$(date +%Y-%m-%d)"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing dependency: $1" >&2; exit 1; }; }
need git; need jq

# ---- resolve project ----
PROJECT_DIR="$(cd "$PROJECT_ARG" 2>/dev/null && pwd)" || { echo "project path not found: $PROJECT_ARG" >&2; exit 1; }
[ -d "$PROJECT_DIR/.git" ] || { echo "not a git repository: $PROJECT_DIR" >&2; exit 1; }
PROJECT_NAME="$(basename "$PROJECT_DIR")"

# is the project inside the workspace? -> gitignore its relative path; else absolute source_root
if [[ "$PROJECT_DIR" == "$WORK_DIR"/* ]]; then
  PROJECT_REL="$(realpath --relative-to="$WORK_DIR" "$PROJECT_DIR")"
  SOURCE_REL="$PROJECT_REL"
else
  PROJECT_REL=""
  SOURCE_REL="$PROJECT_DIR"   # absolute
fi

BRANCH="$(git -C "$PROJECT_DIR" symbolic-ref --quiet --short HEAD 2>/dev/null || git -C "$PROJECT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"

# ---- detect language / layout (best-effort; edit project-conventions.md to correct) ----
detect_lang() {
  local d="$PROJECT_DIR"
  LANGUAGE="unknown"; LAYOUT="unknown"
  if [ -f "$d/package.json" ]; then
    if find "$d" -maxdepth 3 -type f \( -name '*.ts' -o -name '*.tsx' \) -not -path '*/node_modules/*' 2>/dev/null | head -1 | grep -q .; then LANGUAGE="TypeScript"; else LANGUAGE="JavaScript"; fi
    LAYOUT="Node.js project"; [ -d "$d/packages" ] && LAYOUT="Node.js monorepo (packages/)"
  elif [ -f "$d/go.mod" ]; then LANGUAGE="Go"; LAYOUT="Go module"
  elif [ -f "$d/Cargo.toml" ]; then LANGUAGE="Rust"; LAYOUT="Cargo workspace/crate"
  elif [ -f "$d/pyproject.toml" ] || [ -f "$d/setup.py" ] || [ -f "$d/requirements.txt" ]; then LANGUAGE="Python"; LAYOUT="Python project"
  elif [ -f "$d/pom.xml" ] || ls "$d"/*.gradle "$d"/*.gradle.kts >/dev/null 2>&1; then LANGUAGE="Java/Kotlin"; LAYOUT="JVM (Gradle/Maven)"
  elif [ -f "$d/Gemfile" ]; then LANGUAGE="Ruby"; LAYOUT="Ruby project"
  elif [ -f "$d/composer.json" ]; then LANGUAGE="PHP"; LAYOUT="PHP project"
  elif ls "$d"/*.csproj "$d"/*.sln >/dev/null 2>&1; then LANGUAGE="C#"; LAYOUT=".NET project"
  elif [ -f "$d/Package.swift" ]; then LANGUAGE="Swift"; LAYOUT="Swift package"
  fi
}
detect_lang

# ---- render helper (bash substitution — no sed escaping issues) ----
render() { # <infile> <outfile>
  local content; content="$(cat "$1")"
  content="${content//@@PROJECT_NAME@@/$PROJECT_NAME}"
  content="${content//@@SOURCE_ROOT@@/$SOURCE_REL}"
  content="${content//@@BRANCH@@/$BRANCH}"
  content="${content//@@LANGUAGE@@/$LANGUAGE}"
  content="${content//@@LAYOUT@@/$LAYOUT}"
  content="${content//@@DATE@@/$TODAY}"
  printf '%s\n' "$content" > "$2"
}

echo ">> workspace:   $WORK_DIR"
echo ">> project:     $PROJECT_NAME  ($PROJECT_DIR)"
echo ">> source_root: $SOURCE_REL"
echo ">> branch:      $BRANCH   language: $LANGUAGE ($LAYOUT)"

# ---- 1. .gitignore ----
{
  echo "# The project under analysis — tracked by its own repository, not by this workspace."
  [ -n "$PROJECT_REL" ] && echo "/$PROJECT_REL/"
  echo
  echo "# The documentation solution tooling (local; not part of the documented output)."
  echo "/vibedocing/"
  echo "/.opencode/"
  echo
  echo "# Disposable git worktrees created by the walker (stateful replay)."
  echo "/.vibe-trees/"
} > "$WORK_DIR/.gitignore"
echo ">> wrote .gitignore"

# ---- 2. agent/project/ from templates ----
mkdir -p "$WORK_DIR/agent/project/functions" "$WORK_DIR/agent/project/design"
# update-documents.md is generic/portable and never user-customized, so a plain copy is
# correct. (cp -n returns non-zero on skip, which would trigger a fallback that overwrites.)
cp "$TPL/update-documents.md" "$WORK_DIR/agent/project/update-documents.md"
render "$TPL/project-conventions.md" "$WORK_DIR/agent/project/project-conventions.md"
render "$TPL/PROJECT.md" "$WORK_DIR/agent/project/PROJECT.md"
cat > "$WORK_DIR/agent/project/.vibedocing.json" <<JSON
{"project":"$PROJECT_NAME","source_root":"$SOURCE_REL","branch":"$BRANCH","baseline":"","documented_commits":0,"last_synced":null}
JSON
echo ">> seeded agent/project/ (PROJECT.md, update-documents.md, project-conventions.md, .vibedocing.json)"

# ---- 3. git init + initial commit ----
if [ ! -d "$WORK_DIR/.git" ]; then
  git -C "$WORK_DIR" init -q
  echo ">> initialized git repo"
else
  echo ">> git repo already present (continuing)"
fi
git -C "$WORK_DIR" config user.name >/dev/null 2>&1 || git -C "$WORK_DIR" config user.name "vibedocing"
git -C "$WORK_DIR" config user.email >/dev/null 2>&1 || git -C "$WORK_DIR" config user.email "vibedocing@local"
git -C "$WORK_DIR" add .gitignore agent/project
if ! git -C "$WORK_DIR" diff --cached --quiet >/dev/null 2>&1; then
  git -C "$WORK_DIR" commit -q -m "docs(${PROJECT_NAME}): initialize documentation workspace"
  echo ">> initial commit created"
else
  echo ">> nothing to commit (already initialized)"
fi

# ---- 4. config.json ----
render "$TPL/config.json" "$PIPELINE_DIR/config.json"
echo ">> wrote vibedocing/config.json"

# ---- 5. install opencode command + skill ----
mkdir -p "$WORK_DIR/.opencode/command" "$WORK_DIR/.opencode/skills"
[ -d "$PIPELINE_DIR/opencode/command" ] && cp -f "$PIPELINE_DIR"/opencode/command/* "$WORK_DIR/.opencode/command/" 2>/dev/null || true
[ -d "$PIPELINE_DIR/opencode/skills" ] && cp -rf "$PIPELINE_DIR"/opencode/skills/* "$WORK_DIR/.opencode/skills/" 2>/dev/null || true
echo ">> installed opencode command + skill into .opencode/"

cat <<EOF

=========================================================
 Workspace ready.
=========================================================
Next:
  ./vibedocing/run.sh --list | tail -1      # preview: how many commits to process
  ./vibedocing/run.sh --dry-run --limit 20  # classify only (no writes, no commits)
  ./vibedocing/run.sh --limit 20            # document first 20 commits (auto-committed)
  ./vibedocing/run.sh                       # continue from baseline to HEAD

Restart after syncing new upstream changes into '$SOURCE_REL':
  ./vibedocing/run.sh                       # resumes from the committed baseline

Edit per-project specifics any time:
  agent/project/project-conventions.md
  vibedocing/config.json
EOF
