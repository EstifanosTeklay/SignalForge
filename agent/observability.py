"""
agent/observability.py

Langfuse 4.x tracing wrapper.
Uses the @observe decorator pattern (Langfuse 4.x dropped the old .trace() API).

If LANGFUSE_SECRET_KEY is not set, all tracing is a no-op so the pipeline
runs without a cloud account.
"""

import os
from functools import wraps
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

_LANGFUSE_ENABLED = bool(
    os.getenv("LANGFUSE_SECRET_KEY") and os.getenv("LANGFUSE_PUBLIC_KEY")
)

_langfuse_instance = None


def _get_langfuse():
    global _langfuse_instance
    if not _LANGFUSE_ENABLED:
        return None
    if _langfuse_instance is None:
        try:
            from langfuse import Langfuse
            _langfuse_instance = Langfuse(
                public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
                secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
                host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            )
        except Exception as exc:
            print(f"[observability] Langfuse init failed (no-op mode): {exc}")
    return _langfuse_instance


def get_client():
    return _get_langfuse()


def get_tracer():
    """Compat shim — in Langfuse 4.x context is implicit via @observe."""
    return _get_langfuse()


def start_trace(name: str, metadata: Optional[dict] = None):
    """
    No-op in Langfuse 4.x (tracing is handled implicitly by @observe).
    Kept for API compatibility with pipeline.py.
    """
    lf = _get_langfuse()
    if lf is None:
        return None
    # In 4.x there is no explicit trace() call; just return the client
    return lf


def flush():
    """Flush all buffered events to Langfuse."""
    lf = _get_langfuse()
    if lf:
        try:
            lf.flush()
        except Exception:
            pass


def traced(name: str, metadata: Optional[dict] = None):
    """
    Decorator: wraps a function in a Langfuse observation using @observe.
    Falls back to a no-op if Langfuse is not configured.

    Usage:
        @traced("research_agent.run")
        def run(self, company_name): ...
    """
    def decorator(func):
        if not _LANGFUSE_ENABLED:
            return func  # zero-overhead no-op

        try:
            from langfuse.decorators import langfuse_context, observe

            @observe(name=name)
            @wraps(func)
            def wrapper(*args, **kwargs):
                if metadata:
                    langfuse_context.update_current_observation(metadata=metadata)
                return func(*args, **kwargs)

            return wrapper

        except ImportError:
            # langfuse.decorators not available — graceful no-op
            return func

    return decorator


def trace_action(name: str, metadata: dict = None):
    """Legacy alias — prefer @traced() in new code."""
    return traced(name, metadata)


def log_generation(
    name: str,
    model: str,
    input_messages: list,
    output: str,
    usage: dict,
    metadata: Optional[dict] = None,
):
    """
    Log an LLM generation directly (used by llm_client.py).
    Works with Langfuse 4.x context-based API.
    """
    if not _LANGFUSE_ENABLED:
        return
    try:
        from langfuse.decorators import langfuse_context
        langfuse_context.update_current_observation(
            name=name,
            model=model,
            input=input_messages,
            output=output,
            usage=usage,
            metadata=metadata or {},
        )
    except Exception:
        # Context not active (called outside @observe scope) — silently ignore
        pass


# ── Quick smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    @traced("smoke-test")
    def dummy(x: int) -> int:
        return x * 2

    result = dummy(21)
    print(f"Result: {result}")
    flush()
    print("Langfuse enabled:", _LANGFUSE_ENABLED)
