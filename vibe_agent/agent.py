"""The agent loop: messages -> model -> execute tools -> repeat until finish().

With a `validator` callback the loop gains repair rounds: after a finish that
left docs on disk (DOC_UPDATED, or NO_DOC with write_doc calls behind it), the
validator inspects the docs; if it reports problems, they are fed back as a new
user message and the agent continues (fix and finish again), up to
`repair_rounds` extra rounds. Each repair round EXTENDS the step budget by
`REPAIR_EXTRA_STEPS`, so validation feedback can never eat the steps the model
needs to fix and re-finish.

Weak-model guardrails observed on real runs (fernflower):
- narration/empty responses: a reply with text but no tool call gets a short
  user nudge pushing the model back to tools; an EMPTY reply (all tokens burned
  as hidden reasoning) is nudged too, and only aborted after
  `EMPTY_ABORT_THRESHOLD` consecutive empties;
- duplicate calls: an EXACT repeat of a previous (tool, arguments) pair is
  refused with an explanation instead of executing (real runs burned 5-7 steps
  re-reading the same file or re-running the same `git show --stat`);
- deadline: in the last `DEADLINE_WINDOW` steps a note rides on tool results
  and a one-time user deadline is injected (regardless of what the step did),
  so the model calls finish before the limit cuts it off;
- productive-write extension: if the budget runs out while the model is still
  successfully writing docs (large path-hygiene commits touch many docs), the
  budget is extended (up to `WRITE_EXTENSIONS` times) so the work is finished,
  not truncated;
- finish grace: if the budget still runs out with docs already written and no
  finish, ONE extra finish-only round is granted (every other tool refused) -
  throwing away a completed doc pass for a missing finish call wastes an
  entire re-run. If even that round passes without a finish call, the loop
  SYNTHESIZES the missing DOC_UPDATED verdict from the docs actually written
  and still routes it through the validator/repair flow;
- output-token cuts: a tool call whose JSON was cut off by the model/gateway
  output limit (weak gateways cap at ~8k tokens without setting
  finish_reason) is refused with an explanation of the split-write technique
  (write the first half, then write_doc append) instead of a bare parse
  error - real runs retried the same giant write 7-10 times until the budget
  died;
- context compaction (limits.compact_threshold_tokens > 0, e.g. the "small"
  profile): the whole message history is normally re-sent on every
  round-trip, which overflows small (~32k) context windows mid-session.
  Once a request reports prompt_tokens at/above the threshold, OLDER tool
  results are shrunk in place (the last compact_keep_groups assistant+tool
  groups stay verbatim; message structure and tool_call ids are preserved,
  so strict gateways still see a valid conversation) and each shrunken
  result carries an explicit "re-read if needed" note. A duplicate re-read
  whose previous result was compacted away is let through the duplicate
  guard, so the model can always recover content it still needs.
- provider context-limit 400s (llm.ContextOverflowError): when the gateway
  still rejects an oversized request despite compaction (e.g. a huge
  validator repair message arrives as a NEW user message that
  compact_history never touches), the loop runs escalating emergency
  passes - halve/quarter the result cap, keep only the last group verbatim,
  truncate oversized injected user messages and (last resort) the task
  brief itself - retries the request, and adapts the compaction threshold
  to the provider-reported window so later growth compacts before hitting
  it again. Only when every pass frees nothing does the error escape as an
  ERROR verdict.
"""

import json

from .llm import ContextOverflowError

MAX_TOOL_RESULT_CHARS = 100_000
REPAIR_EXTRA_STEPS = 6
WRITE_EXTRA_STEPS = 6
WRITE_EXTENSIONS = 2
RECONSIDER_EXTRA_STEPS = 8  # budget for the one-shot prior-docs reconsideration
DEADLINE_WINDOW = 5  # last N steps of the budget get deadline pressure
EMPTY_ABORT_THRESHOLD = 3
# escalating emergency passes after a provider context-limit 400 (see
# _overflow_shrink); each must free chars or the session errors out
OVERFLOW_ATTEMPTS = 4
OVERFLOW_FLOOR_CHARS = 400  # hard floor for tool results in emergencies
OVERFLOW_USER_CHARS = (6000, 2500)  # per-pass cap for injected user messages
# responses at/above this many completion tokens are treated as cut (the
# neuraldeep gateway caps output at 8000 without setting finish_reason)
CUT_TOKEN_THRESHOLD = 7900
# unparsable arguments at least this long are almost certainly a cut write
CUT_ARGS_CHARS = 20_000
DEADLINE_NOTE = ("\n[step %d/%d - budget nearly exhausted: call the finish tool "
                 "NOW with your best verdict; further exploration will be cut off]")
DEADLINE_USER = ("STEP BUDGET ALMOST EXHAUSTED (step %d of %d): stop exploring "
                 "and call the finish tool NOW with your best verdict.")
GRACE_NOTE = ("STEP BUDGET EXHAUSTED - but docs were written this session, so "
              "the verdict is missing. Call the finish tool NOW (verdict "
              "DOC_UPDATED with the files you wrote, NO_DOC, or ERROR). Every "
              "other tool is disabled; finish is the ONLY accepted call.")
DUPLICATE_NOTE = ("duplicate call: this exact %s call was already executed "
                  "earlier in this session and its result is unchanged. Do "
                  "NOT repeat it - continue with a DIFFERENT action or call "
                  "the finish tool.")
CUT_NOTE = ("arguments JSON is incomplete: your output was cut off at the "
            "model/gateway token limit before the JSON closed. The content "
            "is too long for ONE call - do NOT retry the same giant call. "
            "Split it: (1) write_doc the FIRST half now (a valid, complete "
            "JSON with shorter content), then (2) write_doc with the SAME "
            "path and {\"append\": true, \"content\": ...} for each further "
            "part. Keep every call's content under ~150 lines.")
NUDGE_TEXTONLY = ("You replied with text only and no tool call. This pipeline is "
                  "tool-driven: act now - call the finish tool with your verdict "
                  "(or another tool only if it is strictly necessary).")
NUDGE_EMPTY = ("Your last response was empty (no content, no tool call - the "
               "output stayed hidden reasoning). Respond with a tool call, "
               "preferably finish with your verdict.")


def _estimate_prompt_tokens(messages):
    """Rough chars/4 fallback for gateways that report no prompt_tokens."""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        for call in message.get("tool_calls") or []:
            function = call.get("function") or call
            total += len(str(function.get("arguments") or ""))
    return total // 4


def compact_history(messages, keep_groups, old_result_chars,
                    old_text_chars=400):
    """Downsample OLD assistant/tool groups in place (parity-safe).

    A group is an assistant message carrying tool_calls plus the tool-result
    messages that directly follow it. The last `keep_groups` groups stay
    verbatim (the model acts on them right now); in older groups the
    assistant narration and each tool result are shrunk to small caps, with
    an explicit "re-read if needed" note. Nothing is dropped and no ids are
    rewritten, so the sequence remains a valid OpenAI-compatible
    conversation (every tool_call still answered by its tool result).

    Returns the list of tool_call_ids whose results were shrunk (empty =
    nothing to do; the pass is idempotent).
    """
    starts = [i for i, m in enumerate(messages)
              if m.get("role") == "assistant" and m.get("tool_calls")]
    if len(starts) <= keep_groups:
        return []
    first_kept = starts[len(starts) - keep_groups]
    shrunk_ids = []
    for index in range(starts[0], first_kept):
        message = messages[index]
        role = message.get("role")
        if role == "assistant":
            content = message.get("content")
            if isinstance(content, str) and len(content) > old_text_chars:
                # size the cut so the result lands exactly at the cap ->
                # the pass is idempotent (a second run is a no-op)
                suffix = " ...[compacted]"
                keep = max(0, old_text_chars - len(suffix))
                message["content"] = content[:keep] + suffix
        elif role == "tool":
            content = message.get("content") or ""
            if len(content) > old_result_chars:
                suffix = (" ... [older result compacted from %d chars - "
                          "re-read the file/tool if you need it again]"
                          % len(content))
                keep = max(0, old_result_chars - len(suffix))
                message["content"] = content[:keep] + suffix
                shrunk_ids.append(message.get("tool_call_id"))
    return shrunk_ids


def _overflow_shrink(messages, attempt, keep_groups, result_chars):
    """One escalating emergency pass after a provider context-limit 400.

    compact_history alone cannot save a session whose bulk sits in injected
    USER messages (validator repair lists, reconsider hints) or in results
    that are already at/below the configured caps. The ladder:

      0: normal compaction pass (fresh content may have arrived meanwhile)
      1: keep only the LAST assistant+tool group verbatim, halve the cap
      2: quarter the cap, cap ANY tool result regardless of group position,
         truncate oversized user messages (messages[2:] - nudges, validator
         feedback, hints; the task brief messages[1] stays protected)
      3: hard floor everywhere, the task brief itself truncated too

    Every pass stays parity-safe: contents shrink in place, nothing is
    dropped, tool_call ids are untouched. Returns (chars_freed,
    tool_call_ids_shrunk); (0, []) means this pass could not help.
    """
    keep = keep_groups if attempt == 0 else 1
    cap = result_chars
    if attempt == 1:
        cap = max(OVERFLOW_FLOOR_CHARS, result_chars // 2)
    elif attempt == 2:
        cap = max(OVERFLOW_FLOOR_CHARS, result_chars // 4)
    elif attempt >= 3:
        cap = OVERFLOW_FLOOR_CHARS
    before = sum(len(m.get("content")) for m in messages
                 if isinstance(m.get("content"), str))
    shrunk = compact_history(messages, keep, cap,
                             old_text_chars=400 if attempt == 0 else 200)
    if attempt >= 2:
        suffix = " ...[truncated to fit the model context window]"
        user_cap = OVERFLOW_USER_CHARS[min(attempt - 2,
                                           len(OVERFLOW_USER_CHARS) - 1)]
        for index, message in enumerate(messages):
            role = message.get("role")
            content = message.get("content")
            if not isinstance(content, str):
                continue
            if role == "tool" and len(content) > cap:
                # compact_history keeps the last groups verbatim and skips
                # single-group sessions; in an emergency cap them directly
                # (the model can re-read the source via the tools)
                message["content"] = content[:max(0, cap - len(suffix))] + suffix
                if message.get("tool_call_id"):
                    shrunk.append(message["tool_call_id"])
            elif role == "user" and len(content) > user_cap:
                # protect the original task brief (messages[1]) until the
                # very last pass - losing it degrades grounding
                if index <= 1 and attempt < OVERFLOW_ATTEMPTS - 1:
                    continue
                message["content"] = (content[:max(0, user_cap - len(suffix))]
                                      + suffix)
    after = sum(len(m.get("content")) for m in messages
                if isinstance(m.get("content"), str))
    return before - after, shrunk


def run_agent(client, tools, system_prompt, first_user, max_steps, log,
              validator=None, repair_rounds=0, transcript=None, reconsider=None,
              limits=None):
    """Run the loop. Returns a verdict dict: {verdict, files, reason, steps, usage}.

    `reconsider` (optional) is called exactly once, after the model finishes
    with a CLEAN NO_DOC (no docs written this session). It returns a user
    message string (a prior-run hint to re-examine, see --doc-hints) or None.
    When it returns a message the finish is not accepted yet: the message is
    injected, the budget grows by RECONSIDER_EXTRA_STEPS, and the loop
    continues so the model can write docs and finish again (or reaffirm NO_DOC).

    `limits` (optional, from config `limits` / resolve_limits) carries the
    per-result cap and the compaction knobs; the defaults reproduce the
    historical constants with compaction off.
    """
    lim = limits if isinstance(limits, dict) else {}
    tool_result_cap = max(2000, int(lim.get("tool_result_chars")
                                    or MAX_TOOL_RESULT_CHARS))
    compact_threshold = int(lim.get("compact_threshold_tokens") or 0)
    compact_keep = max(1, int(lim.get("compact_keep_groups") or 4))
    compact_cap = max(200, int(lim.get("compact_result_chars") or 2000))

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": first_user},
    ]
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    empty_streak = 0
    repairs_used = 0
    budget = max_steps
    step = 0
    write_extensions = 0
    deadline_sent_at = None
    # canonical (name, args) -> [tool_call_ids of its LAST execution]; a
    # repeat is refused unless its previous result was compacted away since
    seen_calls = {}
    shrunk_ids = set()   # tool_call_ids shrunk by compact_history so far
    last_prompt_tokens = 0
    compact_stalled = False  # last pass freed nothing; wait for new messages
    finish_only = False   # grace mode: every tool except finish is refused
    grace_used = False
    reconsider_used = False

    def record(event):
        if transcript is not None:
            transcript.record(event)

    def user_message(text, source):
        messages.append({"role": "user", "content": text})
        record({"type": "user", "step": step, "source": source, "content": text})

    def one_round():
        """A single model round-trip + tool execution. True = stop the loop."""
        nonlocal budget, write_extensions, deadline_sent_at, finish_only
        nonlocal grace_used, step, empty_streak, repairs_used
        nonlocal reconsider_used, last_prompt_tokens, compact_stalled
        nonlocal compact_threshold
        step += 1
        if compact_threshold and last_prompt_tokens >= compact_threshold \
                and not compact_stalled:
            shrunk = compact_history(messages, compact_keep, compact_cap)
            if shrunk:
                shrunk_ids.update(sid for sid in shrunk if sid)
                log("context %d tokens >= %d - compacted %d old tool "
                    "result(s) (last %d groups kept verbatim)"
                    % (last_prompt_tokens, compact_threshold, len(shrunk),
                       compact_keep))
                record({"type": "compact", "step": step,
                        "prompt_tokens": last_prompt_tokens,
                        "results_shrunk": len(shrunk)})
            else:
                compact_stalled = True  # everything already small; retry later
        overflow_pass = 0
        while True:
            try:
                response = client.chat(messages, tools.definitions())
                break
            except ContextOverflowError as exc:
                # The gateway rejected the request as too big even though
                # compaction may already have run (its bulk can sit in
                # injected USER messages - e.g. a validator repair list -
                # which compact_history never touches). Shrink harder and
                # retry; surrender only when no pass can free anything.
                while overflow_pass < OVERFLOW_ATTEMPTS:
                    freed, ids = _overflow_shrink(messages, overflow_pass,
                                                  compact_keep, compact_cap)
                    overflow_pass += 1
                    if freed > 0:
                        shrunk_ids.update(sid for sid in ids if sid)
                        log("context overflow (HTTP 400) - emergency pass "
                            "%d/%d freed ~%d chars, retrying"
                            % (overflow_pass, OVERFLOW_ATTEMPTS, freed))
                        record({"type": "overflow", "step": step,
                                "pass": overflow_pass,
                                "chars_freed": freed,
                                "provider_limit": exc.limit})
                        break
                else:
                    raise
                # adapt the budget for the REST of the session: from now on
                # compact early enough that growth never reaches the window
                # again (0.82 of the provider limit leaves headroom for the
                # reply and the tool schemas the gateway also counts)
                target = 0
                if exc.limit:
                    target = max(1000, int(exc.limit * 0.82))
                elif compact_threshold:
                    target = max(1000, compact_threshold * 3 // 4)
                if target and (not compact_threshold
                               or target < compact_threshold):
                    compact_threshold = target
                    compact_stalled = False
                    log("compaction threshold adapted to %d tokens%s"
                        % (target, " (provider limit %d)" % exc.limit
                           if exc.limit else ""))
        for key in usage:
            usage[key] += int(response.get("usage", {}).get(key) or 0)
        last_prompt_tokens = int(response.get("usage", {})
                                 .get("prompt_tokens") or 0)
        if not last_prompt_tokens:
            last_prompt_tokens = _estimate_prompt_tokens(messages)
        compact_stalled = False  # fresh content arrived; a pass may help again
        message = response["message"]
        messages.append(message)
        record({
            "type": "assistant",
            "step": step,
            "content": response.get("content") or "",
            "tool_calls": response["tool_calls"],
            "finish_reason": response.get("finish_reason"),
            "usage": response.get("usage") or None,
            "context_total_tokens": usage["total_tokens"],
        })

        calls = response["tool_calls"]
        if not calls and not (finish_only and tools.wrote_docs):
            # (a grace round with docs on disk falls through to the
            # synthesized finish below instead of nudging further)
            text = (response.get("content") or "").strip()
            if text:
                empty_streak = 0
                user_message(NUDGE_TEXTONLY, "nudge:text-only")
                log("step %d/%d text-only reply - nudged back to tools"
                    % (step, budget))
            else:
                empty_streak += 1
                if empty_streak >= EMPTY_ABORT_THRESHOLD:
                    record({"type": "end", "verdict": "ERROR",
                            "reason": "model returned %d empty responses in a "
                                      "row" % empty_streak, "step": step})
                    one_round.result = _error(
                        "model returned empty responses %d times in a row"
                        % empty_streak, usage, step)
                    return True
                user_message(NUDGE_EMPTY, "nudge:empty")
                log("step %d/%d empty reply (%d/%d) - nudged"
                    % (step, budget, empty_streak, EMPTY_ABORT_THRESHOLD))
            return False

        empty_streak = 0
        finish_report = None
        finished = False
        wrote_this_step = False
        # a length cut (gateway output cap or finish_reason=length) turns
        # half-emitted tool JSON into "not valid JSON" errors - the model
        # then blindly retries the SAME giant call until the budget dies
        # (fernflower: 7-10 refused write_doc per commit). Detect the cut
        # and teach append-splitting instead of a bare parse error.
        raw_finish = response.get("finish_reason")
        cut_response = raw_finish == "length"
        comp = int(response.get("usage", {}).get("completion_tokens") or 0)
        if comp and comp >= CUT_TOKEN_THRESHOLD:
            cut_response = True
        for call in calls:
            name = call["name"]
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = None
            if arguments is None:
                raw_args = call["arguments"] or ""
                looks_cut = (cut_response
                             or len(raw_args) >= CUT_ARGS_CHARS
                             or not raw_args.rstrip().endswith("}"))
                if looks_cut:
                    result = {"ok": False, "error": CUT_NOTE}
                    log("step %d/%d %s -> refused (arguments cut by output "
                        "token limit)" % (step, budget, name))
                else:
                    result = {"ok": False,
                              "error": "arguments is not valid JSON"}
            elif finish_only and name != "finish":
                result = {"ok": False, "error":
                          "step budget exhausted: only the finish tool is "
                          "accepted now."}
            else:
                canonical = None
                if name != "finish" and isinstance(arguments, dict):
                    try:
                        canonical = name + ":" + json.dumps(
                            arguments, ensure_ascii=False, sort_keys=True)
                    except (TypeError, ValueError):
                        canonical = None
                prev_ids = seen_calls.get(canonical) if canonical else None
                if prev_ids and not shrunk_ids.intersection(prev_ids):
                    # exact repeat of an earlier call whose result is still
                    # verbatim in the context: refuse instead of burning
                    # another round on the same output (fernflower runs
                    # looped 5-7x on one git show / read_file). Repeats whose
                    # result was compacted away fall through - the model may
                    # legitimately need that content again.
                    result = {"ok": False, "error": DUPLICATE_NOTE % name}
                    log("step %d/%d %s -> refused (exact duplicate of an "
                        "earlier call)" % (step, budget, name))
                else:
                    if canonical:
                        seen_calls[canonical] = [call["id"]]
                    result = tools.execute(name, arguments)
            log("step %d/%d %s -> %s" % (step, budget, name,
                                         "ok" if result.get("ok") else "refused"))
            if name == "write_doc" and result.get("ok"):
                wrote_this_step = True
            content = json.dumps(result, ensure_ascii=False)
            if len(content) > tool_result_cap:
                content = content[:tool_result_cap] + ' ... [truncated]"}'
            if budget - step < DEADLINE_WINDOW and name != "finish":
                # deadline note rides on the tool results so the model sees it
                # in the very next round-trip (9723-style "fixed everything,
                # never re-finished" failures)
                content += DEADLINE_NOTE % (step, budget)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": content,
            })
            record({
                "type": "tool",
                "step": step,
                "name": name,
                "arguments": call["arguments"],
                "ok": bool(result.get("ok")),
                "result": content,
            })
            if name == "finish" and result.get("ok"):
                finished = True
                # Validate whenever docs may be dirty: a DOC_UPDATED finish, or
                # any write_doc this session (a model that wrote docs and then
                # finished NO_DOC still owes the pipeline a validated docs map).
                if (validator is not None
                        and repairs_used < repair_rounds
                        and (tools.finish_result.get("verdict") == "DOC_UPDATED"
                             or tools.wrote_docs)):
                    finish_report = validator()  # str repair message, or None
                break  # finish ends the batch (later calls in it are dropped)

        if (deadline_sent_at is None and budget - step < DEADLINE_WINDOW
                and not finished):
            # one-time hard deadline whatever the step was doing (the note on
            # tool results alone does not break read/loop spirals)
            deadline_sent_at = step
            user_message(DEADLINE_USER % (step, budget), "deadline")

        if (finish_only and step >= budget and not finished
                and tools.wrote_docs):
            # the finish-only grace round came and went without a finish call:
            # publish the docs the session actually wrote instead of
            # discarding the whole pass (fernflower 842af198: 7 docs written,
            # model kept exploring straight through the grace round)
            tools.finish_result = {
                "verdict": "DOC_UPDATED",
                "files": list(tools.written_files),
                "reason": "auto-finished at step budget exhaustion: the model "
                          "did not call finish; docs written this session "
                          "are published as-is",
            }
            finished = True
            log("grace round without finish - synthesizing DOC_UPDATED for "
                "%d written doc(s)" % len(tools.written_files))
            if (validator is not None
                    and repairs_used < repair_rounds):
                finish_report = validator() or None

        if finished and finish_report:
            repairs_used += 1
            budget += REPAIR_EXTRA_STEPS
            finish_only = False  # repair needs the full toolset again
            log("validation round %d: problems fed back for repair "
                "(budget +%d -> %d steps)" % (repairs_used, REPAIR_EXTRA_STEPS,
                                              budget))
            user_message(finish_report, "validator")
            return False

        if finished:
            verdict = dict(tools.finish_result)
            verdict["steps"] = step
            verdict["usage"] = usage
            if verdict["verdict"] == "NO_DOC" and tools.wrote_docs:
                # docs were written earlier in the session (e.g. before a repair
                # round) - never report NO_DOC with dirty docs on disk
                verdict["verdict"] = "DOC_UPDATED"
            if (reconsider is not None and not reconsider_used
                    and verdict["verdict"] == "NO_DOC"):
                # prior-run hint (--doc-hints): a previous run documented this
                # commit - offer its actual docs for one reconsideration round
                # instead of accepting the NO_DOC straight away
                reconsider_used = True
                hint = reconsider()
                if hint:
                    budget += RECONSIDER_EXTRA_STEPS
                    log("NO_DOC finish - reconsideration round with prior-run "
                        "docs (budget +%d -> %d steps)"
                        % (RECONSIDER_EXTRA_STEPS, budget))
                    user_message(hint, "reconsider")
                    return False
            record({"type": "end", "verdict": verdict["verdict"],
                    "files": verdict.get("files") or [],
                    "reason": verdict.get("reason") or "", "step": step})
            one_round.result = verdict
            return True

        # budget exhausted, but docs were mid-flight: extend so a large
        # path-hygiene pass is COMPLETED (with a hard "finish now" instruction)
        # instead of truncated half-written
        if (wrote_this_step and step >= budget and write_extensions < WRITE_EXTENSIONS
                and not finished):
            write_extensions += 1
            budget += WRITE_EXTRA_STEPS
            log("docs mid-flight at budget - extending by %d steps "
                "(extension %d/%d, budget -> %d)"
                % (WRITE_EXTRA_STEPS, write_extensions, WRITE_EXTENSIONS, budget))
            user_message("Step budget extension (%d more steps): you were in "
                         "the middle of updating docs. FINISH the essential "
                         "remaining writes, then call the finish tool "
                         "immediately." % WRITE_EXTRA_STEPS, "extension")
            return False

        # budget exhausted: if docs were written but finish never came, grant
        # ONE finish-only round instead of discarding the whole doc pass
        if step >= budget and tools.wrote_docs and not grace_used:
            grace_used = True
            budget = step + 1
            finish_only = True
            log("budget exhausted with docs written - finish-grace round "
                "(finish only)")
            user_message(GRACE_NOTE, "grace")
            return False

        if step >= budget:
            # budget exhausted and neither extension nor grace applies
            one_round.result = None
            return True
        return False

    one_round.result = None

    while True:
        if one_round():
            break
        # safety net: budget extensions and grace rounds are bounded, so the
        # loop always terminates
    if one_round.result is None:
        record({"type": "end", "verdict": "ERROR",
                "reason": "max_steps (%d) reached without finish" % budget,
                "step": step})
        return _error("max_steps (%d) reached without finish" % budget,
                      usage, step)
    return one_round.result


def _error(reason, usage, steps):
    return {"verdict": "ERROR", "files": [], "reason": reason,
            "steps": steps, "usage": usage}
