"""
agent/llm_client.py

Anthropic SDK wrapper with prompt caching.
All calls are traced to Langfuse for cost attribution.

Model routing:
  - dev tier  (Days 1-4): ANTHROPIC_DEV_MODEL  (default: claude-haiku-4-5)
  - eval tier (Days 5-7): ANTHROPIC_EVAL_MODEL (default: claude-sonnet-4-6)

Prompt caching:
  System prompts longer than _CACHE_THRESHOLD chars get cache_control: ephemeral,
  which reduces repeated-call cost by ~90% once the cache is warm.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import anthropic
from dotenv import load_dotenv

load_dotenv()

_client: anthropic.Anthropic | None = None
_CACHE_THRESHOLD = 512  # cache system prompts longer than this (chars)


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _model(tier: str = "dev") -> str:
    if tier == "eval":
        return os.getenv("ANTHROPIC_EVAL_MODEL", "claude-sonnet-4-6")
    return os.getenv("ANTHROPIC_DEV_MODEL", "claude-haiku-4-5")


def _extract_system(
    messages: list[dict],
) -> tuple[list[dict] | str | None, list[dict]]:
    """
    Pull the leading system message out of the messages list so it can be
    passed as Anthropic's top-level `system` parameter.
    Adds cache_control to large system prompts to reduce cost on repeated calls.
    """
    if messages and messages[0].get("role") == "system":
        system_text = messages[0]["content"]
        rest = messages[1:]
        if len(system_text) > _CACHE_THRESHOLD:
            system_param: list[dict] | str = [
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_param = system_text
        return system_param, rest
    return None, messages


def chat(
    messages: list[dict],
    *,
    tier: str = "dev",
    temperature: float = 0.3,
    max_tokens: int = 1024,
    response_format: dict | None = None,  # accepted but ignored; use JSON in prompt
    trace_name: str = "llm_call",
    trace_metadata: dict | None = None,
) -> str:
    """
    Send a messages request. Returns the text content string.
    Traces to Langfuse if LANGFUSE_SECRET_KEY is set.
    """
    model = _model(tier)
    client = _get_client()

    system_param, user_messages = _extract_system(messages)

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": user_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system_param is not None:
        kwargs["system"] = system_param

    from agent.observability import log_generation

    t0 = time.perf_counter()
    try:
        resp = client.messages.create(**kwargs)
        content = resp.content[0].text if resp.content else ""

        log_generation(
            name=trace_name,
            model=model,
            input_messages=messages,
            output=content,
            usage={
                "input": resp.usage.input_tokens,
                "output": resp.usage.output_tokens,
            },
            metadata={
                **(trace_metadata or {}),
                "latency_ms": round((time.perf_counter() - t0) * 1000),
                "tier": tier,
                "cache_read_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
                "cache_write_tokens": getattr(
                    resp.usage, "cache_creation_input_tokens", 0
                ),
            },
        )
        return content

    except Exception:
        raise


def chat_json(
    messages: list[dict],
    *,
    tier: str = "dev",
    temperature: float = 0.2,
    max_tokens: int = 2048,
    trace_name: str = "llm_json_call",
    trace_metadata: dict | None = None,
) -> dict:
    """
    Request JSON output. Parses and returns the dict.
    Falls back to extracting the first {...} block from free-form text.
    """
    raw = chat(
        messages,
        tier=tier,
        temperature=temperature,
        max_tokens=max_tokens,
        trace_name=trace_name,
        trace_metadata=trace_metadata,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(raw[start:end])
        raise ValueError(f"LLM did not return valid JSON: {raw[:200]}")
