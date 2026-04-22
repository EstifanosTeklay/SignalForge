"""
agent/sms_handler.py

Africa's Talking sandbox integration.
SMS is SECONDARY channel only — used for warm leads who have already
replied by email and prefer fast coordination for scheduling.

Never send cold SMS. Only trigger after email reply received.
"""

import os
import africastalking
from dotenv import load_dotenv

load_dotenv()

# Initialise Africa's Talking sandbox
africastalking.initialize(
    username=os.getenv("AT_USERNAME", "sandbox"),
    api_key=os.getenv("AT_API_KEY", ""),
)

sms = africastalking.SMS


def send_sms(
    to: str,
    message: str,
    prospect_id: str = None,
) -> dict:
    """
    Send an SMS to a warm lead for scheduling coordination.

    Args:
        to:           Phone number in international format e.g. +1234567890
        message:      SMS body (keep under 160 chars for single segment)
        prospect_id:  HubSpot contact ID for tracing

    Returns:
        Africa's Talking response dict
    """
    if len(message) > 160:
        print(f"[sms_handler] Warning: message exceeds 160 chars ({len(message)})")

    response = sms.send(
        message=message,
        recipients=[to],
        sender_id=os.getenv("AT_SHORTCODE", ""),
    )

    print(f"[sms_handler] SMS sent to {to} | prospect: {prospect_id}")
    return response


def handle_sms_webhook(payload: dict) -> dict:
    """
    Parse an inbound SMS webhook from Africa's Talking.
    Called when a warm lead replies via SMS.

    Args:
        payload: Raw webhook payload from Africa's Talking

    Returns:
        Parsed SMS dict with sender, message
    """
    reply = {
        "from":    payload.get("from", ""),
        "message": payload.get("text", ""),
        "date":    payload.get("date", ""),
    }

    print(f"[sms_handler] SMS reply from {reply['from']}: {reply['message']}")
    return reply


# ── Quick smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Sandbox routes to staff sink — safe to test
    response = send_sms(
        to=os.getenv("TEST_PHONE", "+1234567890"),
        message="Tenacious stack check — SMS handler wired up.",
        prospect_id="test-001",
    )
    print(response)
