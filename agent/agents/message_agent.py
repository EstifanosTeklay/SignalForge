"""
agent/agents/message_agent.py

✉️ Agent 3 — Message Generation Agent

Role:
  - Takes insight dict (from InsightAgent)
  - Generates personalized, grounded outbound emails for a 3-email cold sequence
  - No hallucinations: every claim in the email is pulled from the brief
  - Respects the Tenacious style guide (5 tone markers from style_guide.md)
  - Tags output as 'signal_grounded' or 'generic' for A/B tracking

Email sequence (per cold.md):
  Email 1 (Day 0)  — Signal-grounded opener, max 120 words
  Email 2 (Day 5)  — Research-finding follow-up, max 100 words, new competitor-gap data point
  Email 3 (Day 12) — Gracious close, max 70 words

Output per email:
  {
    "subject": str,          # ≤ 60 characters (Gmail mobile truncates above this)
    "html_body": str,
    "text_body": str,
    "variant": "signal_grounded" | "generic",
    "pitch_angle": str,
    "sequence_position": 1 | 2 | 3,
    "draft": True,           # always True per data-handling policy
  }
"""

from __future__ import annotations

import time
from typing import Optional

from agent.enrichment.bench_loader import bench_description, get_as_of
from agent.llm_client import chat_json
from agent.models.signals import ICPSegment
from agent.observability import traced

# ── Tenacious context (grounded in seed files) ─────────────────────────────────
# ACV values reference baseline_numbers.md — agent must NOT invent specific totals.
# Bench capacity loaded from bench_loader (seed/bench_summary.json, as_of date).
def _build_tenacious_context() -> str:
    bench = bench_description()
    bench_date = get_as_of()
    return f"""Tenacious Consulting and Outsourcing — context for outreach composition:

Services:
1. Managed talent outsourcing: dedicated engineering/data teams, 3–12 engineers, 6–24 months,
   managed under Tenacious, delivering to the client's product. Addis Ababa HQ; 3–5hr daily
   overlap with client time zone.
2. Project-based consulting: fixed-scope AI/data platform deliveries, defined deliverables,
   milestone payments, Phase 1 termination clause.

Pricing (public tier — quote these, do NOT invent totals or discounts):
  - Monthly rates: from junior to senior/team-lead tiers (see pricing_sheet.md)
  - Project consulting: starter analytics/dashboard from the published floor price
  - Engagement minimum: 1 month; extensions in 2-week blocks
  - Route any ask for specific total-contract value to a discovery call

Delivery bench as of {bench_date}:
{bench}

Five tone markers (Tenacious style guide — ALL must be preserved):
1. DIRECT: Clear, brief, actionable. Subject starts with "Context:", "Note:", "Question:", "Request:".
   Never "Hey", "Quick", "Just", "Hope this finds you well".
2. GROUNDED: Every claim traces to the hiring signal brief or competitor gap brief.
   Fewer than 5 open roles → ASK, do not assert "aggressive hiring".
3. HONEST: No over-claims. No bench capacity the summary does not show.
   "We don't see public signal of X" beats a confident wrong claim.
4. PROFESSIONAL: Write for CTOs and VPs Engineering. Avoid: "bench", "top talent",
   "world-class", "A-players", "rockstar", "ninja", "offshore clichés".
   Use "engineering team" instead of "bench"; "available engineers" instead of "capacity".
5. NON-CONDESCENDING: Frame competitor gaps as research findings or questions.
   Never "you're missing a critical AI capability." Always "curious whether this is deliberate."

Word limits (strictly enforced):
  - Email 1 body: MAX 120 words
  - Email 2 body: MAX 100 words
  - Email 3 body: MAX 70 words
  - Subject line: MAX 60 characters (Gmail truncates on mobile above this)
  - One clear ask per message — never stack multiple requests.
  - No emojis in cold outreach.

Signature template:
[First name]
[Title, e.g., Research Partner]
Tenacious Intelligence Corporation
gettenacious.com

Footer required on every draft: [DRAFT — not for deployment without review]"""


# ── Subject line patterns per segment (from cold.md) ─────────────────────────
_SUBJECT_PATTERNS = {
    "recently_funded": "Context: {signal}",       # e.g., "Context: your $14M Series B"
    "cost_restructuring": "Note on {signal}",      # e.g., "Note on your March restructure"
    "leadership_change": "Congrats on the {signal}",  # e.g., "Congrats on the CTO appointment"
    "capability_gap": "Question on {signal}",      # e.g., "Question on your ML platform roadmap"
    "abstain": "Question: engineering roadmap",    # generic
}

# ── Pitch angle guides (per icp_definition.md) ───────────────────────────────
_PITCH_ANGLE_GUIDES = {
    "scale_ai_team_faster": (
        "Segment 1 (high AI readiness). Lead: 'scale your AI team faster than in-house "
        "hiring can support.' Reference the funding event and hiring velocity. "
        "One concrete offer: engineering squad available in 7–14 days."
    ),
    "stand_up_first_ai_function": (
        "Segment 1 (low AI readiness, score 0–1). Lead: 'stand up your first AI function "
        "with a dedicated squad.' Do NOT use Segment 4 language. "
        "Keep AI adjacent — don't over-claim their AI ambition."
    ),
    "offshore_equivalent_with_ai_capability": (
        "Segment 2 (high AI readiness). Lead: 'preserve your AI delivery capacity while "
        "reshaping cost structure.' SOFT urgency — post-restructure CFOs distrust high energy. "
        "State the restructure date as a neutral fact, not a window closing."
    ),
    "replace_higher_cost_roles_preserve_output": (
        "Segment 2 (low AI readiness). Lead: 'maintain platform delivery velocity through "
        "the restructure.' SOFT urgency. Offshore equivalent framing — cost lever, not speed lever."
    ),
    "vendor_reassessment_new_leader": (
        "Segment 3. Lead: reference the appointment concretely ('you started the CTO role "
        "at X in late January'). First 90 days are when vendor mix gets reassessed — say this "
        "without being pushy. Offer a conversation, not a pitch deck."
    ),
    "specialized_ai_build_project_consulting": (
        "Segment 4 (AI maturity ≥ 2). Lead with the competitor gap brief — 'three companies "
        "in your sector at your stage are doing X and you are not.' This segment pitches are "
        "ONLY valid at AI readiness 2+. Never use this pitch for a score-0 or score-1 prospect."
    ),
    "generic_exploratory": (
        "Insufficient signal — abstain path. Send a generic exploratory email. "
        "Ask about their engineering roadmap. Do NOT make specific claims about their company. "
        "Do NOT assert a gap, a signal, or a buying window that was not confirmed."
    ),
}


class MessageAgent:
    """
    Generates a grounded outbound email from an InsightAgent output dict.
    Supports 3-email cold sequence per cold.md.
    """

    def __init__(self, llm_tier: str = "dev"):
        self.llm_tier = llm_tier
        self._context = _build_tenacious_context()

    @traced("message_agent.run")
    def run(
        self,
        insight: dict,
        prospect_name: Optional[str] = None,
        prospect_role: Optional[str] = None,
        sequence_position: int = 1,
    ) -> dict:
        """
        Generate one email in the cold sequence.

        Args:
            insight:             Output dict from InsightAgent.run()
            prospect_name:       Contact first name for personalisation
            prospect_role:       Contact title (e.g. "CTO", "VP Engineering")
            sequence_position:   1 = opener, 2 = follow-up, 3 = close

        Returns:
            Email dict with subject, html_body, text_body, variant, draft=True
        """
        if sequence_position == 2:
            return self.generate_followup_email(insight, prospect_name, prospect_role)
        if sequence_position == 3:
            return self.generate_close_email(insight, prospect_name)
        return self._generate_opener(insight, prospect_name, prospect_role)

    def _generate_opener(
        self,
        insight: dict,
        prospect_name: Optional[str],
        prospect_role: Optional[str],
    ) -> dict:
        """Email 1 — Signal-grounded opener. Max 120 words."""
        t0 = time.perf_counter()

        brief_dict = insight.get("hiring_signal_brief", {})
        narrative = insight.get("narrative", "")
        competitor_gap = insight.get("competitor_gap_brief", {})
        pitch_angle = insight.get("pitch_angle", "generic_exploratory")
        company_name = brief_dict.get("company_name", "your company")
        honesty_flags = brief_dict.get("honesty_flags", [])
        segment = brief_dict.get("icp", {}).get("segment", "abstain")
        ai_score = brief_dict.get("ai_maturity", {}).get("score", 0)

        has_strong_signal = pitch_angle != "generic_exploratory" and bool(narrative)
        variant = "signal_grounded" if has_strong_signal else "generic"

        # Build subject hint from segment
        subject_pattern = _SUBJECT_PATTERNS.get(segment, _SUBJECT_PATTERNS["abstain"])
        signal_phrase = self._extract_signal_phrase(brief_dict, segment)
        subject_hint = subject_pattern.format(signal=signal_phrase)

        # Top 1–2 gap findings for the email
        gap_findings = competitor_gap.get("gap_findings", [])
        gap_text = ""
        if gap_findings:
            best_gap = gap_findings[0]
            gap_text = (
                f"Top competitor gap to reference (confidence={best_gap.get('confidence')}):\n"
                f"Practice: {best_gap.get('practice', '')}\n"
                f"Prospect state: {best_gap.get('prospect_state', '')}\n"
                + "Peer evidence: "
                + "; ".join(
                    f"{e['competitor_name']}: {e['evidence']}"
                    for e in best_gap.get("peer_evidence", [])[:2]
                )
            )

        # Honesty constraint reminders
        honesty_reminders = []
        if "weak_hiring_velocity_signal" in honesty_flags:
            honesty_reminders.append(
                "HARD CONSTRAINT: Fewer than 5 open roles. Do NOT say 'scaling aggressively' "
                "or 'aggressive hiring'. Ask: 'is hiring velocity matching the runway?'"
            )
        if "weak_ai_maturity_signal" in honesty_flags:
            honesty_reminders.append(
                "HARD CONSTRAINT: AI maturity signal is weak. Do NOT assert a mature AI function. "
                "Use hedged language."
            )
        if segment == "abstain":
            honesty_reminders.append(
                "HARD CONSTRAINT: Abstain path — no segment-specific claims. "
                "Generic exploratory email only. Ask, do not assert."
            )

        system_prompt = f"""You write cold outbound emails for Tenacious Consulting.
{self._context}
Pitch direction: {_PITCH_ANGLE_GUIDES.get(pitch_angle, _PITCH_ANGLE_GUIDES['generic_exploratory'])}
{chr(10).join(honesty_reminders)}"""

        user_prompt = f"""Company: {company_name}
Prospect: {prospect_name or 'the engineering leader'} ({prospect_role or 'CTO/VP Engineering'})
Research narrative: {narrative or '(no strong signal — use generic exploratory)'}
{gap_text or '(no competitor gap data)'}
AI maturity score: {ai_score}/3

Write Email 1 (cold opener):
Structure (from cold.md):
  Sentence 1: ONE concrete verifiable fact from the signal brief.
  Sentence 2: The typical bottleneck or opportunity at this stage — observation, not assertion.
  Sentence 3: ONE specific thing Tenacious does that matches. No service menu.
  Sentence 4: The ask — 15 minutes, a specific day or two, Cal.com link placeholder.
  Signature: [first name], Research Partner, Tenacious Intelligence Corporation, gettenacious.com

Rules:
- Subject line ≤ 60 characters. Use pattern: "{subject_hint}" (adapt as needed)
- Body ≤ 120 words. No emojis. No filler. One clear ask.
- End body with: [DRAFT — not for deployment without review]

Return JSON: {{"subject": "...", "text_body": "...", "html_body": "<p>...</p>"}}"""

        try:
            result = chat_json(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tier=self.llm_tier,
                max_tokens=600,
                trace_name="message_agent.opener",
                trace_metadata={
                    "company": company_name,
                    "variant": variant,
                    "pitch_angle": pitch_angle,
                    "sequence_position": 1,
                },
            )
        except Exception as exc:
            print(f"[message_agent] LLM failed for opener: {exc}")
            result = self._fallback_opener(
                company_name, prospect_name, prospect_role, narrative, pitch_angle
            )

        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        print(
            f"[message_agent] Email 1 generated in {elapsed_ms}ms | "
            f"variant={variant} | pitch={pitch_angle}"
        )

        return {
            "subject": result.get("subject", f"Context: {company_name} engineering")[:60],
            "html_body": result.get("html_body", f"<p>{result.get('text_body', '')}</p>"),
            "text_body": result.get("text_body", ""),
            "variant": variant,
            "pitch_angle": pitch_angle,
            "company_name": company_name,
            "sequence_position": 1,
            "draft": True,
        }

    @traced("message_agent.followup")
    def generate_followup_email(
        self,
        insight: dict,
        prospect_name: Optional[str] = None,
        prospect_role: Optional[str] = None,
    ) -> dict:
        """
        Email 2 (Day 5) — Research-finding follow-up. Max 100 words.
        Introduces a NEW competitor-gap data point; no 'just following up'.
        Per cold.md: not a nag — the new data justifies the touch.
        """
        t0 = time.perf_counter()

        brief_dict = insight.get("hiring_signal_brief", {})
        competitor_gap = insight.get("competitor_gap_brief", {})
        pitch_angle = insight.get("pitch_angle", "generic_exploratory")
        company_name = brief_dict.get("company_name", "your company")
        segment = brief_dict.get("icp", {}).get("segment", "abstain")

        # Use the second gap finding (or first if only one) as the new data point
        gap_findings = competitor_gap.get("gap_findings", [])
        new_gap = gap_findings[1] if len(gap_findings) > 1 else (gap_findings[0] if gap_findings else None)

        if not new_gap:
            # No gap to introduce — use sector median context instead
            gap_context = (
                f"Sector median AI maturity is "
                f"{competitor_gap.get('_meta', {}).get('sector_median_score', 'unknown')} vs "
                f"prospect score {competitor_gap.get('prospect_ai_maturity_score', 'unknown')}."
            )
        else:
            peer_names = [e["competitor_name"] for e in new_gap.get("peer_evidence", [])[:2]]
            gap_context = (
                f"New data point: {new_gap['practice']}.\n"
                f"Peer companies showing this: {', '.join(peer_names)}.\n"
                f"Prospect state: {new_gap['prospect_state']}\n"
                f"Confidence: {new_gap.get('confidence', 'medium')}"
            )

        system_prompt = f"""You write cold outbound emails for Tenacious Consulting.
{self._context}
This is Email 2 in a 3-email cold sequence. It introduces a new data point.
DO NOT say: 'just following up', 'circling back', 'hope this finds you well'.
The new data carries the justification for the touch."""

        user_prompt = f"""Company: {company_name}
Prospect: {prospect_name or 'the engineering leader'} ({prospect_role or 'CTO/VP Engineering'})
New competitor data point to introduce:
{gap_context}

Write Email 2 (research-finding follow-up):
Structure:
  One-line opener (no 'just following up').
  1–2 sentences: the new data point, grounded in peer companies above.
  One question: is this pattern deliberate, or is the function being scoped?
  Softer ask than Email 1 — a conversation about the pattern, not a product pitch.
  Signature.

Rules:
- Subject ≤ 60 characters. Pattern: "One more data point: [specific signal]"
  or "Your peer [sector] companies and [capability]"
- Body ≤ 100 words. No bumping language. One question. End with: [DRAFT — not for deployment without review]

Return JSON: {{"subject": "...", "text_body": "...", "html_body": "<p>...</p>"}}"""

        try:
            result = chat_json(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tier=self.llm_tier,
                max_tokens=400,
                trace_name="message_agent.followup",
                trace_metadata={"company": company_name, "sequence_position": 2},
            )
        except Exception as exc:
            print(f"[message_agent] LLM failed for follow-up: {exc}")
            result = self._fallback_followup(company_name, prospect_name, gap_context)

        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        print(f"[message_agent] Email 2 generated in {elapsed_ms}ms")

        return {
            "subject": result.get("subject", f"One more data point: {company_name}")[:60],
            "html_body": result.get("html_body", f"<p>{result.get('text_body', '')}</p>"),
            "text_body": result.get("text_body", ""),
            "variant": "signal_grounded" if new_gap else "generic",
            "pitch_angle": pitch_angle,
            "company_name": company_name,
            "sequence_position": 2,
            "draft": True,
        }

    @traced("message_agent.close")
    def generate_close_email(
        self,
        insight: dict,
        prospect_name: Optional[str] = None,
    ) -> dict:
        """
        Email 3 (Day 12) — Gracious close. Max 70 words.
        Per cold.md: leave a door open without pestering.
        'Closing the loop' outperforms a fourth follow-up.
        """
        t0 = time.perf_counter()

        brief_dict = insight.get("hiring_signal_brief", {})
        company_name = brief_dict.get("company_name", "your company")
        pitch_angle = insight.get("pitch_angle", "generic_exploratory")

        system_prompt = f"""You write cold outbound emails for Tenacious Consulting.
{self._context}
This is Email 3 — the gracious close. Maximum 70 words.
Tone: warm but not needy. Leave a door open without pestering.
DO NOT: guilt-trip, say 'following up again', use fake urgency.
A clean close outperforms a fourth follow-up on both immediate reply rate
and six-month pipeline conversion."""

        user_prompt = f"""Company: {company_name}
Prospect name: {prospect_name or 'the engineering leader'}

Write Email 3 (gracious close):
Structure:
  One sentence: acknowledge timing probably isn't right.
  One sentence: specific non-pushy invitation — offer raw signal data, a one-pager, or check back in 6 months.
  Signature.

Rules:
- Subject ≤ 60 characters. Pattern: "Closing the loop on [original topic]" or "Last note from my side"
- Body ≤ 70 words. No emojis. No guilt. End with: [DRAFT — not for deployment without review]

Return JSON: {{"subject": "...", "text_body": "...", "html_body": "<p>...</p>"}}"""

        try:
            result = chat_json(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tier=self.llm_tier,
                max_tokens=250,
                trace_name="message_agent.close",
                trace_metadata={"company": company_name, "sequence_position": 3},
            )
        except Exception as exc:
            print(f"[message_agent] LLM failed for close: {exc}")
            result = self._fallback_close(company_name, prospect_name)

        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        print(f"[message_agent] Email 3 generated in {elapsed_ms}ms")

        return {
            "subject": result.get("subject", f"Closing the loop on {company_name}")[:60],
            "html_body": result.get("html_body", f"<p>{result.get('text_body', '')}</p>"),
            "text_body": result.get("text_body", ""),
            "variant": "generic",
            "pitch_angle": pitch_angle,
            "company_name": company_name,
            "sequence_position": 3,
            "draft": True,
        }

    # ── Subject signal phrase extraction ─────────────────────────────────────

    def _extract_signal_phrase(self, brief_dict: dict, segment: str) -> str:
        """Extract the best short phrase to fill the subject line pattern."""
        funding = brief_dict.get("buying_window_signals", {}).get("funding_event", {})
        leadership = brief_dict.get("buying_window_signals", {}).get("leadership_change", {})
        layoff = brief_dict.get("buying_window_signals", {}).get("layoff_event", {})
        company = brief_dict.get("company_name", "your company")
        hiring = brief_dict.get("hiring_velocity", {})

        if segment == "recently_funded" and funding.get("detected"):
            stage = funding.get("stage", "funding round").replace("_", " ").title()
            amount = funding.get("amount_usd")
            if amount:
                m = amount // 1_000_000
                return f"${m}M {stage}"
            return f"{stage} round"

        if segment == "cost_restructuring" and layoff.get("detected"):
            return "your recent restructure"

        if segment == "leadership_change" and leadership.get("detected"):
            role = leadership.get("role", "leadership").replace("_", " ").upper()
            return f"{role} appointment"

        if segment == "capability_gap":
            ai_score = brief_dict.get("ai_maturity", {}).get("score", 0)
            return "your AI platform roadmap"

        roles = hiring.get("open_roles_today", 0)
        if roles > 0:
            return f"{roles} open engineering roles"

        return f"{company} engineering"

    # ── Fallback email generators (LLM-free) ─────────────────────────────────

    def _fallback_opener(
        self,
        company_name: str,
        prospect_name: Optional[str],
        prospect_role: Optional[str],
        narrative: str,
        pitch_angle: str,
    ) -> dict:
        greeting = prospect_name or "Hi"
        subject = f"Context: {company_name} engineering"[:60]

        if narrative and pitch_angle != "generic_exploratory":
            body = (
                f"{greeting},\n\n"
                f"{narrative}\n\n"
                "We run dedicated engineering squads for teams at this stage — engineers "
                "available in 7–14 days, embedded in your stack.\n\n"
                "Worth 15 minutes to see if there's a fit? → [Cal link]\n\n"
                "Research Partner\nTenacious Intelligence Corporation\ngettenacious.com\n\n"
                "[DRAFT — not for deployment without review]"
            )
        else:
            body = (
                f"{greeting},\n\n"
                f"I came across {company_name} and wanted to reach out directly.\n\n"
                "Tenacious provides dedicated engineering and data teams — typically 3–12 engineers, "
                "6–24 months, managed under Tenacious and delivering to your product.\n\n"
                "Is engineering capacity on your roadmap this quarter? → [Cal link]\n\n"
                "Research Partner\nTenacious Intelligence Corporation\ngettenacious.com\n\n"
                "[DRAFT — not for deployment without review]"
            )

        return {
            "subject": subject,
            "text_body": body,
            "html_body": body.replace("\n\n", "<br><br>").replace("\n", "<br>"),
        }

    def _fallback_followup(
        self,
        company_name: str,
        prospect_name: Optional[str],
        gap_context: str,
    ) -> dict:
        greeting = prospect_name or "Hi"
        subject = f"One more data point: {company_name}"[:60]
        body = (
            f"{greeting},\n\n"
            f"Adding one data point from our research on {company_name}'s sector.\n\n"
            f"{gap_context[:200]}\n\n"
            "Curious whether the pattern is deliberate or still being scoped. "
            "Happy to compare notes if useful. → [Cal link]\n\n"
            "Research Partner\nTenacious Intelligence Corporation\ngettenacious.com\n\n"
            "[DRAFT — not for deployment without review]"
        )
        return {
            "subject": subject,
            "text_body": body,
            "html_body": body.replace("\n\n", "<br><br>").replace("\n", "<br>"),
        }

    def _fallback_close(
        self,
        company_name: str,
        prospect_name: Optional[str],
    ) -> dict:
        greeting = prospect_name or "Hi"
        subject = f"Closing the loop on our research note"[:60]
        body = (
            f"{greeting},\n\n"
            "Looks like the timing isn't right — that's fine.\n\n"
            "If the hiring-velocity data on your sector would be useful on its own, "
            "reply 'yes' and I'll drop a one-pager in your inbox, no calendar ask. "
            "Otherwise I'll check back in Q3.\n\n"
            "Research Partner\nTenacious Intelligence Corporation\ngettenacious.com\n\n"
            "[DRAFT — not for deployment without review]"
        )
        return {
            "subject": subject,
            "text_body": body,
            "html_body": body.replace("\n\n", "<br><br>").replace("\n", "<br>"),
        }
