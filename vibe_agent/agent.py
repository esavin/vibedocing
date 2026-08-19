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
  died.
"""

import json

MAX_TOOL_RESULT_CHARS = 100_000
REPAIR_EXTRA_STEPS = 6
WRITE_EXTRA_STEPS = 6
WRITE_EXTENSIONS = 2
RECONSIDER_EXTRA_STEPS = 8  # budget for the one-shot prior-docs reconsideration
DEADLINE_WINDOW = 5  # last N steps of the budget get deadline pressure
EMPTY_ABORT_THRESHOLD = 3
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


def run_agent(client, tools, system_prompt, first_user, max_steps, log,
              validator=None, repair_rounds=0, transcript=None, reconsider=None):
    """Run the loop. Returns a verdict dict: {verdict, files, reason, steps, usage}.

    `reconsider` (optional) is called exactly once, after the model finishes
    with a CLEAN NO_DOC (no docs written this session). It returns a user
    message string (a prior-run hint to re-examine, see --doc-hints) or None.
    When it returns a message the finish is not accepted yet: the message is
    injected, the budget grows by RECONSIDER_EXTRA_STEPS, and the loop
    continues so the model can write docs and finish again (or reaffirm NO_DOC).
    """
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
    seen_calls = {}       # canonical (name, args) -> True, for duplicate kicks
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
        nonlocal reconsider_used
        step += 1
        response = client.chat(messages, tools.definitions())
        for key in usage:
            usage[key] += int(response.get("usage", {}).get(key) or 0)
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
                if canonical is not None and canonical in seen_calls:
                    # exact repeat of an earlier call: refuse instead of
                    # burning another round on the same output (fernflower
                    # runs looped 5-7x on one git show / read_file)
                    result = {"ok": False, "error": DUPLICATE_NOTE % name}
                    log("step %d/%d %s -> refused (exact duplicate of an "
                        "earlier call)" % (step, budget, name))
                else:
                    if canonical is not None:
                        seen_calls[canonical] = True
                    result = tools.execute(name, arguments)
            log("step %d/%d %s -> %s" % (step, budget, name,
                                         "ok" if result.get("ok") else "refused"))
            if name == "write_doc" and result.get("ok"):
                wrote_this_step = True
            content = json.dumps(result, ensure_ascii=False)
            if len(content) > MAX_TOOL_RESULT_CHARS:
                content = content[:MAX_TOOL_RESULT_CHARS] + ' ... [truncated]"}'
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
