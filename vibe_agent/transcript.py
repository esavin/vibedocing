"""JSONL transcript of every LLM interaction in one agent session.

One file per commit (``verdicts/<sha>.transcript.jsonl``, gitignored). Each line
is one JSON event; replaying them in order reconstructs the exact message
history the model saw:

  {"type":"session", ...}    session header (model, budgets, prompt sizes)
  {"type":"assistant", ...}  one model response: content, tool_calls,
                             finish_reason, per-step usage tokens
  {"type":"tool", ...}       one tool call: name, raw arguments, ok flag, and
                             the result exactly as it was fed back
  {"type":"user", ...}       pipeline-injected user message (repair feedback)
  {"type":"compact", ...}    history compaction pass (limits profile): old
                             tool results shrunk in place at this step
  {"type":"end", ...}        final verdict / error reason

Summarize a transcript (context-growth curve, tool histogram, repeated calls,
truncated outputs) with:

    python3 -m vibe_agent.transcript verdicts/<sha>.transcript.jsonl
"""

import json
import sys
import time


class Transcript(object):
    """Append-only JSONL event sink. Write failures are never fatal."""

    def __init__(self, path):
        self.path = path
        self._fh = open(path, "a", encoding="utf-8")

    def record(self, event):
        event = dict(event)
        event.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
        try:
            self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._fh.flush()
        except (OSError, ValueError):
            pass

    def close(self):
        try:
            self._fh.close()
        except OSError:
            pass


def load(path):
    events = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"type": "corrupt", "line": number,
                               "raw": line[:200]})
    return events


def _short(text, limit=120):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "..."


def summarize(path):
    events = load(path)
    session = next((e for e in events if e.get("type") == "session"), {})
    assistants = [e for e in events if e.get("type") == "assistant"]
    calls = [e for e in events if e.get("type") == "tool"]
    end = next((e for e in reversed(events) if e.get("type") == "end"), {})

    print("== %s ==" % path)
    print("session: sha=%(sha)s model=%(model)s mode=%(mode)s "
          "root_commit=%(root_commit)s max_steps=%(max_steps)s"
          % {k: session.get(k, "?") for k in ("sha", "model", "mode",
                                              "root_commit", "max_steps")})
    print("model responses: %d; tool calls: %d"
          % (len(assistants), len(calls)))

    print("\ncontext growth (per model response):")
    with_usage = [e for e in assistants if (e.get("usage") or {}).get("prompt_tokens")]
    if not with_usage:
        print("  (no per-step usage reported by the endpoint)")
    compact_at = {}
    for e in events:
        if e.get("type") == "compact":
            compact_at[e.get("step")] = e.get("results_shrunk")
    for e in assistants:
        usage = e.get("usage") or {}
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        notes = []
        reason = e.get("finish_reason")
        if reason not in (None, "stop", "tool_calls"):
            notes.append("finish_reason=%s" % reason)
        if e.get("step") in compact_at:
            notes.append("history compacted (%s result(s) shrunk)"
                         % compact_at[e.get("step")])
        if not (e.get("content") or "").strip() and not e.get("tool_calls"):
            notes.append("empty response, no tool calls")
        names = [c.get("name") for c in (e.get("tool_calls") or [])]
        if names:
            notes.append("calls: " + ", ".join(str(n) for n in names))
        elif (e.get("content") or "").strip():
            notes.append("plain text, no tool call")
        print("  step %-3s prompt_tokens=%-8s completion_tokens=%-7s %s"
              % (e.get("step"), prompt, completion,
                 "; ".join(_short(n) for n in notes)))

    print("\ntool calls by name:")
    counts, refused = {}, {}
    for e in calls:
        name = str(e.get("name") or "?")
        counts[name] = counts.get(name, 0) + 1
        if not e.get("ok"):
            refused[name] = refused.get(name, 0) + 1
    if counts:
        for name in sorted(counts, key=counts.get, reverse=True):
            suffix = (" (%d refused)" % refused[name]) if refused.get(name) else ""
            print("  %-12s %d%s" % (name, counts[name], suffix))
    else:
        print("  (none)")

    repeats = {}
    for e in calls:
        key = (str(e.get("name") or "?"), str(e.get("arguments") or ""))
        repeats[key] = repeats.get(key, 0) + 1
    repeated = sorted((v, k) for k, v in repeats.items() if v > 1)
    if repeated:
        print("\nidentical repeated calls (weak-model signal):")
        for count, (name, arguments) in reversed(repeated[:5]):
            print("  %dx %s %s" % (count, name, _short(arguments)))

    anomalies = []
    length_cuts = [e for e in assistants if e.get("finish_reason") == "length"]
    if length_cuts:
        anomalies.append("%d response(s) cut by finish_reason=length"
                         % len(length_cuts))
    bad_json = [e for e in calls
                if "not valid JSON" in str(e.get("result"))]
    if bad_json:
        anomalies.append("%d tool call(s) with unparsable arguments"
                         % len(bad_json))
    truncated = [e for e in calls if "[truncated" in str(e.get("result"))]
    if truncated:
        anomalies.append("%d tool result(s) truncated before feedback"
                         % len(truncated))
    if anomalies:
        print("\nanomalies:")
        for item in anomalies:
            print("  - %s" % item)

    print("\nend: verdict=%s reason=%s"
          % (end.get("verdict", "?"), _short(end.get("reason"), 200)))

    print("\ninterpretation hints:")
    if length_cuts:
        print("- finish_reason=length means llm.max_tokens is too small: the "
              "tool-call JSON is cut mid-way, the call is refused, and the "
              "model never reaches finish. Raise llm.max_tokens.")
    if bad_json:
        print("- unparsable arguments often follow length cuts or indicate a "
              "model that cannot emit strict JSON tool arguments.")
    if repeated:
        print("- identical calls repeated means the model ignores tool "
              "results: a model-capability issue, not context size.")
    last_prompt = None
    for e in reversed(assistants):
        usage = e.get("usage") or {}
        if usage.get("prompt_tokens"):
            last_prompt = usage.get("prompt_tokens")
            break
    if last_prompt:
        print("- last prompt_tokens=%s: compare with the model's context "
              "window; steady large growth per step means big tool results "
              "are crowding the context (lower max_steps, shrink tool output "
              "caps, or split the work)." % last_prompt)
    if not with_usage and not length_cuts and not repeated:
        print("- read the assistant events above: a model that answers in "
              "plain text without ever calling finish is a weak-model signal.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python3 -m vibe_agent.transcript <file>.transcript.jsonl",
              file=sys.stderr)
        return 2
    try:
        summarize(argv[0])
    except OSError as exc:
        print("transcript: cannot read %s: %s" % (argv[0], exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
