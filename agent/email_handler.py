"""
agent/email_handler.py

Resend integration — outbound email sending and reply webhook handling.
Primary outreach channel for Tenacious prospects.

All outbound is marked 'draft' in metadata per data-handling policy.
Default sender uses Resend shared domain for challenge week.
"""

import os
import resend
from dotenv import load_dotenv

load_dotenv()

resend.api_key = os.getenv("RESEND_API_KEY")

SENDER = os.getenv("RESEND_SENDER", "onboarding@resend.dev")


def send_email(
    to: str,
    subject: str,
    html_body: str,
    prospect_id: str = None,
    variant: str = "generic",
) -> dict:
    """
    Send an outbound email to a prospect.

    Args:
        to:           Recipient email address
        subject:      Email subject line
        html_body:    HTML email body
        prospect_id:  HubSpot contact ID for tracing
        variant:      'signal_grounded' or 'generic' — used for A/B tracking

    Returns:
        Resend response dict with email ID
    """
    response = resend.Emails.send({
        "from": SENDER,
        "to": to,
        "subject": subject,
        "html": html_body,
        "tags": [
            {"name": "prospect_id", "value": prospect_id or "unknown"},
            {"name": "variant", "value": variant},
            {"name": "status", "value": "draft"},  # data-handling policy
        ],
    })

    print(f"[email_handler] Sent to {to} | ID: {response['id']} | variant: {variant}")
    return response


def handle_reply_webhook(payload: dict) -> dict:
    """
    Parse an inbound reply webhook from Resend.
    Called by the backend when a prospect replies to an outreach email.

    Args:
        payload: Raw webhook payload from Resend

    Returns:
        Parsed reply dict with sender, subject, body
    """
    reply = {
        "from":    payload.get("from", ""),
        "subject": payload.get("subject", ""),
        "body":    payload.get("text", "") or payload.get("html", ""),
        "email_id": payload.get("email_id", ""),
    }

    print(f"[email_handler] Reply received from {reply['from']}")
    return reply


# ── Quick smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    response = send_email(
        to=os.getenv("TEST_EMAIL", "test@example.com"),
        subject="Tenacious stack check — email handler",
        html_body="<p>Email handler is wired up.</p>",
        prospect_id="test-001",
        variant="generic",
    )
    print(response)
