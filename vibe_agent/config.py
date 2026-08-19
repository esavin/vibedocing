"""Config resolution. Precedence: CLI flags > environment > config.json > defaults."""

import json
import os

DEFAULT_BASE_URL = "https://api.openai.com/v1"

LLM_DEFAULTS = {
    "base_url": DEFAULT_BASE_URL,
    "api_key_env": "VIBE_API_KEY",
    "model": "",
    "temperature": None,
    "max_tokens": 0,
    "max_steps": 24,
    "max_steps_initial": 0,
    "max_steps_cap": 48,
    "request_timeout_seconds": 180,
    "retries": 5,
    "extra_body": {},
    "log_transcript": True,
}


class ConfigError(Exception):
    """Fatal misconfiguration — the agent cannot start."""


def load_config(path):
    """Load config.json (the pipeline's config) as a dict; {} if absent."""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("cannot read config %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ConfigError("config %s must be a JSON object" % path)
    return data


def resolve_llm(config, cli_model=None, cli_max_steps=None):
    """Merge llm settings from config.json, environment, and CLI overrides."""
    section = config.get("llm")
    if not isinstance(section, dict):
        section = {}
    merged = dict(LLM_DEFAULTS)
    merged.update({k: v for k, v in section.items() if v not in (None, "")})

    base_url = os.environ.get("VIBE_BASE_URL") or merged["base_url"]
    model = cli_model or os.environ.get("VIBE_MODEL") or merged["model"]
    if not model:
        raise ConfigError(
            "no model configured - set llm.model in config.json, export VIBE_MODEL,"
            " or pass --model"
        )

    api_key = os.environ.get("VIBE_API_KEY", "")
    if not api_key and merged["api_key_env"]:
        api_key = os.environ.get(merged["api_key_env"], "")
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY", "")

    try:
        max_steps = int(cli_max_steps or merged["max_steps"])
        max_steps_initial = int(merged["max_steps_initial"] or 0)
        max_steps_cap = int(merged["max_steps_cap"] or 48)
        timeout = int(merged["request_timeout_seconds"])
        retries = int(merged["retries"])
        max_tokens = int(merged["max_tokens"] or 0)
    except (TypeError, ValueError):
        raise ConfigError("llm numeric settings must be integers")

    temperature = merged["temperature"]
    if temperature is not None:
        try:
            temperature = float(temperature)
        except (TypeError, ValueError):
            raise ConfigError("llm.temperature must be a number or null")

    extra_body = merged["extra_body"]
    if extra_body is None:
        extra_body = {}
    if not isinstance(extra_body, dict):
        raise ConfigError("llm.extra_body must be a JSON object")

    log_transcript = merged["log_transcript"]
    if not isinstance(log_transcript, bool):
        log_transcript = str(log_transcript).strip().lower() \
            not in ("false", "0", "no", "off", "")

    return {
        "base_url": base_url.rstrip("/"),
        "model": model,
        "api_key": api_key,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "max_steps": max(1, max_steps),
        "max_steps_initial": max(0, max_steps_initial),
        "max_steps_cap": max(1, max_steps_cap),
        "timeout": max(30, timeout),
        "retries": max(0, retries),
        "extra_body": extra_body,
        "log_transcript": log_transcript,
    }
