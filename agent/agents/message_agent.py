"""
agent/agents/message_agent.py

✉️ Agent 3 — Message Generation Agent

Role:
  - Takes insight dict (from InsightAgent)
  - Generates a personalized, grounded outbound email
  - No hallucinations: every claim in the email is pulled from the brief
  - Respects the Tenacious style guide tone markers
  - Tags output as 'signal_grounded' or 'generic' for A/B tracking

Output:
  {
    "subject": str,
    "html_body": str,
    "text_body": str,
    "variant": "signal_grounded" | "generic",
    "pitch_angle": str,
    "draft": True,   ← always True per data-handling policy
  }
"""

from __future__ import annotations

import time
from typing import Optional

from agent.llm_client import chat_json
from agent.models.signals import ICPSegment, SignalConfidence
from agent.observability import traced

# Tenacious tone constants
_TENACIOUS_CONTEXT = """
Tenacious Consulting and Outsourcing provides two services:
1. Managed talent outsourcing: dedicated engineering/data teams (3-12 engineers, 6-24 months).
2. Project-based consulting: time-boxed AI/data platform deliveries.

Pricing bands (public tier):
- Talent outsourcing: $240–$720K ACV
- Project consulting: $80–$300K per engagement

Bench capacity: Python, Go, data engineering, ML/AI, infrastructure.

Style guide:
- Voice: Direct, peer-to-peer. Never vendor-pitch tone.
- Avoid: "leverage", "synergies", "excited to share", "game-changing", "unlock potential"
- Use: Specific numbers, named signals, one concrete ask.
- Length: Subject ≤ 8 words. Body ≤ 150 words. CTA = one question or one link.
- Honesty: Only reference signals marked as present in the brief.
  If confidence is LOW, ask rather than assert.
- Mark every draft email with a footer note: [DRAFT — not for deployment without review]
"""

# Segment-specific pitch templates (filled by LLM, not hardcoded)
_PITCH_ANGLE_GUIDES = {
    "scale_ai_team_faster": (
        "They recently funded and have high AI maturity. Lead with: "
        "'Scale your AI team faster than in-house hiring can support.'"
    ),
    "stand_up_first_ai_function": (
        "They recently funded but AI maturity is low. Lead with: "
        "'Stand up your first AI function with a dedicated squad.'"
    ),
    "offshore_equivalent_with_ai_capability": (
        "Post-layoff, high AI maturity. Lead with: "
        "'Replace cost without replacing capability — offshore equivalent "
        "with AI-ready engineers.'"
    ),
    "replace_higher_cost_roles_preserve_output": (
        "Post-layoff, low AI maturity. Lead with: "
        "'Keep delivery capacity, cut burn — offshore equivalent for core engineering.'"
    ),
    "vendor_reassessment_new_leader": (
        "New CTO/VP Eng. Lead with: "
        "'New engineering leadership often reassesses the offshore mix in the first 90 days. "
        "Worth 30 minutes to see if Tenacious fits the picture you are building?'"
    ),
    "specialized_ai_build_project_consulting": (
        "High AI maturity, capability gap. Lead with: "
        "'You are building toward X — Tenacious has done this three times in the last 18 months.'"
    ),
    "generic_exploratory": (
        "Insufficient signal. Lead with an open question about their engineering roadmap. "
        "Do NOT make specific claims about their company."
    ),
}


class MessageAgent:
    """
    Generates a grounded outbound email from an InsightAgent output dict.
    Passes output to GuardrailAgent before returning.
    """

    def __init__(self, llm_tier: str = "dev"):
        self.llm_tier = llm_tier

    @traced("message_agent.run")
    def run(
        self,
        insight: dict,
        prospect_name: Optional[str] = None,
        prospect_role: Optional[str] = None,
    ) -> dict:
        """
        Generate a cold outreach email.

        Args:
            insight:        Output dict from InsightAgent.run()
            prospect_name:  Contact first name for personalisation
            prospect_role:  Contact title (e.g. "CTO", "VP Engineering")

        Returns:
            Email dict with subject, html_body, text_body, variant, draft=True
        """
        t0 = time.perf_counter()

        brief_dict = insight.get("hiring_signal_brief", {})
        narrative = insight.get("narrative", "")
        competitor_gap = insight.get("competitor_gap_brief", {})
        pitch_angle = insight.get("pitch_angle", "generic_exploratory")
        company_name = brief_dict.get("company_name", "your company")

        # Determine variant
        has_strong_signal = (
            pitch_angle != "generic_exploratory"
            and bool(narrative)
        )
        variant = "signal_grounded" if has_strong_signal else "generic"

        # Build the prompt context
        gaps = competitor_gap.get("gaps", [])
        gap_text = ""
        if gaps:
            gap_text = "\n".join(
                f"- Gap: {g['gap']}: {g['description']}"
                for g in gaps[:2]
            )

        system_prompt = f"""You write cold outbound emails for Tenacious Consulting and Outsourcing.
{_TENACIOUS_CONTEXT}
Pitch direction: {_PITCH_ANGLE_GUIDES.get(pitch_angle, _PITCH_ANGLE_GUIDES['generic_exploratory'])}"""

        user_prompt = f"""Company: {company_name}
Prospect: {prospect_name or 'the engineering leader'} ({prospect_role or 'CTO/VP Engineering'})
Research narrative: {narrative}
Competitor gaps identified:
{gap_text or '(no gaps extracted)'}
AI maturity score: {brief_dict.get('ai_maturity', {}).get('score', 0)}/3

Write a cold outreach email:
- Subject line (≤8 words, no clickbait)
- Body (≤150 words, one concrete ask at the end)
- Every claim must come from the research narrative or competitor gaps above.
- If narrative is weak, ask rather than assert.
- End with: [DRAFT — not for deployment without review]

Return JSON: {{"subject": "...", "text_body": "...", "html_body": "<p>...</p>"}}"""

        try:
            result = chat_json(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tier=self.llm_tier,
                max_tokens=600,
                trace_name="message_agent.generate_email",
                trace_metadata={
                    "company": company_name,
                    "variant": variant,
                    "pitch_angle": pitch_angle,
                },
            )
        except Exception as exc:
            print(f"[message_agent] LLM failed, using fallback: {exc}")
            result = self._fallback_email(
                company_name, prospect_name, prospect_role, narrative, pitch_angle
            )

        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        print(
            f"[message_agent] Email generated in {elapsed_ms}ms | "
            f"variant={variant} | pitch={pitch_angle}"
        )

        return {
            "subject": result.get("subject", f"Engineering capacity — {company_name}"),
            "html_body": result.get(
                "html_body", f"<p>{result.get('text_body', '')}</p>"
            ),
            "text_body": result.get("text_body", ""),
            "variant": variant,
            "pitch_angle": pitch_angle,
            "company_name": company_name,
            "draft": True,
        }

    def _fallback_email(
        self,
        company_name: str,
        prospect_name: Optional[str],
        prospect_role: Optional[str],
        narrative: str,
        pitch_angle: str,
    ) -> dict:
        """Deterministic fallback if LLM is unavailable."""
        greeting = f"Hi {prospect_name}," if prospect_name else "Hi,"
        role_str = prospect_role or "your engineering team"

        subject = f"{company_name} — engineering capacity question"

        if narrative and pitch_angle != "generic_exploratory":
            body = (
                f"{greeting}\n\n"
                f"{narrative}\n\n"
                "Tenacious works with teams at this stage to extend engineering "
                "output without a long hiring cycle. Worth a 30-minute call to "
                "see if there's a fit?\n\n"
                "Best,\nTenacious Delivery Team\n\n"
                "[DRAFT — not for deployment without review]"
            )
        else:
            body = (
                f"{greeting}\n\n"
                f"I came across {company_name} and wanted to reach out directly.\n\n"
                "Tenacious provides dedicated engineering and data teams to B2B "
                "technology companies — typically 3-12 engineers, 6-24 months, "
                "managed under Tenacious but delivering to your product.\n\n"
                "Is engineering capacity on your roadmap this quarter?\n\n"
                "Best,\nTenacious Delivery Team\n\n"
                "[DRAFT — not for deployment without review]"
            )

        return {
            "subject": subject,
            "text_body": body,
            "html_body": body.replace("\n\n", "<br><br>").replace("\n", "<br>"),
        }
