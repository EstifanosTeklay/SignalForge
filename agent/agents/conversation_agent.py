"""
agent/agents/conversation_agent.py

💬 Agent 4 — Conversation Agent

Role:
  - Handles prospect replies (email webhook payloads)
  - Maintains conversation context via HubSpot notes
  - Runs qualification Q&A through the reply thread
  - Triggers Cal.com booking when ready
  - Escalates to SMS for warm leads who prefer fast scheduling coordination

This is the agent that solves the 30-40% stall problem.
It keeps the thread alive with context-aware follow-ups
without requiring a human to be in the loop.

State machine:
  COLD      → email sent, no reply
  REPLIED   → prospect replied; qualifying
  QUALIFIED → ICP confirmed; ready to book
  BOOKED    → discovery call on calendar
  STALLED   → no reply for N days (triggers SMS nudge)
  CLOSED    → disqualified or lost
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from agent.calendar_handler import get_available_slots, book_discovery_call
from agent.crm_handler import log_email_event, upsert_contact
from agent.llm_client import chat_json
from agent.observability import traced
from agent.sms_handler import send_sms

# Days without reply before SMS nudge fires
_STALL_THRESHOLD_DAYS = 3

# Qualification questions by ICP segment
_QUAL_QUESTIONS = {
    "recently_funded": [
        "What does your engineering team look like right now — headcount and key gaps?",
        "Are you hiring to fill those gaps in-house, or are you open to alternatives?",
        "What is the one deliverable that would make the next 6 months a success?",
    ],
    "cost_restructuring": [
        "What part of the engineering org are you looking to right-size?",
        "Is the goal to reduce headcount cost, increase output, or both?",
        "What does your current offshore or outsourcing mix look like?",
    ],
    "leadership_change": [
        "As you settle in, are you reassessing the vendor mix or offshore strategy?",
        "What are the first two or three deliverables on your new roadmap?",
        "What does success look like in your first 90 days?",
    ],
    "capability_gap": [
        "What specific capability is blocking the team right now?",
        "Have you tried to hire for this in-house, and what was the outcome?",
        "Is this a point project or an ongoing function you need to build?",
    ],
    "unknown": [
        "What are the biggest engineering bottlenecks you are dealing with this quarter?",
        "Are you open to offshore or distributed team models?",
    ],
}


class ConversationAgent:
    """
    Stateful reply handler. Each call processes one inbound reply.
    State is persisted in HubSpot contact notes.
    """

    def __init__(self, llm_tier: str = "dev"):
        self.llm_tier = llm_tier

    @traced("conversation_agent.handle_reply")
    def handle_reply(
        self,
        reply: dict,
        contact_id: str,
        thread_state: dict,
        insight: dict,
    ) -> dict:
        """
        Process one inbound reply and determine next action.

        Args:
            reply:        Parsed reply dict from email_handler.handle_reply_webhook()
            contact_id:   HubSpot contact ID
            thread_state: Current state dict (loaded from HubSpot or local store)
            insight:      InsightAgent output for this prospect

        Returns:
            {
              "action": "follow_up" | "book_call" | "send_sms" | "close" | "escalate_human",
              "next_email": dict | None,
              "booking": dict | None,
              "sms_body": str | None,
              "updated_state": dict,
            }
        """
        t0 = time.perf_counter()

        state = dict(thread_state)
        state["status"] = "replied"
        state["reply_count"] = state.get("reply_count", 0) + 1
        state["last_reply_at"] = datetime.now(timezone.utc).isoformat()

        reply_text = reply.get("body", "")
        company = insight.get("hiring_signal_brief", {}).get("company_name", "your company")
        segment = (
            insight.get("hiring_signal_brief", {})
            .get("icp", {})
            .get("segment", "unknown")
        )

        # ── Classify intent of this reply ────────────────────────────────────
        intent = self._classify_intent(reply_text)

        # Log to HubSpot
        log_email_event(
            contact_id,
            event_type="reply_received",
            note=f"Reply intent: {intent} | body: {reply_text[:200]}",
        )

        action_result: dict = {}

        if intent == "disqualified":
            state["status"] = "closed"
            action_result = {
                "action": "close",
                "next_email": None,
                "booking": None,
                "sms_body": None,
            }

        elif intent == "ready_to_book":
            booking = self._book_call(contact_id, company, insight)
            state["status"] = "booked"
            action_result = {
                "action": "book_call",
                "booking": booking,
                "next_email": self._confirm_email(company, booking),
                "sms_body": None,
            }

        elif intent == "prefers_sms":
            phone = state.get("phone_number")
            sms_body = self._sms_scheduling_message(company)
            if phone:
                send_sms(phone, sms_body, contact_id=contact_id)
            state["status"] = "sms_sent"
            action_result = {
                "action": "send_sms",
                "sms_body": sms_body,
                "next_email": None,
                "booking": None,
            }

        else:
            # Continue qualification
            follow_up = self._generate_follow_up(
                reply_text, segment, state, insight
            )
            action_result = {
                "action": "follow_up",
                "next_email": follow_up,
                "booking": None,
                "sms_body": None,
            }

        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        print(
            f"[conversation_agent] '{company}' reply #{state['reply_count']} → "
            f"intent={intent} action={action_result.get('action')} | {elapsed_ms}ms"
        )

        return {
            **action_result,
            "updated_state": state,
        }

    @traced("conversation_agent.check_stall")
    def check_stall(
        self,
        contact_id: str,
        thread_state: dict,
        prospect_email: str,
        prospect_phone: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Check if a thread has stalled and fire a nudge if so.
        Called by a scheduler or cron; returns action dict or None.
        """
        from datetime import timedelta

        last_reply = thread_state.get("last_reply_at") or thread_state.get("email_sent_at")
        if not last_reply:
            return None

        last_dt = datetime.fromisoformat(last_reply.replace("Z", "+00:00"))
        days_stalled = (datetime.now(timezone.utc) - last_dt).days

        if days_stalled < _STALL_THRESHOLD_DAYS:
            return None

        if thread_state.get("status") in ("booked", "closed"):
            return None

        # Fire SMS nudge if we have a phone number (warm lead only)
        if prospect_phone and thread_state.get("status") == "replied":
            sms_body = (
                f"Hi — just following up on my email re: Tenacious. "
                "Happy to pick a time that suits you. "
                "Any slot this week work?"
            )
            send_sms(prospect_phone, sms_body, contact_id=contact_id)
            return {"action": "sms_nudge_sent", "days_stalled": days_stalled}

        # Otherwise a follow-up email
        return {
            "action": "stall_follow_up_email_queued",
            "days_stalled": days_stalled,
        }

    def _classify_intent(self, reply_text: str) -> str:
        """
        Classify the prospect's intent from their reply text.
        Returns one of: 'interested', 'ready_to_book', 'prefers_sms',
                        'objection', 'disqualified', 'unknown'.
        """
        text_lower = reply_text.lower()

        # Hard signals
        disq_phrases = [
            "not interested", "unsubscribe", "remove me", "do not contact",
            "no thanks", "not a fit", "we handle this internally",
        ]
        if any(p in text_lower for p in disq_phrases):
            return "disqualified"

        book_phrases = [
            "let's talk", "happy to chat", "book a call", "schedule a call",
            "send me a link", "calendar", "meeting invite", "zoom", "teams",
        ]
        if any(p in text_lower for p in book_phrases):
            return "ready_to_book"

        sms_phrases = ["text me", "whatsapp", "call me", "my number", "phone"]
        if any(p in text_lower for p in sms_phrases):
            return "prefers_sms"

        objection_phrases = [
            "too expensive", "not in the budget", "already have a vendor",
            "not right now", "maybe next quarter",
        ]
        if any(p in text_lower for p in objection_phrases):
            return "objection"

        return "interested"

    def _generate_follow_up(
        self,
        reply_text: str,
        segment: str,
        state: dict,
        insight: dict,
    ) -> dict:
        """Generate a contextual follow-up email via LLM."""
        reply_count = state.get("reply_count", 1)
        qual_questions = _QUAL_QUESTIONS.get(segment, _QUAL_QUESTIONS["unknown"])
        # Advance through questions on each reply
        next_question = qual_questions[min(reply_count - 1, len(qual_questions) - 1)]

        narrative = insight.get("narrative", "")
        company = insight.get("hiring_signal_brief", {}).get("company_name", "")

        system_prompt = """You handle B2B sales email follow-ups for Tenacious Consulting.
Rules:
- Max 100 words.
- Acknowledge what the prospect said specifically.
- Ask one qualification question (provided).
- Peer-to-peer tone. No hype language.
- End with: [DRAFT — not for deployment without review]
Return JSON: {"subject": "...", "text_body": "..."}"""

        user_prompt = f"""Prospect replied: "{reply_text[:300]}"
Company: {company}
Previous narrative context: {narrative[:200]}
Qualification question to ask: {next_question}
Reply #{reply_count} in thread."""

        try:
            result = chat_json(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tier=self.llm_tier,
                max_tokens=300,
                trace_name="conversation_agent.follow_up",
            )
            return {**result, "draft": True}
        except Exception as exc:
            print(f"[conversation_agent] Follow-up LLM failed: {exc}")
            return {
                "subject": f"Re: {company} — quick follow-up",
                "text_body": (
                    f"Thanks for the reply.\n\n{next_question}\n\n"
                    "Best,\nTenacious Delivery Team\n\n"
                    "[DRAFT — not for deployment without review]"
                ),
                "draft": True,
            }

    def _book_call(self, contact_id: str, company: str, insight: dict) -> dict:
        """Fetch slots and book the first available discovery call."""
        slots = get_available_slots()
        if not slots:
            return {"status": "no_slots", "company": company}

        first_slot = slots[0] if isinstance(slots, list) else {}
        start_time = first_slot.get("startTime", "")

        context_brief = (
            f"ICP segment: {insight.get('hiring_signal_brief', {}).get('icp', {}).get('segment')}\n"
            f"AI maturity: {insight.get('hiring_signal_brief', {}).get('ai_maturity', {}).get('score')}/3\n"
            f"Pitch angle: {insight.get('pitch_angle')}\n"
            f"Narrative: {insight.get('narrative', '')[:300]}"
        )

        booking = book_discovery_call(
            name=company,
            email=f"prospect@{company.lower().replace(' ', '')}.com",
            start_time=start_time,
            context_brief=context_brief,
        )
        log_email_event(
            contact_id,
            event_type="discovery_call_booked",
            note=f"Cal.com booking: {start_time}",
        )
        return booking

    def _confirm_email(self, company: str, booking: dict) -> dict:
        """Generate a booking confirmation email."""
        slot = booking.get("start_time") or booking.get("startTime", "TBD")
        return {
            "subject": f"Discovery call confirmed — {company}",
            "text_body": (
                f"Hi,\n\nYour discovery call with Tenacious is confirmed for {slot}.\n\n"
                "The call is 30 minutes with a Tenacious delivery lead. "
                "We will send the context brief ahead of time.\n\n"
                "Best,\nTenacious Delivery Team\n\n"
                "[DRAFT — not for deployment without review]"
            ),
            "draft": True,
        }

    def _sms_scheduling_message(self, company: str) -> str:
        return (
            f"Hi — Tenacious here re: {company}. "
            "Happy to find a time that works for you. "
            "What does this week look like?"
        )
