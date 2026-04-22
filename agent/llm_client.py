"""
agent/llm_client.py

Thin wrapper around OpenAI SDK pointed at OpenRouter.
All calls are traced to Langfuse for cost attribution.

Model routing:
  - dev tier  (Days 1-4): OPENROUTER_DEV_MODEL  (default: deepseek/deepseek-chat-v3-5)
  - eval tier (Days 5-7): OPENROUTER_EVAL_MODEL (default: anthropic/claude-sonnet-4-6)
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "HTTP-Referer": "https://github.com/SignalForge/tenacious-agent",
                "X-Title": "SignalForge / Tenacious Lead Agent",
            },
        )
    return _client


def _model(tier: str = "dev") -> str:
    if tier == "eval":
        return os.getenv("OPENROUTER_EVAL_MODEL", "anthropic/claude-sonnet-4-6")
    return os.getenv("OPENROUTER_DEV_MODEL", "deepseek/deepseek-chat-v3-5")


def chat(
    messages: list[dict],
    *,
    tier: str = "dev",
    temperature: float = 0.3,
    max_tokens: int = 1024,
    response_format: dict | None = None,
    trace_name: str = "llm_call",
    trace_metadata: dict | None = None,
) -> str:
    """
    Send a chat completion request. Returns the content string.
    Traces to Langfuse if LANGFUSE_SECRET_KEY is set.
    """
    # observability imported lazily inside chat() to avoid circular imports

    model = _model(tier)
    client = _get_client()

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        kwargs["response_format"] = response_format

    from agent.observability import log_generation
    t0 = time.perf_counter()

    try:
        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or ""
        usage = resp.usage

        log_generation(
            name=trace_name,
            model=model,
            input_messages=messages,
            output=content,
            usage={
                "input": usage.prompt_tokens if usage else 0,
                "output": usage.completion_tokens if usage else 0,
            },
            metadata={
                **(trace_metadata or {}),
                "latency_ms": round((time.perf_counter() - t0) * 1000),
                "tier": tier,
            },
        )
        return content

    except Exception as exc:
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
    Falls back to extracting a JSON block from free-form text.
    """
    raw = chat(
        messages,
        tier=tier,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        trace_name=trace_name,
        trace_metadata=trace_metadata,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: find first {...} block
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(raw[start:end])
        raise ValueError(f"LLM did not return valid JSON: {raw[:200]}")
