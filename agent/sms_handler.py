"""
agent/sms_handler.py

Africa's Talking sandbox integration.
SMS is SECONDARY channel only — used for warm leads who have already
replied by email and prefer fast coordination for scheduling.

Never send cold SMS. Only trigger after email reply received.

Gating
------
send_sms() requires a lead_state argument.  Only states in WARM_LEAD_STATES
are permitted to receive SMS; all others raise LeadStateError before any
network call is made.

Integration hooks
-----------------
handle_sms_webhook() accepts an on_reply callback so downstream workflows
(ConversationAgent, booking flow) are notified without polling.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

import africastalking
from dotenv import load_dotenv

load_dotenv()

africastalking.initialize(
    username=os.getenv("AT_USERNAME", "sandbox"),
    api_key=os.getenv("AT_API_KEY", ""),
)

sms = africastalking.SMS

# Lead states that permit an SMS touch.
# Any state not in this set is treated as cold and blocked.
WARM_LEAD_STATES: frozenset[str] = frozenset({
    "replied",
    "call_requested",
    "scheduling",
    "qualified",
})


class SmsSendError(Exception):
    """Raised when Africa's Talking rejects or fails to deliver an SMS."""


class LeadStateError(SmsSendError):
    """Raised when send_sms is called for a lead that is not yet warm."""


# ── Gating helper ─────────────────────────────────────────────────────────────

def _assert_warm(lead_state: str) -> None:
    """Raise LeadStateError if lead_state is not in WARM_LEAD_STATES."""
    if lead_state not in WARM_LEAD_STATES:
        raise LeadStateError(
            f"SMS blocked: lead_state={lead_state!r} is not a warm state. "
            f"Allowed: {sorted(WARM_LEAD_STATES)}"
        )


# ── Public API ────────────────────────────────────────────────────────────────

def send_sms(
    to: str,
    message: str,
    prospect_id: str = None,
    *,
    lead_state: str,
    on_success: Optional[Callable[[dict], None]] = None,
    on_failure: Optional[Callable[[SmsSendError], None]] = None,
) -> dict:
    """
    Send an SMS to a warm lead for scheduling coordination.

    Args:
        to:           Phone number in international format e.g. +254700000000
        message:      SMS body (keep under 160 chars for a single segment)
        prospect_id:  HubSpot contact ID for tracing
        lead_state:   Current CRM lead state — must be in WARM_LEAD_STATES or
                      LeadStateError is raised before any API call is made.
        on_success:   Optional callback(response_dict) fired after a successful send.
        on_failure:   Optional callback(SmsSendError) fired on any send failure.
                      If on_failure is None and sending fails, SmsSendError is raised.

    Returns:
        Africa's Talking response dict on success,
        or {'status': 'failed', 'error': str} when on_failure absorbs the error.

    Raises:
        LeadStateError: if lead_state is not warm (always raised; not absorbed by on_failure).
        SmsSendError:   if the AT API call fails and on_failure is not provided.
    """
    # Gate: enforce warm-lead policy in code, not just in comments
    _assert_warm(lead_state)

    if len(message) > 160:
        print(f"[sms_handler] Warning: message exceeds 160 chars ({len(message)})")

    try:
        response = sms.send(
            message=message,
            recipients=[to],
            sender_id=os.getenv("AT_SHORTCODE", ""),
        )
        print(f"[sms_handler] sent to={to} lead_state={lead_state} prospect={prospect_id}")

        if on_success:
            on_success(response)
        return response

    except Exception as exc:
        err = SmsSendError(f"Africa's Talking error sending to {to}: {exc}")
        print(f"[sms_handler] SEND FAILED to={to} err={exc}")
        if on_failure:
            on_failure(err)
            return {"status": "failed", "error": str(exc)}
        raise err from exc


def handle_sms_webhook(
    payload: dict,
    *,
    on_reply: Optional[Callable[[dict], None]] = None,
) -> dict:
    """
    Parse an inbound SMS webhook from Africa's Talking and dispatch downstream.

    Africa's Talking POST body keys: from, to, text, date, id, linkId.

    Args:
        payload:   Raw webhook payload dict from Africa's Talking.
        on_reply:  Callback(reply_dict) invoked for every valid inbound message.
                   The ConversationAgent or booking flow should pass its handler here
                   so replies are routed without polling.

    Returns:
        Parsed reply dict with 'from', 'message', 'date', 'link_id'.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"SMS webhook payload must be a dict, got {type(payload).__name__}")

    reply = {
        "from":     payload.get("from", ""),
        "message":  payload.get("text", ""),
        "date":     payload.get("date", ""),
        "link_id":  payload.get("linkId", ""),
    }

    print(f"[sms_handler] inbound from={reply['from']}: {reply['message'][:60]}")

    if on_reply:
        on_reply(reply)

    return reply


# ── Quick smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    def _on_success(r: dict) -> None:
        print(f"[hook] on_success: {r}")

    def _on_failure(e: SmsSendError) -> None:
        print(f"[hook] on_failure: {e}")

    # This will raise LeadStateError — correct behaviour for a cold lead
    try:
        send_sms(
            to=os.getenv("TEST_PHONE", "+1234567890"),
            message="Tenacious stack check — cold lead, should be blocked.",
            prospect_id="test-cold",
            lead_state="prospecting",
        )
    except LeadStateError as e:
        print(f"[smoke] Correctly blocked cold lead: {e}")

    # Warm lead — routes to AT sandbox
    response = send_sms(
        to=os.getenv("TEST_PHONE", "+1234567890"),
        message="Tenacious stack check — SMS handler wired up.",
        prospect_id="test-001",
        lead_state="replied",
        on_success=_on_success,
        on_failure=_on_failure,
    )
    print(response)
