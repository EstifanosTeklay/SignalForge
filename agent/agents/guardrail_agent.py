"""
agent/agents/guardrail_agent.py

🛑 Agent 5 — Guardrail Agent

Role:
  - Checks every outgoing message before it is sent
  - Three checks (run in a single LLM call for cost efficiency):
      1. Tone check: does the draft match Tenacious style guide?
      2. Claim check: does any claim exceed what the signals support?
      3. Bench check: does the email commit to capacity the bench does not show?
  - Returns: approved bool + corrected draft + flags list

This is a significant differentiator. Tenacious's brand constraint is
honesty — one over-claimed email can damage a prospect relationship
more than silence would.

Scoring:
  PASS  — no flags, email sent as-is
  WARN  — minor flags, email sent with auto-corrections noted
  BLOCK — substantive over-claim or tone violation; email regenerated
"""

from __future__ import annotations

import os
import time
from typing import Optional

from agent.llm_client import chat_json
from agent.observability import traced

# Tenacious bench capacity (loaded from bench_summary.json or env)
# In production this is read from the seed repo's bench summary
_DEFAULT_BENCH = {
    "python": 8,
    "go": 4,
    "data_engineering": 6,
    "ml_ai": 5,
    "infrastructure": 4,
}

_STYLE_VIOLATIONS = [
    "leverage", "synergies", "excited to share", "game-changing",
    "unlock", "revolutionize", "disruptive", "world-class",
    "best-in-class", "bleeding-edge", "cutting-edge",
]

_OVER_CLAIM_PATTERNS = [
    "aggressive hiring",  # requires HIGH velocity signal
    "tripled",            # requires two snapshots 60 days apart
    "explosive growth",
    "rapidly scaling",
    "guarantee",
    "ensure",
    "will definitely",
    "always available",   # bench over-commitment
]


class GuardrailAgent:
    """
    Validates a draft email against tone, honesty, and bench constraints.
    Single LLM call with structured JSON output.
    """

    def __init__(self, llm_tier: str = "dev", bench: Optional[dict] = None):
        self.llm_tier = llm_tier
        self.bench = bench or _load_bench()

    @traced("guardrail_agent.check")
    def check(
        self,
        email: dict,
        brief_dict: Optional[dict] = None,
    ) -> dict:
        """
        Run all three guardrail checks on a draft email.

        Args:
            email:      Output from MessageAgent.run()
            brief_dict: hiring_signal_brief dict for claim verification

        Returns:
            {
              "verdict": "PASS" | "WARN" | "BLOCK",
              "flags": [...],
              "corrected_body": str | None,
              "corrected_subject": str | None,
              "bench_ok": bool,
            }
        """
        t0 = time.perf_counter()

        text_body = email.get("text_body", "")
        subject = email.get("subject", "")
        company = email.get("company_name", "unknown")

        # ── Fast deterministic checks (no LLM cost) ───────────────────────────
        flags = []

        # Style violations
        text_lower = text_body.lower()
        style_hits = [w for w in _STYLE_VIOLATIONS if w in text_lower]
        if style_hits:
            flags.append({
                "type": "tone",
                "severity": "WARN",
                "detail": f"Style guide violations found: {', '.join(style_hits)}",
            })

        # Over-claim patterns
        over_claims = [p for p in _OVER_CLAIM_PATTERNS if p in text_lower]
        if over_claims:
            flags.append({
                "type": "over_claim",
                "severity": "BLOCK",
                "detail": f"Potential over-claim language: {', '.join(over_claims)}",
            })

        # Bench commitment check
        bench_ok = self._check_bench_commitment(text_body)
        if not bench_ok:
            flags.append({
                "type": "bench_over_commitment",
                "severity": "BLOCK",
                "detail": (
                    "Email appears to commit to specific capacity not confirmed "
                    "in bench summary. Route to human before sending."
                ),
            })

        # If there are BLOCK-level flags, route through LLM for correction
        has_block = any(f["severity"] == "BLOCK" for f in flags)

        if has_block or flags:
            corrected = self._llm_correct(email, brief_dict, flags)
        else:
            corrected = None

        verdict = (
            "BLOCK" if has_block
            else "WARN" if flags
            else "PASS"
        )

        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        print(
            f"[guardrail_agent] '{company}' verdict={verdict} | "
            f"flags={len(flags)} | {elapsed_ms}ms"
        )

        return {
            "verdict": verdict,
            "flags": flags,
            "corrected_body": corrected.get("text_body") if corrected else None,
            "corrected_subject": corrected.get("subject") if corrected else None,
            "bench_ok": bench_ok,
            "original_subject": subject,
        }

    def _check_bench_commitment(self, text: str) -> bool:
        """
        Return False if the email makes a specific headcount commitment
        that is not supported by the bench summary.
        Heuristic: look for patterns like 'X engineers ready' or 'team of N'.
        """
        import re
        text_lower = text.lower()
        # Patterns that imply a specific capacity commitment
        commitment_patterns = [
            r"\b(\d+)\s+engineers?\s+(available|ready|on bench)\b",
            r"team of\s+(\d+)",
            r"(\d+)\s+developers?\s+ready",
        ]
        for pattern in commitment_patterns:
            matches = re.findall(pattern, text_lower)
            if matches:
                # Extract number and check against bench
                for match in matches:
                    n = int(match[0]) if isinstance(match, tuple) else int(match)
                    total_bench = sum(self.bench.values())
                    if n > total_bench:
                        return False
        return True

    def _llm_correct(
        self, email: dict, brief_dict: Optional[dict], flags: list[dict]
    ) -> dict:
        """
        Ask the LLM to fix flagged issues while preserving the grounded claims.
        """
        flag_descriptions = "\n".join(
            f"- [{f['severity']}] {f['type']}: {f['detail']}" for f in flags
        )
        brief_summary = ""
        if brief_dict:
            funding = brief_dict.get("funding", {})
            hiring = brief_dict.get("hiring", {})
            brief_summary = (
                f"Verified signals: "
                f"funding={funding.get('has_recent_funding', False)}, "
                f"open_roles={hiring.get('open_roles_count', 0)}, "
                f"ai_roles={hiring.get('ai_adjacent_role_count', 0)}"
            )

        system_prompt = """You are a compliance editor for a B2B sales team.
Fix the draft email based on the flags listed. Rules:
- Remove or soften any over-claim language.
- Replace style-guide violations with direct, peer-to-peer alternatives.
- Never add new claims — only use what is in the brief.
- Keep the email under 150 words.
- Return JSON: {"subject": "...", "text_body": "..."}"""

        user_prompt = f"""Draft email subject: {email.get('subject', '')}
Draft email body:
{email.get('text_body', '')}

Flags to fix:
{flag_descriptions}

Verified signals available:
{brief_summary or '(brief not provided)'}

Return a corrected version."""

        try:
            return chat_json(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tier=self.llm_tier,
                max_tokens=500,
                trace_name="guardrail_agent.correct",
            )
        except Exception as exc:
            print(f"[guardrail_agent] Correction LLM failed: {exc}")
            # Manual fix: strip the most obvious violations
            fixed = email.get("text_body", "")
            for v in _STYLE_VIOLATIONS + _OVER_CLAIM_PATTERNS:
                fixed = fixed.replace(v, "")
            return {"subject": email.get("subject", ""), "text_body": fixed}


def _load_bench() -> dict:
    """Load bench summary from bench_summary.json or fall back to defaults."""
    bench_path = os.getenv("BENCH_SUMMARY_PATH", "data/bench_summary.json")
    if os.path.exists(bench_path):
        import json
        with open(bench_path) as f:
            data = json.load(f)
        return data.get("available_by_stack", _DEFAULT_BENCH)
    return _DEFAULT_BENCH
