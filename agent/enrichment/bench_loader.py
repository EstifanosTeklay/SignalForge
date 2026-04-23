"""
agent/enrichment/bench_loader.py

Loads the Tenacious delivery bench summary from seed/bench_summary.json.

The agent must reference actual counts and never commit to capacity the bench
does not show. This module is the single source of truth for that constraint.

Path resolution order:
  1. BENCH_SUMMARY_JSON env var
  2. data/Tenacious Data/tenacious_sales_data/tenacious_sales_data/seed/bench_summary.json
  3. data/bench_summary.json  (fallback copy at repo root)
"""

from __future__ import annotations

import json
import os
from typing import Optional

_DEFAULT_SEED_PATH = os.path.join(
    "data",
    "Tenacious Data",
    "tenacious_sales_data",
    "tenacious_sales_data",
    "seed",
    "bench_summary.json",
)
_FALLBACK_PATH = os.path.join("data", "bench_summary.json")

_BENCH_SUMMARY_PATH = os.getenv("BENCH_SUMMARY_JSON", _DEFAULT_SEED_PATH)


def _load() -> dict:
    for path in (_BENCH_SUMMARY_PATH, _FALLBACK_PATH):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    # Return a safe empty structure so callers don't crash on missing file
    return {"stacks": {}, "total_engineers_on_bench": 0}


# Module-level singleton loaded once at import time
BENCH: dict = _load()


def available_count(stack: str) -> int:
    """Return number of available engineers for a given stack name (case-insensitive)."""
    stack_lower = stack.lower().replace(" ", "_").replace("-", "_")
    stacks = BENCH.get("stacks", {})
    for key, val in stacks.items():
        if key.lower() == stack_lower:
            return val.get("available_engineers", 0)
    return 0


def stack_names() -> list[str]:
    """Return the list of stack names the bench currently supports."""
    return list(BENCH.get("stacks", {}).keys())


def check_bench_match(required_stacks: list[str]) -> dict:
    """
    Check whether every required stack has at least one available engineer.

    Returns:
        {
            "bench_available": bool,
            "gaps": list[str],          # stacks with zero availability
            "counts": {stack: int},     # available count per required stack
        }
    """
    gaps: list[str] = []
    counts: dict[str, int] = {}

    for stack in required_stacks:
        n = available_count(stack)
        counts[stack] = n
        if n == 0:
            gaps.append(stack)

    return {
        "bench_available": len(gaps) == 0,
        "gaps": gaps,
        "counts": counts,
    }


def bench_description() -> str:
    """One-line human-readable bench capacity summary for prompts."""
    stacks = BENCH.get("stacks", {})
    parts = []
    for name, info in stacks.items():
        n = info.get("available_engineers", 0)
        if n > 0:
            parts.append(f"{name} ({n})")
    total = BENCH.get("total_engineers_on_bench", 0)
    if not parts:
        return "Bench summary unavailable."
    return f"Available engineers: {', '.join(parts)}. Total on bench: {total}."


def get_as_of() -> str:
    return BENCH.get("as_of", "unknown")
