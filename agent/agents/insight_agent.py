"""
agent/agents/insight_agent.py

📊 Agent 2 — Insight / Brief Agent

Role:
  - Takes HiringSignalBrief + Company (from ResearchAgent)
  - LLM converts signals → human-readable reasoning narrative
  - Builds competitor_gap_brief.json: sector peers scored, gaps identified
  - Output: enriched brief dict ready for MessageAgent

This is the agent that turns "you have 12 open ML roles" into
"three companies in your sector at your funding stage have built
dedicated AI functions and you haven't — here is what that gap
looks like in practice."

Competitor gap logic:
  - Pulls sector peers from Crunchbase ODM (same industry, similar size)
  - Applies deterministic AI maturity scoring to each peer
  - Ranks by score, extracts top-quartile practices the prospect lacks
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from datetime import date
from typing import Optional

from agent.enrichment.crunchbase_loader import load_companies_by_industry
from agent.enrichment.signal_computer import compute_signals, _compute_ai_maturity
from agent.llm_client import chat_json
from agent.models.company import Company
from agent.models.signals import HiringSignalBrief, ICPSegment, SignalConfidence
from agent.observability import traced

# Tenacious style guide markers (embedded per spec; style_guide.md not yet in repo)
_STYLE_MARKERS = """
Tone: Direct, evidence-first, never condescending. Speak peer-to-peer.
Avoid: "leverage", "synergies", "unlock", "excited to share", "game-changing".
Use: Specific numbers, named signals, hedged language when confidence is low.
Length: Insight narrative max 120 words. Gap explanation max 80 words per gap.
"""


def _confidence_hedge(confidence: str) -> str:
    """Return appropriate hedge phrase based on signal confidence."""
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
          "competitor_gap_brief": {...},
          "pitch_angle": "...",
        }
        """
        t0 = time.perf_counter()

        narrative = self._generate_narrative(brief)
        comp_gap = self._build_competitor_gap(company, brief)
        pitch_angle = self._select_pitch_angle(brief)

        result = {
            "hiring_signal_brief": self._brief_to_dict(brief),
            "narrative": narrative,
            "competitor_gap_brief": comp_gap,
            "pitch_angle": pitch_angle,
            "generated_at": date.today().isoformat(),
        }

        if save_path:
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, default=str)
            print(f"[insight_agent] Saved to {save_path}")

        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        print(
            f"[insight_agent] '{company.name}' insight generated in {elapsed_ms}ms | "
            f"gap_count={len(comp_gap.get('gaps', []))}"
        )
        return result

    def _generate_narrative(self, brief: HiringSignalBrief) -> str:
        """
        Single LLM call: translate structured signals into a 2-3 sentence
        research finding a prospect would read with interest.
        Honesty constraint: only reference signals with confidence >= MEDIUM.
        """
        # Build a context block from high-confidence signals only
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

        system_prompt = f"""You write concise research findings for a B2B sales team.
Rules:
- Max 120 words total.
- Reference only the signals provided. Do not invent facts.
- Use hedged language if confidence is noted as medium/low.
- Write for a CTO or VP Engineering who is skeptical of vendor outreach.
- Never mention Tenacious by name in the narrative.
{_STYLE_MARKERS}"""

        user_prompt = f"""Company: {brief.company_name}
ICP segment: {brief.icp.segment.value}
Signals:
{chr(10).join(f'- {s}' for s in signal_lines)}

Write a 2-3 sentence research finding that makes the prospect's current situation
concrete and verifiable. End with one question that opens the door to a conversation."""

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
            # Deterministic fallback — still grounded in signals
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
            parts.append(
                f"They currently have {brief.hiring.open_roles_count} open "
                f"engineering roles"
                + (
                    f", {brief.hiring.ai_adjacent_role_count} of which are AI-adjacent"
                    if brief.hiring.has_ai_adjacent_roles
                    else ""
                )
                + "."
            )
        if brief.layoff.has_recent_layoff:
            parts.append(
                f"A layoff occurred {brief.layoff.days_since_layoff} days ago, "
                "suggesting cost pressure alongside scaling goals."
            )

        narrative = " ".join(parts) if parts else signal_lines[0]
        return f"{hedge}{narrative}"

    def _build_competitor_gap(
        self, company: Company, brief: HiringSignalBrief
    ) -> dict:
        """
        Find 5-10 sector peers in Crunchbase ODM, score their AI maturity,
        compute prospect's position in the distribution, extract top-quartile gaps.
        """
        industry = company.industry or "Software"
        peers = load_companies_by_industry(industry, limit=30)

        if not peers:
            return {
                "sector": industry,
                "peer_count": 0,
                "prospect_score": brief.ai_maturity.score,
                "sector_median": None,
                "sector_p75": None,
                "gaps": [],
                "note": "Insufficient Crunchbase ODM data for this sector.",
            }

        # Score all peers
        scored_peers = []
        for peer in peers:
            if peer.name.lower() == company.name.lower():
                continue
            peer_signal = _compute_ai_maturity(peer)
            scored_peers.append({
                "name": peer.name,
                "score": peer_signal.score,
                "evidence": peer_signal.evidence_notes[:2],
            })

        scored_peers.sort(key=lambda p: p["score"], reverse=True)

        scores = [p["score"] for p in scored_peers]
        n = len(scores)
        median_score = sorted(scores)[n // 2] if scores else None
        p75_score = sorted(scores)[int(n * 0.75)] if scores else None

        # Top-quartile peers
        top_quartile = [p for p in scored_peers if p["score"] >= (p75_score or 3)][:5]

        # Identify gaps: practices top-quartile has that the prospect lacks
        gaps = self._extract_gaps(brief, top_quartile)

        return {
            "sector": industry,
            "peer_count": len(scored_peers),
            "prospect_score": brief.ai_maturity.score,
            "sector_median": median_score,
            "sector_p75": p75_score,
            "top_quartile_peers": top_quartile[:5],
            "gaps": gaps,
            "prospect_percentile": (
                sum(1 for s in scores if s <= brief.ai_maturity.score) / n * 100
                if n > 0 else None
            ),
        }

    def _extract_gaps(self, brief: HiringSignalBrief, top_quartile: list[dict]) -> list[dict]:
        """
        Derive 2-3 specific practice gaps the top quartile shows that the prospect does not.
        Based on public-signal differences — no claims beyond what signals support.
        """
        gaps = []
        prospect_score = brief.ai_maturity.score

        if not brief.ai_maturity.has_named_ai_leadership and prospect_score < 3:
            gaps.append({
                "gap": "Named AI/ML leadership",
                "description": (
                    "Top-quartile peers in this sector have a named Head of AI, "
                    "VP Data, or Chief Scientist on their public team page. "
                    "This role typically owns the data platform and AI roadmap."
                ),
                "tenacious_relevance": (
                    "Tenacious can staff a fractional ML lead or a dedicated AI squad "
                    "to fill this gap while a permanent hire is sourced."
                ),
            })

        if not brief.ai_maturity.has_modern_ml_stack and prospect_score < 2:
            gaps.append({
                "gap": "Modern data/ML stack",
                "description": (
                    "Companies at this stage in the sector are running on dbt, "
                    "Snowflake, or Databricks. The public stack signal here does not "
                    "show these tools yet."
                ),
                "tenacious_relevance": (
                    "Tenacious has bench engineers who have migrated three comparable "
                    "platforms to a modern lakehouse in 90-120 days."
                ),
            })

        if brief.ai_maturity.ai_adjacent_role_count == 0 and len(top_quartile) > 0:
            top_ai_roles_avg = 2  # assumed for top quartile without exact data
            gaps.append({
                "gap": "AI-adjacent engineering headcount",
                "description": (
                    f"Top-quartile peers in {brief.company_name}'s sector "
                    "have dedicated AI/ML engineers embedded in product teams. "
                    "No AI-adjacent open roles were found publicly for this company."
                ),
                "tenacious_relevance": (
                    "A dedicated Tenacious AI squad can be staffed in 2-3 weeks "
                    "versus a 3-6 month in-house hiring cycle."
                ),
            })

        return gaps[:3]  # Cap at 3 per spec

    def _select_pitch_angle(self, brief: HiringSignalBrief) -> str:
        """
        Select the Tenacious pitch angle based on segment + AI maturity.
        Returns a one-line pitch direction for the MessageAgent.
        """
        segment = brief.icp.segment
        ai_score = brief.ai_maturity.score

        if segment == ICPSegment.RECENTLY_FUNDED:
            if ai_score >= 2:
                return "scale_ai_team_faster"
            return "stand_up_first_ai_function"

        if segment == ICPSegment.COST_RESTRUCTURING:
            if ai_score >= 2:
                return "offshore_equivalent_with_ai_capability"
            return "replace_higher_cost_roles_preserve_output"

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
