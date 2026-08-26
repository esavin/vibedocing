"""Snapshot bootstrap: document the CURRENT tree, skip the history.

For repositories with histories of thousands/tens of thousands of commits,
replaying every commit costs days or weeks of agent time. The snapshot mode
inverts the process:

  1. one PLANNER request (a single LLM call, no tools) partitions the tree
     into a handful of coherent MODULES; a mechanical top-level-directory
     partition is the fallback when the planner fails or returns garbage;
  2. one AGENT SESSION per module (the regular run_agent loop with the full
     guarded toolset, validator and repair rounds) writes that module's
     initial docs - module index + capability-cluster function docs + at most
     a couple of design docs. Small per-module contexts stay inside the
     model window no matter how big the repository is;
  3. the hub is reconciled deterministically between modules, the final map
     is validated, and the per-module verdict markers make an interrupted
     run resume where it stopped (re-running --snapshot skips finished
     modules).

run.sh then sets the committed baseline to the snapshot commit, so the next
regular run documents only commits NEWER than the snapshot (the incremental
mode the pipeline is designed for).

Verdict artifacts (in the pipeline's verdicts/ directory):
  <sha>~snapshot.json / .txt        - the aggregate snapshot verdict
  <sha>~snapshot~<module>.json      - per-module session markers (resume)
  <sha>~snapshot.transcript.jsonl   - the planner + every module session

Usage (run.sh --snapshot calls this):
  python3 -m vibe_agent.snapshot --config vibedocing/config.json \
      --ref <sha> --worktree <tree> --docs-root agent/project \
      --docs-root-rel agent/project --verdicts-dir vibedocing/verdicts
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

from .agent import run_agent
from .config import (ConfigError, load_config, resolve_llm, resolve_limits,
                     resolve_modules, resolve_snapshot)
from .hub import reconcile_navigation
from .llm import ChatClient, FatalLLMError
from .prompt import (MODULES_ADDENDUM, PLANNER_SYSTEM, SNAPSHOT_SYSTEM_PROMPT,
                     _tree_digest, build_planner_user, build_snapshot_user)
from .tools import ToolSet
from .transcript import Transcript
from .validate import format_report, repair_message, validate_docs

_MODULE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# directories that never carry user-facing capabilities worth a module
_SKIP_TOP = {".git", ".github", ".circleci", "node_modules", "vendor",
             "third_party", "thirdparty", "external", "build", "dist",
             "target", "out", "bin", "obj"}


def log(message):
    print("[vibe-snapshot] %s" % message, flush=True)


def read_conventions(docs_root):
    path = os.path.join(docs_root, "project-conventions.md")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(12000)
    except OSError:
        return ""


def _git(worktree, argv):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.run(["git", "--no-pager", "-C", worktree] + argv,
                          capture_output=True, text=True, errors="replace",
                          timeout=120, env=env)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "git error").strip()[:300])
    return proc.stdout


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return slug or "misc"


# ---- planner --------------------------------------------------------------


def _extract_json_array(text):
    """Best-effort extraction of the first JSON array in a model reply."""
    if not isinstance(text, str) or not text.strip():
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def _known_paths(worktree, ref):
    """Repository-root-relative directory AND file paths present at <ref>."""
    known = set()
    try:
        raw = _git(worktree, ["ls-tree", "-r", "--name-only", ref])
    except RuntimeError:
        return known
    for name in raw.splitlines():
        parts = name.split("/")
        for i in range(len(parts)):
            known.add("/".join(parts[:i + 1]))
    return known


def sanitize_plan(plan, worktree, ref, max_modules):
    """Validate a planner reply against the real tree: drop unknown paths,
    fix slugs, dedupe, cap the module count (excess merges into 'misc')."""
    known = _known_paths(worktree, ref)
    seen = set()
    modules = []
    for item in plan:
        if not isinstance(item, dict):
            continue
        slug = _slugify(item.get("module") or item.get("name") or "")
        paths = [str(p).strip("/") for p in (item.get("paths") or [])
                 if isinstance(p, str) and p.strip("/")]
        valid = [p for p in paths if p in known]
        if not slug or slug in seen or not valid:
            continue
        seen.add(slug)
        modules.append({
            "module": slug,
            "title": str(item.get("title") or slug)[:120],
            "paths": valid[:12],
            "summary": str(item.get("summary") or "")[:400],
        })
    if len(modules) > max_modules:
        kept = modules[:max_modules - 1]
        misc_paths = []
        for m in modules[max_modules - 1:]:
            misc_paths.extend(p for p in m["paths"] if p not in misc_paths)
        kept.append({"module": "misc", "title": "Miscellaneous",
                     "paths": misc_paths[:24],
                     "summary": "Remaining areas merged by the module cap."})
        modules = kept
    return modules


def mechanical_plan(worktree, ref, max_modules):
    """Fallback partition when the planner LLM is unavailable/garbage:
    top-level directories, or second-level when one directory dominates
    (opencv-style modules/<name>, monorepo packages/<name>)."""
    counts = {}
    try:
        raw = _git(worktree, ["ls-tree", "-r", "--name-only", ref])
    except RuntimeError:
        return []
    names = raw.splitlines()
    for name in names:
        top = name.split("/")[0]
        if top in _SKIP_TOP or top.startswith("."):
            continue
        counts[top] = counts.get(top, 0) + 1
    total = sum(counts.values()) or 1
    biggest = max(counts, key=lambda k: counts[k]) if counts else None
    modules = []
    if biggest and counts[biggest] >= total * 0.6:
        base = biggest
        sub = {}
        for name in names:
            parts = name.split("/")
            if len(parts) >= 2 and parts[0] == base:
                sub.setdefault(parts[1], 0)
                sub[parts[1]] += 1
        if len(sub) >= 6:
            for name, count in sorted(sub.items(), key=lambda kv: (-kv[1], kv[0])):
                if name in _SKIP_TOP or name.startswith(".") or count < 3:
                    continue
                modules.append({"module": _slugify(name), "title": name,
                                "paths": ["%s/%s" % (base, name)],
                                "summary": ""})
            modules.append({"module": _slugify(base) + "-misc",
                            "title": "%s (misc)" % base,
                            "paths": [base], "summary": ""})
    if not modules:
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            modules.append({"module": _slugify(name), "title": name,
                            "paths": [name], "summary": ""})
    if len(modules) > max_modules:
        kept = modules[:max_modules - 1]
        kept.append({"module": "misc", "title": "Miscellaneous",
                     "paths": [], "summary": ""})
        modules = kept
    return modules


def plan_modules(planner_client, worktree, ref, conventions, limits,
                 max_modules):
    """Planner LLM request with a mechanical fallback."""
    digest = _tree_digest(worktree, ref)
    log("planner: partitioning the tree into modules (model=%s)"
        % planner_client.model)
    reply = None
    try:
        response = planner_client.chat(
            [{"role": "system", "content": PLANNER_SYSTEM},
             {"role": "user",
              "content": build_planner_user(digest, conventions)}],
            [])
        reply = response.get("content")
    except FatalLLMError as exc:
        log("planner request failed (%s) - falling back to the mechanical "
            "partition" % exc)
    plan = _extract_json_array(reply) if reply else None
    modules = []
    if plan is not None:
        modules = sanitize_plan(plan, worktree, ref, max_modules)
        if not modules:
            log("planner reply unusable - falling back to the mechanical "
                "partition")
    if not modules:
        modules = mechanical_plan(worktree, ref, max_modules)
    return modules, bool(plan and modules)


# ---- per-module session ---------------------------------------------------


def module_marker_path(verdicts_dir, ref, module):
    return os.path.join(verdicts_dir,
                        "%s~snapshot~%s.json" % (ref, module["module"]))


def run_module_session(client, module, ref, worktree, docs_root, conventions,
                       limits, max_steps, val_rounds, path_check,
                       modules_layout, transcript):
    """One run_agent session scoped to a module. Returns the verdict dict."""
    today = datetime.date.today().isoformat()
    tools = ToolSet(worktree, docs_root, classify_only=False, limits=limits)
    first_user = build_snapshot_user(module, ref, worktree, docs_root, today,
                                     conventions, limits=limits,
                                     modules_layout=modules_layout)
    # snapshot sessions always use the snapshot-flavored system prompt: the
    # per-commit classification rules do not apply to a tree bootstrap
    prompt = (SNAPSHOT_SYSTEM_PROMPT + MODULES_ADDENDUM) \
        if modules_layout else SNAPSHOT_SYSTEM_PROMPT

    def validator(_scope=None):
        problems = validate_docs(docs_root, worktree, (), path_check)
        if problems["errors"]:
            return repair_message(problems, val_rounds)
        return None

    def session_log(message):
        # per-step lines streamed live must say WHICH module is working
        log("module %s: %s" % (module["module"], message))

    verdict = run_agent(client, tools, prompt, first_user, max_steps,
                        session_log,
                        validator=validator, repair_rounds=val_rounds,
                        transcript=transcript, limits=limits)
    if tools.wrote_docs and verdict.get("verdict") == "NO_DOC":
        verdict["verdict"] = "DOC_UPDATED"
    verdict["module"] = module["module"]
    return verdict


# ---- CLI ------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="vibe-agent-snapshot",
        description="Bootstrap the docs map from the CURRENT tree at a commit "
                    "(module-partitioned), skipping history replay.")
    parser.add_argument("--config", default="", help="pipeline config.json path")
    parser.add_argument("--ref", required=True,
                        help="snapshot commit (tree state to document)")
    parser.add_argument("--worktree", required=True,
                        help="worktree with the snapshot commit checked out")
    parser.add_argument("--docs-root", required=True,
                        help="documentation root (writable)")
    parser.add_argument("--docs-root-rel", default="agent/project",
                        help="docs root relative to the workspace")
    parser.add_argument("--verdicts-dir", required=True,
                        help="directory for verdict files")
    parser.add_argument("--model", default="", help="override the agent model")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore previous per-module markers and re-run "
                             "every module")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        config = load_config(args.config)
        llm = resolve_llm(config, cli_model=args.model or None)
        limits = resolve_limits(config)
        snap = resolve_snapshot(config, llm)
        modules_on = resolve_modules(config)
    except ConfigError as exc:
        print("vibe-snapshot: %s" % exc, file=sys.stderr)
        return 2

    docs_root = os.path.realpath(args.docs_root)
    worktree = os.path.realpath(args.worktree)
    ref = args.ref.strip()
    conventions = read_conventions(docs_root)
    val_mode, val_rounds, path_check = _validation_settings(config)

    client = ChatClient(
        base_url=llm["base_url"], api_key=llm["api_key"], model=llm["model"],
        timeout=llm["timeout"], retries=llm["retries"],
        temperature=llm["temperature"], max_tokens=llm["max_tokens"],
        extra_body=llm["extra_body"],
        heartbeat_seconds=llm["heartbeat_seconds"])
    planner_client = ChatClient(
        base_url=llm["base_url"], api_key=llm["api_key"],
        model=snap["planner_model"], timeout=llm["timeout"],
        retries=llm["retries"], temperature=0.2, max_tokens=0,
        extra_body=llm["extra_body"],
        heartbeat_seconds=llm["heartbeat_seconds"])

    transcript = None
    if llm["log_transcript"]:
        try:
            os.makedirs(args.verdicts_dir, exist_ok=True)
            transcript = Transcript(os.path.join(
                args.verdicts_dir, ref + "~snapshot.transcript.jsonl"))
        except OSError:
            transcript = None

    max_steps = snap["max_steps"] or llm["max_steps_initial"] or 48
    log("model=%s planner=%s ref=%s modules_layout=%s steps<=%d "
        "validation=%s/%d"
        % (llm["model"], snap["planner_model"], ref[:12], modules_on,
           max_steps, val_mode, val_rounds))

    if transcript is not None:
        transcript.record({"type": "session", "mode": "snapshot", "ref": ref,
                           "model": llm["model"],
                           "planner_model": snap["planner_model"],
                           "modules_layout": modules_on,
                           "max_steps": max_steps})

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    steps = 0
    files = []
    documented = []
    failed = []

    try:
        modules, planned = plan_modules(planner_client, worktree, ref,
                                        conventions, limits,
                                        snap["max_modules"])
        if not modules:
            raise FatalLLMError("no modules to document (empty tree?)")
        log("module plan (%s): %s"
            % ("planner" if planned else "mechanical fallback",
               ", ".join(m["module"] for m in modules)))
        if transcript is not None:
            transcript.record({"type": "plan", "source": "planner" if planned
                               else "mechanical", "modules": modules})

        for index, module in enumerate(modules, 1):
            tag = "module %s (%d/%d)" % (module["module"], index, len(modules))
            marker = module_marker_path(args.verdicts_dir, ref, module)
            if not args.fresh and os.path.isfile(marker):
                try:
                    with open(marker, "r", encoding="utf-8") as fh:
                        prior = json.load(fh)
                    if prior.get("verdict") != "ERROR":
                        log("%s: done in a previous run - skipping" % tag)
                        for key in usage:
                            usage[key] += int((prior.get("usage") or {})
                                              .get(key) or 0)
                        continue
                except (OSError, json.JSONDecodeError):
                    pass
            log("%s: documenting (paths: %s)"
                % (tag, ", ".join(module["paths"]) or "(none)"))
            try:
                verdict = run_module_session(
                    client, module, ref, worktree, docs_root, conventions,
                    limits, max_steps, val_rounds, path_check, modules_on,
                    transcript)
            except FatalLLMError as exc:
                verdict = {"verdict": "ERROR", "files": [], "reason":
                           "llm: %s" % exc, "steps": 0,
                           "usage": {"prompt_tokens": 0,
                                     "completion_tokens": 0,
                                     "total_tokens": 0}}
            verdict["paths"] = module["paths"]
            try:
                os.makedirs(args.verdicts_dir, exist_ok=True)
                _write_atomic(marker, json.dumps(verdict,
                                                 ensure_ascii=False,
                                                 indent=2) + "\n")
            except OSError:
                pass
            for key in usage:
                usage[key] += int((verdict.get("usage") or {}).get(key) or 0)
            steps += int(verdict.get("steps") or 0)
            for item in verdict.get("files") or []:
                if item and item not in files:
                    files.append(item)
            if verdict["verdict"] == "ERROR":
                failed.append(module["module"])
                log("%s: ERROR - %s"
                    % (tag, verdict.get("reason", "")[:160]))
            else:
                documented.append(module["module"])
                log("%s: %s (%d file(s), %d tokens)"
                    % (tag, verdict["verdict"],
                       len(verdict.get("files") or []),
                       int((verdict.get("usage") or {}).get("total_tokens")
                           or 0)))
            # keep the hub complete between modules (cheap, deterministic)
            try:
                summary = reconcile_navigation(docs_root)
                if summary:
                    log("hub: %s" % summary)
            except OSError:
                pass
    except FatalLLMError as exc:
        _final(args, ref, "ERROR", usage, steps, files, documented, failed,
               transcript, reason="llm: %s" % exc)
        return 1

    # final whole-map validation (mechanical only)
    last_validation = None
    if val_mode != "off":
        problems = validate_docs(docs_root, worktree, (), path_check)
        last_validation = {"errors": len(problems["errors"]),
                           "warnings": len(problems["warnings"])}
        try:
            os.makedirs(args.verdicts_dir, exist_ok=True)
            _write_atomic(os.path.join(args.verdicts_dir,
                                       ref + "~snapshot.validation.md"),
                          format_report(problems, ref))
        except OSError:
            pass
        if val_mode == "strict" and problems["errors"] and not failed:
            failed.append("(strict validation)")
            log("strict validation failed with %d error(s) - see "
                "verdicts/%s~snapshot.validation.md"
                % (len(problems["errors"]), ref))

    verdict = "ERROR" if failed else ("DOC_UPDATED" if files else "NO_DOC")
    _final(args, ref, verdict, usage, steps, files, documented, failed,
           transcript, last_validation=last_validation)
    log("snapshot %s: %d/%d module(s) documented, %d doc file(s), %d tokens"
        % (verdict, len(documented), len(documented) + len(failed),
           len(files), usage["total_tokens"]))
    return 0 if verdict != "ERROR" else 1


def _validation_settings(config):
    section = config.get("validation")
    if not isinstance(section, dict):
        section = {}
    mode = str(section.get("mode") or "strict").lower()
    if mode not in ("strict", "warn", "off"):
        mode = "strict"
    try:
        rounds = max(0, int(section.get("rounds", 2)))
    except (TypeError, ValueError):
        rounds = 2
    path_check = str(section.get("path_check") or "error").lower()
    if path_check not in ("error", "warn", "off"):
        path_check = "error"
    return mode, (0 if mode == "off" else rounds), path_check


def _final(args, ref, verdict, usage, steps, files, documented, failed,
           transcript, reason="", last_validation=None):
    if transcript is not None:
        transcript.close()
    record = {
        "verdict": verdict,
        "mode": "snapshot",
        "sha": ref,
        "modules": {"documented": documented, "failed": failed},
        "files": files,
        "reason": reason or ("snapshot bootstrap at %s" % ref[:12]),
        "steps": steps,
        "usage": usage,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    if last_validation is not None:
        record["validation"] = last_validation
    line = ("VERDICT: ERROR %s" % reason) if verdict == "ERROR" else \
           ("VERDICT: %s%s" % (verdict,
                               (" " + ",".join(files)) if files else ""))
    try:
        os.makedirs(args.verdicts_dir, exist_ok=True)
        _write_atomic(os.path.join(args.verdicts_dir, ref + "~snapshot.json"),
                      json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        _write_atomic(os.path.join(args.verdicts_dir, ref + "~snapshot.txt"),
                      line[:400] + "\n")
    except OSError as exc:
        print("vibe-snapshot: cannot write verdict: %s" % exc,
              file=sys.stderr)
    return record


if __name__ == "__main__":
    sys.exit(main())
