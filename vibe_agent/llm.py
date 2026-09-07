"""OpenAI-compatible chat-completions client over stdlib urllib.

Non-streaming by design: the agent is headless and single-request-per-step, so a
plain POST with retries is all that is needed. Swapping this module for the
`openai` SDK later would not affect the rest of the package.
"""

import json
import random
import re
import threading
import time
import urllib.error
import urllib.request

RETRYABLE_HTTP = {429, 500, 502, 503, 504}
# Provider outages (503 "no available server") span minutes: wait 1, 2, 4, 8,
# 16 minutes between HTTP-level retries instead of burning them within seconds.
HTTP_BACKOFF_SECONDS = (60, 120, 240, 480, 960)

# HTTP 400 bodies that mean "input exceeds the context/token window".
# neuraldeep.ru reports in Russian ("Слишком длинный запрос: N токенов входа
# при пределе M"); OpenAI/vLLM/others send English variants.
_OVERFLOW_MARKERS = (
    "слишком длинн", "токенов входа", "context_length", "context length",
    "maximum context", "prompt is too long", "too many tokens",
    "reduce the length", "reduce your prompt", "input too large",
)
_OVERFLOW_PAIRS = (
    ("token", "too long"), ("токен", "предел"), ("токен", "превыш"),
    ("token", "exceed"),
)
# "при пределе 46112" / "context length is 128000 tokens" -> the window size
_LIMIT_RE = re.compile(
    r"(?:пределе|limit|length|maximum|max)[^\d]{0,30}(\d{4,})", re.IGNORECASE)

# DeepSeek models sometimes emit tool calls as plain text in content - the
# DSML syntax (<｜DSML｜:finish verdict="..." .../> or
# <｜DSML｜:finish>{json}</｜DSML｜:finish>) - instead of the message.tool_calls
# channel (the gateway's chat template fails to lift them). Left unparsed,
# every such reply looks "text-only" and the agent nudges the model back to
# tools forever: the model believes it IS calling tools, so it repeats the
# same DSML text (observed: snapshot resources module, 77+ identical rounds).
_DSML_PREFIX = "<｜DSML｜:"
_DSML_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# key="quoted value" (backslash escapes allowed) or key=[unquoted json array]
_DSML_ATTR_RE = re.compile(
    r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|(\[[^\]]*\]))')
_DSML_MAX_CALLS = 16
_DSML_SEGMENT_CHARS = 20_000  # attr-form span cap (garbled multi-call blobs)


def _balanced_json_end(text, start):
    """Index just past the balanced {...} object opening at text[start]
    (string- and escape-aware), or 0 when it never closes."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return 0


def _dsml_calls(text):
    """Scan content for DSML tool calls. [{name, arguments, span: (s, e)}].

    Well-formed calls only, at most _DSML_MAX_CALLS; [] when the text carries
    none. Attribute values keep their JSON type when they parse ("..."-quoted
    scalars, arrays); repeated attributes become a list; a STRING "files"
    value is normalized to a path list because the finish tool schema wants
    an array and models emit both files="a b" and files="a, b".
    """
    if not isinstance(text, str) or _DSML_PREFIX not in text:
        return []
    found = []
    pos = text.find(_DSML_PREFIX)
    while pos >= 0 and len(found) < _DSML_MAX_CALLS:
        name_match = _DSML_NAME_RE.match(text, pos + len(_DSML_PREFIX))
        if not name_match:
            pos = text.find(_DSML_PREFIX, pos + 1)
            continue
        name = name_match.group(0)
        i = name_match.end()
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        # HTML-ish opener: <｜DSML｜:finish>{json}</｜DSML｜:finish>
        opened = text.startswith(">", i)
        if opened:
            i += 1
            while i < len(text) and text[i] in " \t\r\n":
                i += 1
        arguments = None
        end = 0
        if text.startswith("{", i):
            close = _balanced_json_end(text, i)
            if close:
                try:
                    parsed = json.loads(text[i:close])
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    arguments = parsed
                    tag = "</%s%s>" % (_DSML_PREFIX, name)
                    end = close + (len(tag) if text.startswith(tag, close) else 0)
        if arguments is None:
            # attribute form: scan to the first '>' outside double quotes
            j = i
            in_str = False
            esc = False
            while j < len(text):
                ch = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch == ">":
                    break
                j += 1
            if j >= len(text):
                if not opened:
                    pos = text.find(_DSML_PREFIX, pos + 1)
                    continue
                # a bare <｜DSML｜:name> opener with nothing else: an empty
                # call the tool layer answers with a schema error (better
                # feedback to the model than dropping it silently)
                arguments, end = {}, i
            elif j - i > _DSML_SEGMENT_CHARS:
                pos = text.find(_DSML_PREFIX, pos + 1)
                continue
            else:
                arguments = {}
                for match in _DSML_ATTR_RE.finditer(text[i:j]):
                    key = match.group(1)
                    raw = match.group(2)
                    if raw is None:  # unquoted [...] value
                        raw = match.group(3)
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        value = raw
                    if key in arguments:
                        previous = arguments[key]
                        if not isinstance(previous, list):
                            arguments[key] = [previous]
                        arguments[key].append(value)
                    else:
                        arguments[key] = value
                end = j + 1
        files = arguments.get("files")
        if isinstance(files, str):
            # "a b", "a, b" and even an unparsable "[a, b]" all normalize to
            # the path list the finish schema expects
            arguments["files"] = [p for p in re.split(r"[,\s]+",
                                                      files.strip("[]")) if p]
        found.append({"name": name, "arguments": arguments, "span": (pos, end)})
        pos = text.find(_DSML_PREFIX, end) if end > pos else pos + 1
    return found


def parse_dsml_tool_calls(text):
    """DSML tool calls lifted into the flat internal tool-call shape
    [{id, type, name, arguments}] (arguments re-serialized as JSON)."""
    return [{"id": "call_dsml_%d" % n, "type": "function",
             "name": call["name"],
             "arguments": json.dumps(call["arguments"], ensure_ascii=False)}
            for n, call in enumerate(_dsml_calls(text))]


class FatalLLMError(Exception):
    """Endpoint error that retries cannot fix (or retries exhausted)."""


class ContextOverflowError(FatalLLMError):
    """HTTP 400: the request exceeds the provider's context/input-token limit.

    Retrying the identical body can never succeed - the caller must shrink
    the request first. `.limit` carries the provider-reported window size
    (input tokens) when parseable, so the caller can adapt its budget.
    """

    def __init__(self, message, limit=None):
        FatalLLMError.__init__(self, message)
        self.limit = limit


def _is_context_overflow(detail):
    text = detail.lower()
    for marker in _OVERFLOW_MARKERS:
        if marker in text:
            return True
    for first, second in _OVERFLOW_PAIRS:
        if first in text and second in text:
            return True
    return False


def _context_limit(detail):
    match = _LIMIT_RE.search(detail)
    return int(match.group(1)) if match else None


def _log(message):
    print("[llm] %s" % message, flush=True)


class _Heartbeat(object):
    """Prints a periodic line while an LLM request is in flight.

    A single round-trip on a reasoning model (or a retry backoff during a
    provider outage) regularly runs minutes with no other output - from the
    outside that is indistinguishable from a hang. A daemon thread emits one
    line per `interval` seconds; `interval <= 0` disables the heartbeat.
    """

    def __init__(self, model, interval):
        self._model = model
        self._interval = max(0, int(interval or 0))
        self._stop = threading.Event()
        self._thread = None
        self._start = time.monotonic()

    def __enter__(self):
        if self._interval > 0:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self._interval):
            _log("waiting for %s: %ds" % (self._model,
                                          int(time.monotonic() - self._start)))

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


class ChatClient(object):
    def __init__(self, base_url, api_key, model, timeout=180, retries=4,
                 temperature=None, max_tokens=0, extra_body=None,
                 heartbeat_seconds=60):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key or ""
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra_body = dict(extra_body or {})
        self.heartbeat_seconds = max(0, int(heartbeat_seconds or 0))

    # -- public -------------------------------------------------------------

    def chat(self, messages, tools):
        """One round-trip. Returns {message, tool_calls, finish_reason, usage}."""
        payload = self.build_payload(messages, tools)
        body = json.dumps(payload).encode("utf-8")

        last_error = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(
                self.url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "vibe-agent/1.0",
                },
            )
            if self.api_key:
                request.add_header("Authorization", "Bearer " + self.api_key)
            try:
                with _Heartbeat(self.model, self.heartbeat_seconds), \
                        urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return self._parse(data)
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:400].decode("utf-8", "replace")
                if exc.code == 400 and _is_context_overflow(detail):
                    raise ContextOverflowError(
                        "HTTP 400 from %s: %s" % (self.url, detail),
                        _context_limit(detail))
                if exc.code in RETRYABLE_HTTP and attempt < self.retries:
                    last_error = "HTTP %d: %s" % (exc.code, detail)
                    delay = self._backoff(attempt,
                                          exc.headers.get("Retry-After"),
                                          http=True)
                    _log("HTTP %d from %s - retry %d/%d in %.0fs"
                         % (exc.code, self.model, attempt + 1, self.retries,
                            delay))
                    time.sleep(delay)
                    continue
                raise FatalLLMError("HTTP %d from %s: %s" % (exc.code, self.url, detail))
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                if attempt < self.retries:
                    last_error = "connection error: %s" % exc
                    delay = self._backoff(attempt, None)
                    _log("connection error (%s) - retry %d/%d in %.0fs"
                         % (exc, attempt + 1, self.retries, delay))
                    time.sleep(delay)
                    continue
                raise FatalLLMError("connection error (retries exhausted): %s" % exc)
            except json.JSONDecodeError as exc:
                if attempt < self.retries:
                    last_error = "invalid JSON response: %s" % exc
                    time.sleep(self._backoff(attempt, None))
                    continue
                raise FatalLLMError("invalid JSON response: %s" % exc)
        raise FatalLLMError("unreachable after retries: %s" % last_error)

    # -- internals ----------------------------------------------------------

    def build_payload(self, messages, tools):
        payload = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        # Provider-specific extras (e.g. vLLM chat_template_kwargs for Qwen
        # enable_thinking). Never override the core fields we set ourselves.
        for key, value in self.extra_body.items():
            payload.setdefault(key, value)
        return payload

    @staticmethod
    def _backoff(attempt, retry_after, http=False):
        """Wait before the next retry.

        HTTP-level failures (503 "no available server", overloads) are far
        longer-lived than connection resets, so they follow a patient fixed
        schedule (1, 2, 4, 8, 16 minutes); an explicit Retry-After header wins,
        capped at the schedule maximum. Connection errors keep the fast
        exponential curve (they are almost always transient).
        """
        try:
            if retry_after:
                return min(HTTP_BACKOFF_SECONDS[-1], float(retry_after))
        except (TypeError, ValueError):
            pass
        if http:
            index = min(attempt, len(HTTP_BACKOFF_SECONDS) - 1)
            return float(HTTP_BACKOFF_SECONDS[index])
        return min(60.0, float(2 ** attempt)) * (0.5 + random.random())

    @staticmethod
    def _rm_think(text):
        """Strip inline <think>...</think> blocks some Qwen backends leave in
        content (same heuristic as Qwen-Agent's _rm_think). No-op elsewhere."""
        if isinstance(text, str) and "</think>" in text:
            return text.rsplit("</think>", 1)[-1].lstrip("\n")
        return text

    @staticmethod
    def _parse(data):
        try:
            choice = data["choices"][0]
            message = choice["message"] or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise FatalLLMError("unexpected response shape: %r" % exc)
        # Keep only the standard fields: reasoning models (e.g. qwen3) add
        # reasoning_content, which strict gateways reject on the next request.
        content = ChatClient._rm_think(message.get("content"))
        clean = {"role": message.get("role") or "assistant", "content": content}
        # Flat internal shape: name/arguments at the top level. agent.py and
        # the transcript iterate tool_calls with call["name"]/["arguments"]/["id"].
        tool_calls = []
        for call in (message.get("tool_calls") or []):
            function = call.get("function") or {}
            tool_calls.append({
                "id": call.get("id") or "call_0",
                "type": "function",
                "name": function.get("name") or "",
                "arguments": function.get("arguments") or "{}",
            })
        if not tool_calls:
            lifted = _dsml_calls(content)
            if lifted:
                # DSML-in-content fallback: lift the calls so the agent loop
                # sees real tool calls, and strip their spans from the echoed
                # content so the resent history keeps only the narration text
                # (the calls themselves travel via clean["tool_calls"]).
                _log("%d DSML tool call(s) lifted from content into tool_calls"
                     % len(lifted))
                stripped = content
                for call in reversed(lifted):
                    start, end = call["span"]
                    stripped = stripped[:start] + stripped[end:]
                clean["content"] = stripped.strip()
                tool_calls = [
                    {"id": "call_dsml_%d" % n, "type": "function",
                     "name": call["name"],
                     "arguments": json.dumps(call["arguments"],
                                             ensure_ascii=False)}
                    for n, call in enumerate(lifted)
                ]
        if tool_calls:
            # OpenAI-compatible serialization for the NEXT request: strict
            # gateways (e.g. api.ai.gnivc.ru, Rust/Serde) reject the flattened
            # top-level name/arguments as "missing field `function`" once the
            # assistant's tool calls are echoed back with the tool results.
            # Re-nest them under "function" for the message body only.
            clean["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": call["arguments"],
                    },
                }
                for call in tool_calls
            ]
        elif clean["content"] is None:
            clean["content"] = ""
        usage = data.get("usage") or {}
        return {
            "message": clean,
            "content": content,
            "tool_calls": tool_calls,
            "finish_reason": choice.get("finish_reason"),
            "usage": usage,
        }
