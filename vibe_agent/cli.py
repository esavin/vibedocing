"""CLI entry point: one agent invocation = one commit.

Writes two verdict artifacts for the outer bash loop:
  verdicts/<sha>.json - full verdict (verdict, files, reason, steps, usage, validation)
  verdicts/<sha>.txt  - one line, backward-compatible with run.sh parsing:
                        VERDICT: DOC_UPDATED <files...> | NO_DOC | ERROR <reason>
plus, whenever docs were written:
  verdicts/<sha>.validation.md - the docs validation report (last state)

Pipeline per commit: build a grounded first message (commit meta, rename-aware
name-status, stale-doc worklist, tree digest for the root commit), run the agent
loop, then validate the docs and - if problems remain - hand them back to the agent
for up to `validation.rounds` repair rounds. In strict mode (default) remaining
errors flip the verdict to ERROR, so the commit is requeued and retried instead of
publishing broken docs.
"""

import argparse
import datetime
import json
import os
import sys

from .agent import run_agent
from .config import ConfigError, load_config, resolve_llm
from .llm import ChatClient, FatalLLMError
from .prompt import (InspectError, SYSTEM_PROMPT, build_first_user,
                     sha_looks_valid)
from .tools import ToolSet
from .transcript import Transcript
from .validate import format_report, repair_message, validate_docs


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="vibe-agent",
        description="Classify one project commit and update the docs map"
                    " (single step of the vibedocing pipeline).",
    )
    parser.add_argument("--config", default="", help="pipeline config.json path")
    parser.add_argument("--sha", required=True, help="commit under review")
    parser.add_argument("--worktree", required=True,
                        help="worktree with the commit checked out")
    parser.add_argument("--docs-root", required=True,
                        help="documentation root (writable)")
    parser.add_argument("--docs-root-rel", default="agent/project",
                        help="docs root relative to the workspace, for verdict paths")
    parser.add_argument("--verdicts-dir", required=True,
                        help="directory for verdict files")
    parser.add_argument("--classify-only", action="store_true",
                        help="decide and report without writing docs")
    parser.add_argument("--model", default="", help="override the configured model")
    parser.add_argument("--max-steps", type=int, default=0,
                        help="override the max agent steps")
    return parser.parse_args(argv)


def log(message):
    print("[vibe-agent] %s" % message, flush=True)


def read_conventions(docs_root):
    path = os.path.join(docs_root, "project-conventions.md")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(12000)
    except OSError:
        return ""


def validation_settings(config, classify_only):
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
    if classify_only or mode == "off":
        rounds = 0
    return mode, rounds, path_check


def repo_relative(path, docs_root, docs_root_rel):
    """Normalize a doc path reported by the model to workspace-relative."""
    text = str(path).strip().replace("\\", "/")
    docs_abs = os.path.realpath(docs_root).replace(os.sep, "/").rstrip("/")
    if text.startswith(docs_abs + "/"):
        text = text[len(docs_abs) + 1:]
    text = text.lstrip("./")
    prefix = docs_root_rel.strip("/")
    if prefix and text.startswith(prefix + "/"):
        return text
    return (prefix + "/" + text) if prefix else text


def verdict_line(verdict, docs_root, docs_root_rel):
    if verdict["verdict"] == "DOC_UPDATED":
        files = ",".join(repo_relative(f, docs_root, docs_root_rel)
                         for f in verdict.get("files") or [])
        return ("VERDICT: DOC_UPDATED " + files).rstrip()
    if verdict["verdict"] == "NO_DOC":
        return "VERDICT: NO_DOC"
    reason = " ".join(str(verdict.get("reason") or "unspecified").split())[:200]
    return "VERDICT: ERROR %s" % reason


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if not sha_looks_valid(args.sha):
        print("vibe-agent: --sha does not look like a commit hash", file=sys.stderr)
        return 2

    try:
        config = load_config(args.config)
        llm = resolve_llm(config, cli_model=args.model or None,
                          cli_max_steps=args.max_steps or None)
    except ConfigError as exc:
        print("vibe-agent: %s" % exc, file=sys.stderr)
        return 2

    docs_root = os.path.realpath(args.docs_root)
    worktree = os.path.realpath(args.worktree)
    mode = "classify-only" if args.classify_only else "document"
    today = datetime.date.today().isoformat()
    val_mode, val_rounds, path_check = validation_settings(config,
                                                           args.classify_only)

    tools = ToolSet(worktree, docs_root, classify_only=args.classify_only)
    client = ChatClient(
        base_url=llm["base_url"],
        api_key=llm["api_key"],
        model=llm["model"],
        timeout=llm["timeout"],
        retries=llm["retries"],
        temperature=llm["temperature"],
        max_tokens=llm["max_tokens"],
        extra_body=llm["extra_body"],
    )

    transcript = None
    if llm["log_transcript"]:
        try:
            os.makedirs(args.verdicts_dir, exist_ok=True)
            transcript = Transcript(os.path.join(
                args.verdicts_dir, args.sha + ".transcript.jsonl"))
        except OSError:
            transcript = None

    verdict = None
    old_paths = []
    root_commit = False
    changed = 0
    try:
        first_user, info = build_first_user(args.sha, worktree, docs_root, mode,
                                            today, read_conventions(docs_root))
        old_paths = info["old_paths"]
        root_commit = info["is_root"]
        changed = info.get("changed", 0)
    except InspectError as exc:
        verdict = {"verdict": "ERROR", "files": [], "reason":
                   "cannot-inspect-commit: %s" % exc, "steps": 0,
                   "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                             "total_tokens": 0}}
        if transcript is not None:
            transcript.record({"type": "end", "verdict": "ERROR",
                               "reason": verdict["reason"]})

    last_validation = None

    def validator():
        """Called by the agent loop after a DOC_UPDATED finish (repair rounds)."""
        problems = validate_docs(docs_root, worktree, old_paths, path_check)
        report_path = os.path.join(args.verdicts_dir,
                                   args.sha + ".validation.md")
        try:
            os.makedirs(args.verdicts_dir, exist_ok=True)
            write_atomic(report_path, format_report(problems, args.sha))
        except OSError:
            pass
        if problems["errors"] or problems["warnings"]:
            log("validation: %d error(s), %d warning(s)"
                % (len(problems["errors"]), len(problems["warnings"])))
        if problems["errors"]:
            return repair_message(problems, val_rounds)
        return None

    if verdict is None:
        max_steps = llm["max_steps"]
        boost = 0
        if root_commit and llm["max_steps_initial"] > 0:
            max_steps = llm["max_steps_initial"]
        elif changed:
            # doc-heavy commits (big moves touching many cited docs) need more
            # tool rounds: +1 step per 8 changed files, capped at max_steps_cap
            boost = min(max(0, llm["max_steps_cap"] - max_steps), changed // 8)
            max_steps += boost
        log("model=%s endpoint=%s mode=%s root_commit=%s steps<=%d%s validation=%s/%d"
            % (llm["model"], llm["base_url"], mode, root_commit, max_steps,
               (" (+%d for %d changed files)" % (boost, changed)) if boost else "",
               val_mode, val_rounds))
        if transcript is not None:
            transcript.record({
                "type": "session",
                "sha": args.sha,
                "model": llm["model"],
                "base_url": llm["base_url"],
                "mode": mode,
                "root_commit": root_commit,
                "max_steps": max_steps,
                "repair_rounds": val_rounds,
                "system_prompt_chars": len(SYSTEM_PROMPT),
                "first_user_chars": len(first_user),
            })
        try:
            verdict = run_agent(client, tools, SYSTEM_PROMPT, first_user,
                                max_steps, log,
                                validator=validator, repair_rounds=val_rounds,
                                transcript=transcript)
        except FatalLLMError as exc:
            verdict = {"verdict": "ERROR", "files": [], "reason":
                       "llm: %s" % exc, "steps": 0,
                       "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                                 "total_tokens": 0}}
            if transcript is not None:
                transcript.record({"type": "end", "verdict": "ERROR",
                                   "reason": verdict["reason"]})
        finally:
            if transcript is not None:
                transcript.close()

        # final validation state (the validator callback tracks the last run)
        if val_mode != "off" and not args.classify_only and tools.wrote_docs:
            problems = validate_docs(docs_root, worktree, old_paths, path_check)
            last_validation = {"errors": len(problems["errors"]),
                               "warnings": len(problems["warnings"]),
                               "report": args.sha + ".validation.md"}
            try:
                os.makedirs(args.verdicts_dir, exist_ok=True)
                write_atomic(os.path.join(args.verdicts_dir,
                                          args.sha + ".validation.md"),
                             format_report(problems, args.sha))
            except OSError:
                pass
            if (val_mode == "strict" and problems["errors"]
                    and verdict["verdict"] == "DOC_UPDATED"):
                verdict["verdict"] = "ERROR"
                verdict["reason"] = ("validation failed with %d error(s) - docs NOT "
                                     "published; see verdicts/%s.validation.md"
                                     % (len(problems["errors"]), args.sha))

    verdict["model"] = llm["model"]
    verdict["mode"] = mode
    verdict["sha"] = args.sha
    verdict["root_commit"] = root_commit
    if transcript is not None:
        verdict["transcript"] = os.path.basename(transcript.path)
    if last_validation is not None:
        verdict["validation"] = last_validation
    verdict["timestamp"] = datetime.datetime.now().isoformat(timespec="seconds")

    line = verdict_line(verdict, docs_root, args.docs_root_rel)
    try:
        os.makedirs(args.verdicts_dir, exist_ok=True)
        write_atomic(os.path.join(args.verdicts_dir, args.sha + ".json"),
                     json.dumps(verdict, ensure_ascii=False, indent=2) + "\n")
        write_atomic(os.path.join(args.verdicts_dir, args.sha + ".txt"),
                     line + "\n")
    except OSError as exc:
        print("vibe-agent: cannot write verdict: %s" % exc, file=sys.stderr)
        return 2

    usage = verdict.get("usage", {}).get("total_tokens", 0)
    log("done in %s steps, %s tokens: %s" % (verdict.get("steps", 0), usage, line))
    if verdict["verdict"] == "ERROR":
        if transcript is not None:
            log("to analyze: python3 -m vibe_agent.transcript %s" % transcript.path)
        elif not llm["log_transcript"]:
            log("(transcript disabled - set llm.log_transcript: true in config)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
