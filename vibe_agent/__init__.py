"""vibe-agent — a minimal, single-purpose LLM agent for the vibedocing pipeline.

Stdlib-only (no third-party Python packages). Speaks the OpenAI-compatible
chat-completions API, so any compliant endpoint works: OpenAI, OpenRouter,
DeepSeek, Groq, vLLM, Ollama, LiteLLM, Anthropic's compat endpoint, etc.

The agent gets exactly one commit (checked out in a disposable worktree),
classifies it (DOCUMENT vs SKIP), optionally updates the documentation map
under agent/project/, and emits a verdict file for the outer bash loop.
"""

__version__ = "1.0.0"
