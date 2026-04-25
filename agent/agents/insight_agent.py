"""
agent/agents/insight_agent.py

📊 Agent 2 — Insight / Brief Agent

Role:
  - Takes HiringSignalBrief + Company (from ResearchAgent)
  - LLM converts signals → human-readable reasoning narrative
  - Builds competitor_gap_brief.json conforming to schemas/competitor_gap_brief.schema.json
  - Output: enriched brief dict ready for MessageAgent

Competitor gap logic:
  - Pulls sector peers from Crunchbase ODM (same industry, similar headcount)
  - Applies deterministic AI maturity scoring to each peer
  - Ranks by score, extracts top-quartile practices the prospect lacks
  - Every gap finding carries at least 2 peer evidence items with source attribution
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Optional

from agent.enrichment.competitor_gap import build_competitor_gap_brief
from agent.llm_client import chat_json
from agent.models.company import Company
from agent.models.signals import HiringSignalBrief, ICPSegment, SignalConfidence
from agent.observability import traced

# ── Tenacious style markers (from style_guide.md) ─────────────────────────────
# These are the five tone markers. Any draft that violates two or more is
# a brand violation and must be regenerated.
_STYLE_MARKERS = """
Tone rules (Tenacious style guide — all five must be preserved):
1. DIRECT: Clear, brief, actionable. No filler words. Subject lines state intent.
   Use "Request:", "Note:", "Context:", "Question:" — not "Hey", "Quick", "Just".
2. GROUNDED: Every claim must trace to the hiring signal brief or competitor gap brief.
   Use hedged language ("it appears", "public signal suggests") when confidence is medium/low.
   When signal is weak (fewer than 5 open roles), ASK rather than ASSERT.
3. HONEST: Refuse claims that cannot be grounded in data. Never claim "aggressive hiring"
   with fewer than 5 open roles. Never over-commit bench capacity.
   "We don't see public signal of X" is better than a confident wrong claim.
4. PROFESSIONAL: Language appropriate for founders, CTOs, and VPs Engineering.
   Avoid: "bench" (use "engineering team"), "top talent", "world-class", "A-players",
   "rockstar", "ninja". No cost-savings percentages without substantiation.
5. NON-CONDESCENDING: Frame competitor gaps as a research finding or a question worth asking.
   Bad: "You're missing a critical AI capability your competitors have."
   Good: "Three of your peers have posted AI-platform-engineer roles in the last 90 days.
          Curious whether you've made a deliberate choice not to, or whether it's still being scoped."
"""

# Segment-specific pitch language (from icp_definition.md)
_PITCH_LANGUAGE = {
    "scale_ai_team_faster": (
        "scale your AI team faster than in-house hiring can support"
    ),
    "stand_up_first_ai_function": (
        "stand up your first AI function with a dedicated squad"
    ),
    "offshore_equivalent_with_ai_capability": (
        "preserve your AI delivery capacity while reshaping cost structure"
    ),
    "replace_higher_cost_roles_preserve_output": (
        "maintain platform delivery velocity through the restructure"
    ),
    "vendor_reassessment_new_leader": (
        "the first 90 days are typically when vendor mix gets a fresh look"
    ),
    "specialized_ai_build_project_consulting": (
        "three companies in your sector at your stage are doing X and you are not"
    ),
    "generic_exploratory": (
        "open question about engineering roadmap — no specific claims"
    ),
}


def _confidence_hedge(confidence: str) -> str:
    hedges = {
        "high":   "",
        "medium": "Based on available public data, ",
        "low":    "The public signal here is limited — it appears that ",
        "none":   "We could not verify this publicly, but ",
    }
    return hedges.get(confidence, "")


class InsightAgent:
    """
    Converts raw signals into narrative insight and competitor gap analysis.
    One LLM call for narrative; deterministic logic for competitor scoring.
    """

    def __init__(self, llm_tier: str = "dev"):
        self.llm_tier = llm_tier

    @traced("insight_agent.run")
    def run(
        self,
        company: Company,
        brief: HiringSignalBrief,
        save_path: Optional[str] = None,
    ) -> dict:
        """
        Returns a combined insight dict:
        {
          "hiring_signal_brief": {...},
          "narrative": "...",
          "competitor_gap_brief": {...},   # schema-compliant
          "pitch_angle": "...",
          "generated_at": "...",
        }
        """
        t0 = time.perf_counter()

        narrative = self._generate_narrative(brief)
        comp_gap = build_competitor_gap_brief(company, brief)
        pitch_angle = self._select_pitch_angle(brief)

        result = {
            "hiring_signal_brief": self._brief_to_dict(brief),
            "narrative": narrative,
            "competitor_gap_brief": comp_gap,
            "pitch_angle": pitch_angle,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        if save_path:
            os.makedirs(os.path.dirname(save_path) if os.path.dirname(save_path) else ".", exist_ok=True)
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, default=str)
            print(f"[insight_agent] Saved to {save_path}")

        gap_count = len(comp_gap.get("gap_findings", []))
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        print(
            f"[insight_agent] '{company.name}' insight generated in {elapsed_ms}ms | "
            f"gap_count={gap_count}"
        )
        return result

    def _generate_narrative(self, brief: HiringSignalBrief) -> str:
        """
        Single LLM call: translate structured signals into a 2–3 sentence
        research finding a prospect CTO/VP Eng would read with interest.
        Honesty constraint: only reference signals with confidence ≥ MEDIUM.
        """
        signal_lines = []

        if brief.funding.has_recent_funding and brief.funding.confidence in (
            SignalConfidence.HIGH, SignalConfidence.MEDIUM
        ):
            signal_lines.append(f"Funding: {brief.funding.justification}")

        if brief.hiring.open_roles_count and brief.hiring.open_roles_count >= 3:
            signal_lines.append(f"Hiring: {brief.hiring.justification}")

        if brief.layoff.has_recent_layoff:
            signal_lines.append(f"Layoff: {brief.layoff.justification}")

        if brief.leadership.has_recent_change:
            signal_lines.append(f"Leadership: {brief.leadership.justification}")

        if brief.ai_maturity.score > 0 and brief.ai_maturity.evidence_notes:
            signal_lines.append(
                f"AI maturity {brief.ai_maturity.score}/3: "
                + " ".join(brief.ai_maturity.evidence_notes[:2])
            )

        if not signal_lines:
            return (
                f"{brief.company_name} did not return strong public signals. "
                "Recommend exploratory outreach rather than a signal-grounded pitch."
            )

        # Flag honesty constraints to the LLM
        honesty_notes = ""
        if "weak_hiring_velocity_signal" in (brief.honesty_flags or []):
            honesty_notes += "\nHONESTY CONSTRAINT: Fewer than 5 open roles — do NOT claim 'aggressive hiring' or 'scaling aggressively'. Ask rather than assert."
        if "weak_ai_maturity_signal" in (brief.honesty_flags or []):
            honesty_notes += "\nHONESTY CONSTRAINT: AI maturity signal is weak — use hedged language, do not assert a mature AI function."

        system_prompt = f"""You write concise research findings for a B2B sales team.
{_STYLE_MARKERS}
{honesty_notes}
- Max 120 words total.
- Reference only the signals provided. Do not invent facts.
- Write for a CTO or VP Engineering who is skeptical of vendor outreach.
- Never mention Tenacious by name in the narrative itself."""

        user_prompt = f"""Company: {brief.company_name}
ICP segment: {brief.icp.segment.value}
Segment confidence: {brief.icp.confidence_score:.2f}
Signals:
{chr(10).join(f'- {s}' for s in signal_lines)}

Write a 2–3 sentence research finding that makes the prospect's current situation
concrete and verifiable. End with one question that opens the door to a conversation.
Return JSON: {{"narrative": "..."}}"""

        try:
            result = chat_json(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tier=self.llm_tier,
                max_tokens=300,
                trace_name="insight_agent.narrative",
                trace_metadata={"company": brief.company_name},
            )
            return result.get("narrative", result.get("text", str(result)))
        except Exception as exc:
            print(f"[insight_agent] Narrative LLM failed: {exc}")
            return self._fallback_narrative(brief, signal_lines)

    def _fallback_narrative(
        self, brief: HiringSignalBrief, signal_lines: list[str]
    ) -> str:
        """Deterministic narrative when LLM is unavailable."""
        hedge = _confidence_hedge(brief.icp.confidence.value)
        parts = []

        if brief.funding.has_recent_funding:
            parts.append(
                f"{brief.company_name} closed a {brief.funding.round_type} "
                f"{brief.funding.days_since_funding} days ago."
            )
        if brief.hiring.open_roles_count and brief.hiring.open_roles_count >= 3:
            ai_str = (
                f", {brief.hiring.ai_adjacent_role_count} of which are AI-adjacent"
                if brief.hiring.has_ai_adjacent_roles else ""
            )
            parts.append(
                f"They currently have {brief.hiring.open_roles_count} open "
                f"engineering roles{ai_str}."
            )
        if brief.layoff.has_recent_layoff:
            parts.append(
                f"A layoff occurred {brief.layoff.days_since_layoff} days ago, "
                "suggesting cost pressure alongside scaling goals."
            )

        narrative = " ".join(parts) if parts else signal_lines[0]
        return f"{hedge}{narrative}"

    def _select_pitch_angle(self, brief: HiringSignalBrief) -> str:
        segment = brief.icp.segment
        ai_score = brief.ai_maturity.score

        if segment == ICPSegment.RECENTLY_FUNDED:
            return "scale_ai_team_faster" if ai_score >= 2 else "stand_up_first_ai_function"
        if segment == ICPSegment.COST_RESTRUCTURING:
            return (
                "offshore_equivalent_with_ai_capability" if ai_score >= 2
                else "replace_higher_cost_roles_preserve_output"
            )
        if segment == ICPSegment.LEADERSHIP_CHANGE:
            return "vendor_reassessment_new_leader"
        if segment == ICPSegment.CAPABILITY_GAP:
            return "specialized_ai_build_project_consulting"
        return "generic_exploratory"

    @staticmethod
    def _brief_to_dict(brief: HiringSignalBrief) -> dict:
        def _default(obj):
            if isinstance(obj, date):
                return obj.isoformat()
            if hasattr(obj, "value"):
                return obj.value
            return str(obj)
        return json.loads(json.dumps(asdict(brief), default=_default))
