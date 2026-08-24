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

DOCUMENT when the commit (judge from the DIFF, not the message):
- adds a new user-facing function / command / route / screen / endpoint / capability;
- adds a new configuration option, setting, flag, or preference key (a constant in \
a preferences/config module is a user-facing knob);
- adds new API surface in production code: a new class/type, or new non-private \
methods, fields, or constants - even when the message sounds like a fix or cleanup \
("handle some cases of X", "make Y apply only to Z", "adapt patches", "cleanup \
after review");
- materially changes the behavior of a core processing or output subsystem (new \
handling for a construct/category, changed mapping or rendering rules, broader \
input support), even when the diff is small and the message frames it as a fix;
- changes the signature, visibility, or name of existing API: new overloads, changed \
parameter/return types, constructors, or renamed methods/classes/constants/option \
keys. Documentation cites these names: paired removal of old-name declarations and \
addition of new-name ones (often across many files) is a rename - DOCUMENT it;
- changes what the tool emits or produces for its users (output rendering, \
formatting rules, naming, mappings, generated artifacts);
- replaces a core mechanism or architecture (new pipeline stage, different \
threading/context model, new intermediate representation), even when the message \
calls it a refactor or rework;
- fixes a total failure on an entire platform, version, or input class - a commit \
message that pairs an issue ID or user report with a failure ("does not work \
for/on X", "exception", "crash", "not supported") marks a capability gap, so a \
small guard/fallback diff restoring operation is a DOCUMENT;
- broadens support to a new input or toolchain category (a different compiler \
such as ECJ vs javac, a new Java/JVM/Kotlin version, a new bytecode pattern or \
obfuscation form) - typically a small addition inside an existing helper, not a \
new file;
- introduces a new module, package, service, or subsystem worth a design note;
- renames, moves or deletes source files (path hygiene - documentation may cite \
those paths). When in doubt for a rename/move/delete, answer DOCUMENT.

SKIP when the commit is only: a narrow bug fix of one incorrect case, a refactor \
with no API/behavior change, formatting, lint, build/CI, dependency bump, \
tests/testdata only, docs-only change, chore, typo, or a perf micro-tweak. Changes \
confined to test/spec directories or build files are never user-facing.

Commit messages are unreliable - judge from the actual diff content provided. When \
the full diff is omitted, you still get the NAME STATUS, DIFFSTAT and an API-SURFACE \
DIGEST of declaration-like lines added (+) / removed (-): use them - a new "+" \
declaration or paired "-"/"+" declarations mean new or renamed API. When a change \
to non-test source code is genuinely on the border, answer DOCUMENT: a false \
DOCUMENT only costs one extra check by a stronger agent, but a false SKIP is \
silently lost documentation."""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_VALID = ("DOCUMENT", "SKIP")
_MAX_JSON_ATTEMPTS = 3

# Declaration-ish added/removed lines extracted from oversized diffs so the
# classifier still sees new/renamed API surface when the full diff does not fit.
_DECL_LINE_RE = re.compile(
    r"\b(?:public|protected|private|internal|export|extern|open|sealed|"
    r"override|final|abstract)\b[^=\n]{0,120}\("
    r"|\b(?:class|interface|enum|struct|record|trait|object|module)\s+[A-Za-z_]\w*"
    r"|\b(?:def|fn|func|function|sub|proc|operator)\s+\w+"
    r"|\bconst(?:ant)?\s+\w+\s*[:=]"
    r"|\b[A-Z][A-Z0-9_]{2,}\s*=\s*[\"'\[0-9tfn]"
    r"|#define\s+\w+"
)
_DECL_NOISE_RE = re.compile(r"^\s*(//|/\*|\*|import\b|from\b|package\b|using\b|"
                            r"#include\b)")
_TEST_PATH_RE = re.compile(r"(^|/)(tests?|spec|testdata|fixtures?)(/|$)|"
                           r"(^|/)(test_[^/]*|[^/]*_test)\.[a-z]+$|"
                           r"[Tt]est[A-Z_]|(^|/)build(/|$)", re.IGNORECASE)


def declarations_digest(diff, cap):
    """Declaration-like +/= lines from a diff that is too big to inject whole.

    Keeps the signal a classifier needs for oversized commits: new types and
    methods, option/constants, and paired removed/added declarations (renames).
    Test/build paths are excluded - they are not user-facing surface.
    """
    out, seen, path, size = [], set(), None, 0
    for line in diff.splitlines():
        if line.startswith(("+++ ", "--- ")):
            token = line[4:].strip()
            if token != "/dev/null":
                path = token.split("\t")[0][2:] if token[:2] in ("a/", "b/") \
                    else token
            continue
        if not line[:1] in "+-" or line.startswith(("+++", "---")):
            continue
        if path and _TEST_PATH_RE.search(path):
            continue
        body = line[1:].strip()
        if not body or len(body) > 200 or _DECL_NOISE_RE.match(body):
            continue
        if not _DECL_LINE_RE.search(body):
            continue
        key = line[:1] + body
        if key in seen:
            continue
        seen.add(key)
        out.append(key[:200])
        size += len(key) + 1
        if size >= cap:
            break
    return "\n".join(out)


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
            digest = declarations_digest(
                diff, int(limits.get("diff_digest_chars") or 4000))
            note = ("full diff omitted: %d chars - judge from name status + "
                    "diffstat" % len(diff))
            if digest:
                note += (" + the API-surface digest below")
                parts.append("(full diff omitted: %d chars)" % len(diff))
                parts.append("API-SURFACE DIGEST (declaration-like lines added "
                             "(+) / removed (-) by this commit, test and build "
                             "files excluded):\n%s" % digest)
            else:
                parts.append("(%s)" % note)
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


def _add_usage(total, usage):
    """Accumulate a provider usage block into {prompt,completion,total}."""
    if not usage:
        return total
    total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
    total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
    total["total_tokens"] += int(usage.get("total_tokens") or 0)
    return total


def classify_one(client, sha, repo, limits):
    """Classify a single commit. Returns (verdict, reason, usage); never
    raises. usage sums provider-reported tokens across all LLM round-trips
    for this commit (empty when no LLM call was needed). Root commits are
    DOCUMENT without an LLM call (the agent has a dedicated
    initial-snapshot mode for them)."""
    if not P.sha_looks_valid(sha):
        return "ERROR", "not a commit: %s" % sha, {}
    if not P.parent_sha(repo, sha):
        return "DOCUMENT", "root commit (initial snapshot)", {}
    messages = [
        {"role": "system", "content": CLASSIFIER_SYSTEM},
        {"role": "user", "content": build_user_message(sha, repo, limits)},
    ]
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for _attempt in range(_MAX_JSON_ATTEMPTS):
        try:
            resp = client.chat(messages, None)
        except Exception as exc:  # FatalLLMError after client-level retries
            return "ERROR", "llm: %s" % str(exc)[:300], usage
        _add_usage(usage, resp.get("usage"))
        text = (resp.get("content") or "").strip()
        verdict, reason = parse_verdict(text)
        if verdict:
            return verdict, reason, usage
        messages = messages + [
            {"role": "assistant", "content": text[:2000]},
            {"role": "user", "content":
                'Invalid answer. Reply with ONE line of JSON only: '
                '{"verdict": "DOCUMENT" or "SKIP", "reason": "..."}'},
        ]
    return "ERROR", "unparseable reply after %d attempts" % _MAX_JSON_ATTEMPTS, usage


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
    state = {"done": {}, "inflight": 0, "next": 0, "usage": []}
    work = _queue.Queue()

    def worker():
        while True:
            item = work.get()
            if item is None:
                return
            idx, sha = item
            try:
                verdict, reason, usage = classify_one(client, sha, args.repo,
                                                      limits)
            except Exception as exc:  # one commit must never kill the worker
                verdict, reason, usage = "ERROR", "crash: %s" % exc, {}
            try:
                payload = {
                    "sha": sha, "verdict": verdict, "reason": reason,
                    "model": model, "index": idx,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                }
                if usage.get("total_tokens"):
                    payload["usage"] = usage
                write_verdict(args.out, payload)
            except OSError as exc:
                log("cannot write verdict for %s: %s" % (sha[:10], exc))
            with lock:
                state["done"][idx] = verdict
                state["inflight"] -= 1
                if usage.get("total_tokens"):
                    state["usage"].append(usage)
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
    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for usage in state["usage"]:
        _add_usage(tokens, usage)
    log("classifier done: DOCUMENT=%d SKIP=%d ERROR=%d tokens(prompt=%d "
        "completion=%d total=%d)"
        % (counts["DOCUMENT"], counts["SKIP"], counts["ERROR"],
           tokens["prompt_tokens"], tokens["completion_tokens"],
           tokens["total_tokens"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
