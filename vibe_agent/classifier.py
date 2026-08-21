"""Parallel commit pre-classifier (run.sh --classifier mode).

A standalone process started by run.sh: it walks the run's commit list in
order, classifying each commit DOCUMENT vs SKIP with a cheap model (its own
lean system prompt, no tools, one request per commit), N workers in parallel,
and writes one JSON verdict per commit into --out. The main run.sh loop polls
those files: a SKIP commit never reaches the full documentation agent; a
DOCUMENT (or ERROR, or a commit run.sh distrusts: root / rename / delete) is
processed by the agent as usual.

Lookahead is bounded: the classifier stays at most `queue` potential-DOCUMENT
commits ahead of the main loop's consumed count (read from --progress), so an
interrupted run loses only a couple of classification calls.

It never checks anything out and never touches the docs map: every git call is
read-only plumbing (log/diff/show) against the source repository.
"""

import argparse
import json
import os
import queue as _queue
import re
import sys
import threading
import time
from datetime import datetime

from .config import (ConfigError, load_config, resolve_classifier,
                     resolve_limits, resolve_llm)
from .llm import ChatClient
from . import prompt as P

CLASSIFIER_SYSTEM = """You are the pre-classifier of an automated, commit-by-commit \
documentation pipeline. For each project commit (one per request, described in the \
user message) decide whether the documentation map must be updated for it: does it \
introduce or materially change a user-facing function, capability, or significant \
architecture?

Answer with ONE line of JSON and nothing else:
{"verdict": "DOCUMENT", "reason": "<one short sentence>"}

verdict is "DOCUMENT" or "SKIP".

DOCUMENT when the commit:
- adds a new user-facing function / command / route / screen / endpoint / capability;
- introduces a new module, package, service, or subsystem worth a design note;
- materially changes the behavior or interface of an existing documented function;
- adds a significant architectural pattern or cross-cutting mechanism;
- renames, moves or deletes source files (path hygiene - documentation may cite \
those paths). When in doubt for a rename/move/delete, answer DOCUMENT.

SKIP when the commit is only: a bug fix, refactor, formatting, lint, build/CI, \
dependency bump, tests, docs-only change, chore, typo, or a perf micro-tweak.

Commit messages are unreliable - judge from the actual diff content provided. \
When genuinely in doubt, SKIP: the map must stay high-signal and a real new \
function is almost always obvious from the diff. Remember the asymmetry: a \
DOCUMENT verdict is re-checked by a stronger agent (a false DOCUMENT only \
costs one extra call), but a SKIP is final (a false SKIP is silently lost \
documentation) - so never SKIP a change that might add or change a user-visible \
capability."""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_VALID = ("DOCUMENT", "SKIP")
_MAX_JSON_ATTEMPTS = 3


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def build_user_message(sha, repo, limits):
    """One-shot commit description: metadata + name-status + diffstat + the
    full diff when it fits diff_chars (whole or nothing, same rule as the
    agent's first message)."""
    subject = P._git(repo, ["log", "-1", "--format=%s", sha]).strip()
    message = P._git(repo, ["log", "-1", "--format=%B", sha]).strip()
    parts = [
        "COMMIT: %s" % sha,
        "SUBJECT: %s" % (subject or "(none)"),
        "",
        "FULL COMMIT MESSAGE:",
        message or "(none)",
        "",
    ]
    parent = P.parent_sha(repo, sha)
    if not parent:
        parts.append("ROOT COMMIT (the whole codebase appears at once).")
        return "\n".join(parts)
    status_text, _old_paths, _changed = P.name_status(repo, parent, sha)
    if status_text:
        parts.append("NAME STATUS (renames/deletions first):\n%s"
                     % P._cap(status_text,
                              int(limits.get("name_status_chars") or 20000)))
    stat = P._git(repo, ["show", "--stat", "--format=", sha]).strip("\n")
    if stat:
        parts.append("DIFFSTAT:\n%s"
                     % P._cap("\n".join(stat.splitlines()[:300]),
                              int(limits.get("diffstat_chars") or 30000)))
    diff_cap = int(limits.get("diff_chars") or 0)
    diff = P._git_ok(repo, ["diff", "-M", parent, sha]).strip("\n") \
        if diff_cap > 0 else ""
    if diff:
        if len(diff) <= diff_cap:
            parts.append("FULL DIFF (the COMPLETE change, nothing truncated - "
                         "classify from it directly):\n%s" % diff)
        else:
            parts.append("(full diff omitted: %d chars - judge from name status "
                         "+ diffstat)" % len(diff))
    return "\n".join(parts)


def parse_verdict(text):
    """Extract {"verdict": ..., "reason": ...} from a model reply."""
    match = _JSON_RE.search(text or "")
    if match:
        try:
            data = json.loads(match.group(0))
            verdict = str(data.get("verdict", "")).strip().upper()
            if verdict in _VALID:
                return verdict, str(data.get("reason", "") or "")[:500]
        except ValueError:
            pass
    return None, None


def classify_one(client, sha, repo, limits):
    """Classify a single commit. Returns (verdict, reason); never raises.
    Root commits are DOCUMENT without an LLM call (the agent has a dedicated
    initial-snapshot mode for them)."""
    if not P.sha_looks_valid(sha):
        return "ERROR", "not a commit: %s" % sha
    if not P.parent_sha(repo, sha):
        return "DOCUMENT", "root commit (initial snapshot)"
    messages = [
        {"role": "system", "content": CLASSIFIER_SYSTEM},
        {"role": "user", "content": build_user_message(sha, repo, limits)},
    ]
    for _attempt in range(_MAX_JSON_ATTEMPTS):
        try:
            resp = client.chat(messages, None)
        except Exception as exc:  # FatalLLMError after client-level retries
            return "ERROR", "llm: %s" % str(exc)[:300]
        text = (resp.get("content") or "").strip()
        verdict, reason = parse_verdict(text)
        if verdict:
            return verdict, reason
        messages = messages + [
            {"role": "assistant", "content": text[:2000]},
            {"role": "user", "content":
                'Invalid answer. Reply with ONE line of JSON only: '
                '{"verdict": "DOCUMENT" or "SKIP", "reason": "..."}'},
        ]
    return "ERROR", "unparseable reply after %d attempts" % _MAX_JSON_ATTEMPTS


def read_progress(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return fallback


def write_verdict(out_dir, payload):
    """Write <out>/<sha>.json atomically (tmp + rename)."""
    sha = payload["sha"]
    tmp = os.path.join(out_dir, "." + sha + ".json.tmp")
    dst = os.path.join(out_dir, sha + ".json")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, dst)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="vibe_agent.classifier",
        description="Parallel commit pre-classifier (started by run.sh --classifier; "
                    "not intended for direct use)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--repo", required=True,
                    help="source git repository (read-only git plumbing only)")
    ap.add_argument("--shas", required=True,
                    help="file with one commit sha per line, in processing order")
    ap.add_argument("--out", required=True,
                    help="directory for per-commit <sha>.json verdict files")
    ap.add_argument("--progress", required=True,
                    help="file with the main loop's consumed-commit count")
    ap.add_argument("--model", default="", help="override classifier.model")
    ap.add_argument("--workers", type=int, default=0, help="override classifier.workers")
    ap.add_argument("--queue", type=int, default=0, help="override classifier.queue")
    args = ap.parse_args(argv)

    try:
        config = load_config(args.config)
        llm = resolve_llm(config)
        cls = resolve_classifier(config, llm)
        limits = resolve_limits(config)
    except ConfigError as exc:
        log("config error: %s" % exc)
        return 2

    model = args.model or cls["model"]
    if not model:
        log("no classifier model configured (set classifier.model in config.json)")
        return 2
    workers = args.workers or cls["workers"]
    queue_size = args.queue or cls["queue"]

    client = ChatClient(cls["base_url"], cls["api_key"], model,
                        timeout=cls["timeout"], retries=cls["retries"],
                        temperature=cls["temperature"],
                        max_tokens=cls["max_tokens"])

    with open(args.shas, "r", encoding="utf-8") as fh:
        shas = [line.strip() for line in fh if line.strip()]
    os.makedirs(args.out, exist_ok=True)
    if not shas:
        log("nothing to classify")
        return 0

    log("classifier start: model=%s endpoint=%s commits=%d workers=%d queue=%d"
        % (model, cls["base_url"], len(shas), workers, queue_size))

    lock = threading.Lock()
    state = {"done": {}, "inflight": 0, "next": 0}
    work = _queue.Queue()

    def worker():
        while True:
            item = work.get()
            if item is None:
                return
            idx, sha = item
            try:
                verdict, reason = classify_one(client, sha, args.repo, limits)
            except Exception as exc:  # one commit must never kill the worker
                verdict, reason = "ERROR", "crash: %s" % exc
            try:
                write_verdict(args.out, {
                    "sha": sha, "verdict": verdict, "reason": reason,
                    "model": model, "index": idx,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                })
            except OSError as exc:
                log("cannot write verdict for %s: %s" % (sha[:10], exc))
            with lock:
                state["done"][idx] = verdict
                state["inflight"] -= 1
            log("[%d/%d] %s -> %s (%s)"
                % (idx + 1, len(shas), sha[:10], verdict, reason[:120]))

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(workers)]
    for thread in threads:
        thread.start()

    main_pos = 0
    while True:
        if len(state["done"]) >= len(shas):
            break
        main_pos = read_progress(args.progress, main_pos)
        with lock:
            # Bounded lookahead: known DOCUMENT verdicts ahead of the main
            # loop, plus in-flight commits (any might come back DOCUMENT).
            # SKIP-heavy streams keep all workers busy (in-flight is nearly
            # free); DOCUMENT-heavy streams stall the scheduler at
            # `queue` (+ up to workers-1) pending DOCUMENTs until the main
            # loop consumes them - so an interrupt loses little work.
            ahead = sum(1 for i, v in state["done"].items()
                        if v == "DOCUMENT" and i >= main_pos)
            ahead += state["inflight"]
            while (state["next"] < len(shas)
                   and state["inflight"] < workers
                   and ahead < queue_size + workers):
                idx = state["next"]
                if idx not in state["done"]:
                    work.put((idx, shas[idx]))
                    state["inflight"] += 1
                    ahead += 1
                state["next"] = idx + 1
        time.sleep(0.3)

    for _thread in threads:
        work.put(None)
    for thread in threads:
        thread.join(timeout=120)

    counts = {"DOCUMENT": 0, "SKIP": 0, "ERROR": 0}
    for verdict in state["done"].values():
        counts[verdict] = counts.get(verdict, 0) + 1
    log("classifier done: DOCUMENT=%d SKIP=%d ERROR=%d"
        % (counts["DOCUMENT"], counts["SKIP"], counts["ERROR"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
