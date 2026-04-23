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

from agent.enrichment.crunchbase_loader import load_companies_by_industry
from agent.enrichment.signal_computer import _compute_ai_maturity
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


def _headcount_band_for(company: Company) -> str:
    """Map Company headcount data to competitor_gap_brief.schema.json band enum."""
    exact = company.employee_count_exact
    band = company.employee_count_band or ""

    def _resolve(n: int) -> str:
        if n < 80:
            return "15_to_80"
        if n < 200:
            return "80_to_200"
        if n < 500:
            return "200_to_500"
        if n < 2000:
            return "500_to_2000"
        return "2000_plus"

    if exact:
        return _resolve(exact)

    # Parse band string midpoint
    parts = band.replace("–", "-").split("-")
    if len(parts) == 2:
        try:
            mid = (int(parts[0]) + int(parts[1])) // 2
            return _resolve(mid)
        except ValueError:
            pass
    return "80_to_200"  # safe default


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
        comp_gap = self._build_competitor_gap(company, brief)
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

    def _build_competitor_gap(
        self, company: Company, brief: HiringSignalBrief
    ) -> dict:
        """
        Build a competitor_gap_brief conforming to competitor_gap_brief.schema.json.
        Requires 5–10 peers, each with ai_maturity_score, justification, headcount_band.
        Gap findings (1–3) each require ≥2 peer evidence items.
        """
        industry = company.industry or "Software"
        raw_peers = load_companies_by_industry(industry, limit=40)

        # Remove the prospect itself
        peers = [p for p in raw_peers if p.name.lower() != company.name.lower()]

        if len(peers) < 5:
            return self._empty_gap_brief(company, brief, industry)

        # Score each peer
        scored: list[dict] = []
        for peer in peers[:15]:  # analyse up to 15, keep best 5–10
            peer_signal = _compute_ai_maturity(peer)
            justifications = list(peer_signal.evidence_notes[:3]) or [
                "No public AI signal detected."
            ]
            band = _headcount_band_for(peer)
            domain = peer.website or f"{peer.name.lower().replace(' ', '-')}.com"
            if domain.startswith("http"):
                from urllib.parse import urlparse
                domain = urlparse(domain).netloc or domain

            scored.append({
                "name": peer.name,
                "domain": domain,
                "ai_maturity_score": peer_signal.score,
                "ai_maturity_justification": justifications,
                "headcount_band": band,
                "top_quartile": False,  # set below after ranking
                "sources_checked": (
                    [peer.website] if peer.website else []
                ),
            })

        # Sort and mark top quartile
        scored.sort(key=lambda p: p["ai_maturity_score"], reverse=True)
        scores = [p["ai_maturity_score"] for p in scored]
        n = len(scores)
        p75_score = sorted(scores)[int(n * 0.75)] if n >= 4 else (max(scores) if scores else 3)
        top_quartile_threshold = max(p75_score, 2)  # at least score 2 to be "top quartile"

        for p in scored:
            p["top_quartile"] = p["ai_maturity_score"] >= top_quartile_threshold

        # Keep 5–10 peers for the schema
        peers_for_schema = scored[:10]
        top_quartile_peers = [p for p in peers_for_schema if p["top_quartile"]]

        sector_median = sorted(scores)[n // 2] if scores else 0
        sector_top_quartile_avg = (
            sum(p["ai_maturity_score"] for p in top_quartile_peers) / len(top_quartile_peers)
            if top_quartile_peers else float(top_quartile_threshold)
        )

        # Build gap findings
        gap_findings = self._build_gap_findings(brief, top_quartile_peers)

        # Gap quality self-check
        all_have_urls = all(
            any(e.get("source_url") for e in gf.get("peer_evidence", []))
            for gf in gap_findings
        )
        at_least_one_high = any(gf.get("confidence") == "high" for gf in gap_findings)

        # Prospect-silent-but-sophisticated risk: has technical blog/stack but low AI public signal
        silent_sophisticated = (
            brief.ai_maturity.score <= 1
            and bool(company.detected_technologies)
            and len(company.public_ai_mentions) > 0
        )

        # Pitch shift suggestion
        pitch_shift = self._suggest_pitch_shift(brief, gap_findings)

        return {
            "prospect_domain": brief.prospect_domain or f"{company.name.lower().replace(' ', '-')}.com",
            "prospect_sector": industry,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "prospect_ai_maturity_score": brief.ai_maturity.score,
            "sector_top_quartile_benchmark": round(sector_top_quartile_avg, 2),
            "competitors_analyzed": peers_for_schema,
            "gap_findings": gap_findings,
            "suggested_pitch_shift": pitch_shift,
            "gap_quality_self_check": {
                "all_peer_evidence_has_source_url": all_have_urls,
                "at_least_one_gap_high_confidence": at_least_one_high,
                "prospect_silent_but_sophisticated_risk": silent_sophisticated,
            },
            # Additional metadata (not in schema but useful for debugging)
            "_meta": {
                "peer_count_analysed": len(scored),
                "sector_median_score": sector_median,
                "sector_p75_score": p75_score,
                "prospect_percentile": (
                    round(sum(1 for s in scores if s <= brief.ai_maturity.score) / n * 100, 1)
                    if n > 0 else None
                ),
            },
        }

    def _build_gap_findings(
        self, brief: HiringSignalBrief, top_quartile: list[dict]
    ) -> list[dict]:
        """
        Build 1–3 gap findings conforming to competitor_gap_brief.schema.json.
        Each finding needs ≥2 peer_evidence items with source_url.
        Confidence must be high/medium/low (string, not enum).
        """
        findings: list[dict] = []
        prospect_score = brief.ai_maturity.score
        segment = brief.icp.segment

        # ── Gap 1: Named AI/ML leadership ─────────────────────────────────────
        if not brief.ai_maturity.has_named_ai_leadership and len(top_quartile) >= 2:
            peer_ev = []
            for peer in top_quartile[:3]:
                if peer["ai_maturity_score"] >= 2:
                    source = peer["sources_checked"][0] if peer["sources_checked"] else None
                    peer_ev.append({
                        "competitor_name": peer["name"],
                        "evidence": (
                            f"AI maturity score {peer['ai_maturity_score']}/3 based on: "
                            + "; ".join(peer["ai_maturity_justification"][:2])
                        ),
                        "source_url": source or f"https://{peer['domain']}/team",
                    })

            if len(peer_ev) >= 2:
                findings.append({
                    "practice": (
                        "Named AI/ML leadership role (Head of AI, VP Data, or Chief Scientist) "
                        "present on public team page"
                    ),
                    "peer_evidence": peer_ev[:3],
                    "prospect_state": (
                        f"{brief.company_name} has no named AI/ML leadership role on the public "
                        "team page. CTO or VP Engineering holds the AI remit."
                        if not brief.ai_maturity.has_named_ai_leadership
                        else "No public AI leadership signal found."
                    ),
                    "confidence": "high" if len(top_quartile) >= 3 else "medium",
                    "segment_relevance": [
                        "segment_1_series_a_b",
                        "segment_4_specialized_capability",
                    ],
                })

        # ── Gap 2: Modern ML / MLOps stack ────────────────────────────────────
        if not brief.ai_maturity.has_modern_ml_stack and prospect_score < 2 and len(top_quartile) >= 2:
            peer_ev = []
            for peer in top_quartile[:3]:
                justifications = peer.get("ai_maturity_justification", [])
                stack_evidence = [j for j in justifications if any(
                    kw in j.lower() for kw in ["stack", "dbt", "databricks", "snowflake", "mlops", "platform"]
                )]
                if stack_evidence or peer["ai_maturity_score"] >= 2:
                    source = peer["sources_checked"][0] if peer["sources_checked"] else None
                    peer_ev.append({
                        "competitor_name": peer["name"],
                        "evidence": (
                            stack_evidence[0] if stack_evidence
                            else f"AI maturity score {peer['ai_maturity_score']}/3; modern stack inferred from role signals."
                        ),
                        "source_url": source or f"https://{peer['domain']}",
                    })

            if len(peer_ev) >= 2:
                findings.append({
                    "practice": (
                        "Modern data/ML stack in production (dbt, Snowflake, Databricks, "
                        "or MLOps tooling visible in public job descriptions or BuiltWith data)"
                    ),
                    "peer_evidence": peer_ev[:3],
                    "prospect_state": (
                        f"No modern ML stack signal detected publicly for {brief.company_name}. "
                        "This may reflect a quieter public presence rather than absence."
                    ),
                    "confidence": "medium",
                    "segment_relevance": [
                        "segment_1_series_a_b",
                        "segment_2_mid_market_restructure",
                    ],
                })

        # ── Gap 3: AI-adjacent engineering headcount ───────────────────────────
        if brief.ai_maturity.ai_adjacent_role_count == 0 and len(top_quartile) >= 2:
            peer_ev = []
            for peer in top_quartile[:3]:
                justifications = peer.get("ai_maturity_justification", [])
                role_evidence = [j for j in justifications if any(
                    kw in j.lower() for kw in ["role", "engineer", "open", "hire", "ml", "ai"]
                )]
                source = peer["sources_checked"][0] if peer["sources_checked"] else None
                peer_ev.append({
                    "competitor_name": peer["name"],
                    "evidence": (
                        role_evidence[0] if role_evidence
                        else f"AI maturity {peer['ai_maturity_score']}/3; AI-adjacent roles inferred."
                    ),
                    "source_url": source or f"https://{peer['domain']}/careers",
                })

            if len(peer_ev) >= 2:
                findings.append({
                    "practice": (
                        "Dedicated AI/ML engineers embedded in product teams "
                        "(visible as AI-adjacent open roles on public job boards)"
                    ),
                    "peer_evidence": peer_ev[:3],
                    "prospect_state": (
                        f"No AI-adjacent open roles found publicly for {brief.company_name}. "
                        "This could reflect private job listings, a hiring freeze on AI roles, "
                        "or a deliberate choice to grow AI capability through existing engineers."
                    ),
                    "confidence": "medium" if len(top_quartile) >= 3 else "low",
                    "segment_relevance": [
                        "segment_1_series_a_b",
                        "segment_4_specialized_capability",
                    ],
                })

        return findings[:3]  # schema allows max 3

    def _suggest_pitch_shift(self, brief: HiringSignalBrief, gap_findings: list[dict]) -> str:
        """Generate a pitch-shift suggestion for the MessageAgent."""
        segment = brief.icp.segment
        high_conf_gap = next(
            (g for g in gap_findings if g.get("confidence") == "high"), None
        )

        if segment == ICPSegment.CAPABILITY_GAP and high_conf_gap:
            practice = high_conf_gap["practice"].split("(")[0].strip()
            return (
                f"Lead with the '{practice}' gap (high confidence). "
                "Frame as a question — is this a deliberate choice or still being scoped? "
                "Segment 4 pitch: 'three companies in your sector at your stage are doing X.'"
            )
        if segment == ICPSegment.RECENTLY_FUNDED:
            ai_score = brief.ai_maturity.score
            if ai_score >= 2:
                return (
                    "High AI readiness. Lead with: 'scale your AI team faster than "
                    "in-house hiring can support.' Reference any high-confidence gap if present."
                )
            return (
                "Low AI readiness. Lead with: 'stand up your first AI function with "
                "a dedicated squad.' Avoid Segment 4 language entirely."
            )
        if segment == ICPSegment.COST_RESTRUCTURING:
            return (
                "Post-restructure framing. Lead with the restructure date as a neutral fact. "
                "Avoid high-energy urgency language — post-restructure CFOs are skeptical."
            )
        if segment == ICPSegment.LEADERSHIP_CHANGE:
            return (
                "New leader context. Open with a grounded reference to the transition. "
                "Let the leader's reply direct the technical language — do not pre-assume AI stance."
            )
        return (
            "Insufficient signal for segment-specific pitch. Send generic exploratory. "
            "Ask about engineering roadmap — do not make specific claims about their company."
        )

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

    def _empty_gap_brief(self, company: Company, brief: HiringSignalBrief, industry: str) -> dict:
        return {
            "prospect_domain": brief.prospect_domain or "",
            "prospect_sector": industry,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "prospect_ai_maturity_score": brief.ai_maturity.score,
            "sector_top_quartile_benchmark": 0.0,
            "competitors_analyzed": [],
            "gap_findings": [],
            "suggested_pitch_shift": "Insufficient Crunchbase ODM data for sector comparison.",
            "gap_quality_self_check": {
                "all_peer_evidence_has_source_url": False,
                "at_least_one_gap_high_confidence": False,
                "prospect_silent_but_sophisticated_risk": False,
            },
        }

    @staticmethod
    def _brief_to_dict(brief: HiringSignalBrief) -> dict:
        def _default(obj):
            if isinstance(obj, date):
                return obj.isoformat()
            if hasattr(obj, "value"):
                return obj.value
            return str(obj)
        return json.loads(json.dumps(asdict(brief), default=_default))
