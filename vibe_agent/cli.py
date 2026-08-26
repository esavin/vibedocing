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
from .config import (ConfigError, load_config, resolve_llm, resolve_limits,
                     resolve_modules)
from .hygiene import hygiene_plan
from .llm import ChatClient, FatalLLMError
from .prompt import (InspectError, SYSTEM_PROMPT, build_first_user,
                     build_reconsider_message, load_prior_hint, sha_looks_valid,
                     system_prompt)
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
    parser.add_argument("--prior-verdict", default="",
                        help="verdict JSON from a previous run (DOC_UPDATED "
                             "hint; enables the reconsideration round)")
    parser.add_argument("--prior-docs", default="",
                        help="docs root of the previous run, to read the hint "
                             "docs from")
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
        limits = resolve_limits(config)
    except ConfigError as exc:
        print("vibe-agent: %s" % exc, file=sys.stderr)
        return 2

    docs_root = os.path.realpath(args.docs_root)
    worktree = os.path.realpath(args.worktree)
    mode = "classify-only" if args.classify_only else "document"
    today = datetime.date.today().isoformat()
    val_mode, val_rounds, path_check = validation_settings(config,
                                                           args.classify_only)
    sys_prompt = system_prompt(resolve_modules(config))

    tools = ToolSet(worktree, docs_root, classify_only=args.classify_only,
                    limits=limits)
    client = ChatClient(
        base_url=llm["base_url"],
        api_key=llm["api_key"],
        model=llm["model"],
        timeout=llm["timeout"],
        retries=llm["retries"],
        temperature=llm["temperature"],
        max_tokens=llm["max_tokens"],
        extra_body=llm["extra_body"],
        heartbeat_seconds=llm["heartbeat_seconds"],
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
    plan = None
    try:
        # deterministic path hygiene FIRST (mode=document only): citations of
        # RENAMED paths are rewritten in place before any LLM call, and a
        # remaining repair worklist bigger than hygiene_batch_docs docs is
        # split into per-batch sessions (hygiene.py)
        if not args.classify_only:
            plan = hygiene_plan(worktree, args.sha, docs_root,
                                batch_docs=int(limits.get("hygiene_batch_docs")
                                               or 5))
            if plan["prepass_edited"]:
                log("rename pre-pass: %d citation(s) rewritten across %d doc(s)"
                    " (deterministic, no LLM)"
                    % (sum(plan["prepass_edited"].values()),
                       len(plan["prepass_edited"])))
        first_user, info = build_first_user(args.sha, worktree, docs_root, mode,
                                            today, read_conventions(docs_root),
                                            limits=limits)
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

    # --doc-hints: prior run documented this commit; if the agent finishes
    # NO_DOC, feed the prior run's actual docs back for one reconsideration
    # round (see agent.run_agent).
    reconsider = None
    reconsider_state = {"fired": False}
    if args.prior_verdict and args.prior_docs:
        prior_hint = load_prior_hint(args.prior_verdict, args.prior_docs,
                                     args.docs_root_rel)
        if prior_hint is not None:
            def reconsider(hint=prior_hint):
                message = build_reconsider_message(hint,
                                                   args.classify_only)
                reconsider_state["fired"] = bool(message)
                return message
            log("prior-run hint loaded: %d doc(s) eligible for reconsideration"
                % len(prior_hint["docs"]))

    def validator(scope_docs=None):
        """Called by the agent loop after a DOC_UPDATED finish (repair rounds).

        With scope_docs (a path-hygiene batch session) only that batch's docs
        are checked and fed back: other batches' stale paths belong to their
        own sessions and would be pure repair noise here."""
        problems = validate_docs(docs_root, worktree, old_paths, path_check)
        if scope_docs is not None:
            scope = set(scope_docs)
            problems = {
                "errors": [e for e in problems["errors"]
                           if e.split(":", 1)[0].strip() in scope],
                "warnings": [w for w in problems["warnings"]
                             if w.split(":", 1)[0].strip() in scope],
            }
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
        batches = (plan or {}).get("batches") or []
        stale_by_doc = (plan or {}).get("stale_by_doc") or {}
        if root_commit and llm["max_steps_initial"] > 0:
            max_steps = llm["max_steps_initial"]
        elif changed and not batches and not (
                plan and (plan["prepass_edited"] or plan["stale_by_doc"])):
            # doc-heavy commits (big moves touching many cited docs) need more
            # tool rounds: +1 step per 8 changed files, capped at max_steps_cap.
            # NOT applied when path hygiene ran (pre-pass or a batched
            # worklist): the repair workload is handled by its own mechanical
            # pass / batch sessions, and a giant changed-file count then only
            # buys the classification session room to wander.
            boost = min(max(0, llm["max_steps_cap"] - max_steps), changed // 8)
            max_steps += boost
        limits_label = limits["profile"]
        if limits["compact_threshold_tokens"]:
            limits_label += " (compact >= %d tokens)" % limits["compact_threshold_tokens"]
        if batches:
            limits_label += (" hygiene=%d doc(s) in %d batch(es)"
                             % (len(stale_by_doc), len(batches)))
        log("model=%s endpoint=%s mode=%s root_commit=%s steps<=%d%s validation=%s/%d limits=%s"
            % (llm["model"], llm["base_url"], mode, root_commit, max_steps,
               (" (+%d for %d changed files)" % (boost, changed)) if boost else "",
               val_mode, val_rounds, limits_label))

        def record_session(extra=None):
            if transcript is None:
                return
            record = {
                "type": "session",
                "sha": args.sha,
                "model": llm["model"],
                "base_url": llm["base_url"],
                "mode": mode,
                "root_commit": root_commit,
                "max_steps": max_steps,
                "repair_rounds": val_rounds,
                "limits_profile": limits["profile"],
                "compact_threshold_tokens": limits["compact_threshold_tokens"],
                "system_prompt_chars": len(sys_prompt),
                "first_user_chars": len(first_user),
            }
            if extra:
                record.update(extra)
            transcript.record(record)

        record_session()
        session_verdicts = []
        wrote_docs = False
        try:
            # path-hygiene batches: one fresh, small session per batch of the
            # repair worklist (giant single sessions blow the context window)
            for index, batch in enumerate(batches):
                batch_docs = tuple(sorted(batch))
                focus_user, _info = build_first_user(
                    args.sha, worktree, docs_root, mode, today,
                    read_conventions(docs_root), limits=limits,
                    focus={"batch": index + 1, "batches": len(batches),
                           "stale": batch})
                batch_tools = ToolSet(worktree, docs_root,
                                      classify_only=args.classify_only,
                                      limits=limits)
                record_session({"batch": "%d/%d" % (index + 1, len(batches)),
                                "focus_docs": list(batch_docs),
                                "first_user_chars": len(focus_user),
                                "max_steps": llm["max_steps"]})
                log("hygiene batch %d/%d: %s" % (index + 1, len(batches),
                                                 ", ".join(batch_docs)))
                session_verdicts.append(run_agent(
                    client, batch_tools, sys_prompt, focus_user,
                    llm["max_steps"], log,
                    validator=lambda docs=batch_docs: validator(docs),
                    repair_rounds=val_rounds, transcript=transcript,
                    limits=limits))
                wrote_docs = wrote_docs or batch_tools.wrote_docs

            # main session: classification as usual. After batches the docs
            # changed, so the first message is rebuilt (its stale-docs
            # worklist must reflect the post-repair state, not the old one).
            if batches:
                first_user, _info = build_first_user(
                    args.sha, worktree, docs_root, mode, today,
                    read_conventions(docs_root), limits=limits)
            verdict = run_agent(client, tools, sys_prompt, first_user,
                                max_steps, log,
                                validator=validator, repair_rounds=val_rounds,
                                transcript=transcript, reconsider=reconsider,
                                limits=limits)
            session_verdicts.append(verdict)
            wrote_docs = wrote_docs or tools.wrote_docs
        except FatalLLMError as exc:
            verdict = {"verdict": "ERROR", "files": [], "reason":
                       "llm: %s" % exc, "steps": 0,
                       "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                                 "total_tokens": 0}}
            session_verdicts.append(verdict)
            if transcript is not None:
                transcript.record({"type": "end", "verdict": "ERROR",
                                   "reason": verdict["reason"]})
        finally:
            if transcript is not None:
                transcript.close()

        # aggregate the pre-pass + every session into ONE commit verdict
        prepass_edited = (plan or {}).get("prepass_edited") or {}
        files = list(prepass_edited)
        for session in session_verdicts:
            for item in session.get("files") or []:
                if item and item not in files:
                    files.append(item)
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        steps = 0
        for session in session_verdicts:
            for key in usage:
                usage[key] += int((session.get("usage") or {}).get(key) or 0)
            steps += int(session.get("steps") or 0)
        failed = [s for s in session_verdicts if s["verdict"] == "ERROR"]
        verdict = {
            "verdict": session_verdicts[-1]["verdict"] if session_verdicts
                       else "ERROR",
            "files": files,
            "reason": (session_verdicts[-1].get("reason") or "")
                      if session_verdicts else "no session ran",
            "steps": steps,
            "usage": usage,
        }
        if failed:
            verdict["verdict"] = "ERROR"
            verdict["files"] = []
            verdict["reason"] = failed[0].get("reason") or "session failed"
        elif files and verdict["verdict"] == "NO_DOC":
            # docs changed this invocation (pre-pass / batches) - never report
            # NO_DOC with dirty docs on disk
            verdict["verdict"] = "DOC_UPDATED"
        if len(session_verdicts) > 1 or prepass_edited:
            note = ("path hygiene: %d deterministic rewrite(s) in %d doc(s), "
                    "%d agent session(s)"
                    % (sum(prepass_edited.values()), len(prepass_edited),
                       len(session_verdicts)))
            if verdict["reason"]:
                verdict["reason"] = note + "; " + verdict["reason"]
            else:
                verdict["reason"] = note

        # final validation state (the validator callback tracks the last run);
        # gated on ANY docs change this invocation - agent sessions, the
        # deterministic rename pre-pass, or both
        if (val_mode != "off" and not args.classify_only
                and (wrote_docs or prepass_edited)):
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
    if reconsider_state["fired"]:
        verdict["reconsidered"] = True
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
