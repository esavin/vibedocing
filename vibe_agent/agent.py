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
- deadline: in the last `DEADLINE_WINDOW` steps a note rides on tool results
  and a one-time user deadline is injected, so the model calls finish before
  the limit cuts it off;
- productive-write extension: if the budget runs out while the model is still
  successfully writing docs (large path-hygiene commits touch many docs), the
  budget is extended once so the work is finished, not truncated.
"""

import json

MAX_TOOL_RESULT_CHARS = 100_000
REPAIR_EXTRA_STEPS = 6
WRITE_EXTRA_STEPS = 6
DEADLINE_WINDOW = 3  # last N steps of the budget get deadline pressure
EMPTY_ABORT_THRESHOLD = 3
DEADLINE_NOTE = ("\n[step %d/%d - budget nearly exhausted: call the finish tool "
                 "NOW with your best verdict; further exploration will be cut off]")
NUDGE_TEXTONLY = ("You replied with text only and no tool call. This pipeline is "
                  "tool-driven: act now - call the finish tool with your verdict "
                  "(or another tool only if it is strictly necessary).")
NUDGE_EMPTY = ("Your last response was empty (no content, no tool call - the "
               "output stayed hidden reasoning). Respond with a tool call, "
               "preferably finish with your verdict.")


def run_agent(client, tools, system_prompt, first_user, max_steps, log,
              validator=None, repair_rounds=0, transcript=None):
    """Run the loop. Returns a verdict dict: {verdict, files, reason, steps, usage}."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": first_user},
    ]
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    empty_streak = 0
    repairs_used = 0
    budget = max_steps
    step = 0
    write_extended = False
    deadline_sent_at = None

    def record(event):
        if transcript is not None:
            transcript.record(event)

    def user_message(text, source):
        messages.append({"role": "user", "content": text})
        record({"type": "user", "step": step, "source": source, "content": text})

    while step < budget:
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
        if not calls:
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
                    return _error("model returned empty responses %d times in "
                                  "a row" % empty_streak, usage, step)
                user_message(NUDGE_EMPTY, "nudge:empty")
                log("step %d/%d empty reply (%d/%d) - nudged"
                    % (step, budget, empty_streak, EMPTY_ABORT_THRESHOLD))
            continue

        empty_streak = 0
        finish_report = None
        finished = False
        wrote_this_step = False
        for call in calls:
            name = call["name"]
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = None
            if arguments is None:
                result = {"ok": False, "error": "arguments is not valid JSON"}
            else:
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

        if wrote_this_step:
            wrote_last_step = True
            if deadline_sent_at is None and budget - step < DEADLINE_WINDOW:
                deadline_sent_at = step
                user_message("STEP BUDGET ALMOST EXHAUSTED (step %d of %d): "
                             "stop exploring and call the finish tool NOW with "
                             "your best verdict." % (step, budget), "deadline")

        if finished and finish_report:
            repairs_used += 1
            budget += REPAIR_EXTRA_STEPS
            log("validation round %d: problems fed back for repair "
                "(budget +%d -> %d steps)" % (repairs_used, REPAIR_EXTRA_STEPS,
                                              budget))
            user_message(finish_report, "validator")
            continue

        if finished:
            verdict = dict(tools.finish_result)
            verdict["steps"] = step
            verdict["usage"] = usage
            if verdict["verdict"] == "NO_DOC" and tools.wrote_docs:
                # docs were written earlier in the session (e.g. before a repair
                # round) - never report NO_DOC with dirty docs on disk
                verdict["verdict"] = "DOC_UPDATED"
            record({"type": "end", "verdict": verdict["verdict"],
                    "files": verdict.get("files") or [],
                    "reason": verdict.get("reason") or "", "step": step})
            return verdict

        # budget exhausted, but docs were mid-flight: extend once so a large
        # path-hygiene pass is COMPLETED (with a hard "finish now" instruction)
        # instead of truncated half-written
        if (wrote_this_step and step >= budget and not write_extended
                and not finished):
            write_extended = True
            budget += WRITE_EXTRA_STEPS
            log("docs mid-flight at budget - extending by %d steps "
                "(budget -> %d)" % (WRITE_EXTRA_STEPS, budget))
            user_message("Step budget extension (%d more steps): you were in "
                         "the middle of updating docs. FINISH the essential "
                         "remaining writes, then call the finish tool "
                         "immediately - this is the last extension."
                         % WRITE_EXTRA_STEPS, "extension")
            continue

    record({"type": "end", "verdict": "ERROR",
            "reason": "max_steps (%d) reached without finish" % budget,
            "step": step})
    return _error("max_steps (%d) reached without finish" % budget, usage, step)


def _error(reason, usage, steps):
    return {"verdict": "ERROR", "files": [], "reason": reason,
            "steps": steps, "usage": usage}
