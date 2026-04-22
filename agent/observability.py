"""
agent/observability.py

Langfuse tracing wrapper.
Every agent action — enrichment, email, SMS, CRM update, booking —
must produce a trace for the evidence graph and cost attribution.
"""

import os
from functools import wraps
from langfuse import Langfuse
from dotenv import load_dotenv

load_dotenv()

_client = Langfuse(
    public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
    secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
    host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
)


def trace_action(name: str, metadata: dict = None):
    """
    Decorator — wraps any agent function in a Langfuse observation.

    Usage:
        @trace_action(name="enrich_prospect")
        def enrich(company_name): ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with _client.start_as_current_observation(
                name=name,
                metadata=metadata or {},
            ) as obs:
                try:
                    result = func(*args, **kwargs)
                    obs.update(output=str(result)[:500])  # truncate large outputs
                    return result
                except Exception as e:
                    obs.update(level="ERROR", status_message=str(e))
                    raise
        return wrapper
    return decorator


def flush():
    """Call at end of each pipeline run to ensure all traces are sent."""
    _client.flush()


# ── Quick smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":

    @trace_action(name="test-action", metadata={"source": "smoke-test"})
    def dummy_action(x: int) -> int:
        return x * 2

    result = dummy_action(21)
    print(f"Result: {result}")
    flush()
    print("Trace sent to Langfuse.")
