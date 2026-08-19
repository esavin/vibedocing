"""OpenAI-compatible chat-completions client over stdlib urllib.

Non-streaming by design: the agent is headless and single-request-per-step, so a
plain POST with retries is all that is needed. Swapping this module for the
`openai` SDK later would not affect the rest of the package.
"""

import json
import random
import time
import urllib.error
import urllib.request

RETRYABLE_HTTP = {429, 500, 502, 503, 504}
# Provider outages (503 "no available server") span minutes: wait 1, 2, 4, 8,
# 16 minutes between HTTP-level retries instead of burning them within seconds.
HTTP_BACKOFF_SECONDS = (60, 120, 240, 480, 960)


class FatalLLMError(Exception):
    """Endpoint error that retries cannot fix (or retries exhausted)."""


class ChatClient(object):
    def __init__(self, base_url, api_key, model, timeout=180, retries=4,
                 temperature=None, max_tokens=0, extra_body=None):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key or ""
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra_body = dict(extra_body or {})

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
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return self._parse(data)
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:400].decode("utf-8", "replace")
                if exc.code in RETRYABLE_HTTP and attempt < self.retries:
                    last_error = "HTTP %d: %s" % (exc.code, detail)
                    time.sleep(self._backoff(attempt,
                                             exc.headers.get("Retry-After"),
                                             http=True))
                    continue
                raise FatalLLMError("HTTP %d from %s: %s" % (exc.code, self.url, detail))
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                if attempt < self.retries:
                    last_error = "connection error: %s" % exc
                    time.sleep(self._backoff(attempt, None))
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
            "tools": tools,
            "tool_choice": "auto",
        }
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
