"""
agent/email_handler.py

Resend integration — outbound email sending and reply/bounce webhook handling.
Primary outreach channel for Tenacious prospects.

All outbound is marked 'draft' in metadata per data-handling policy.
Default sender uses Resend shared domain for challenge week.

Integration hooks
-----------------
send_email()         accepts on_success / on_failure callbacks.
handle_reply_webhook() accepts on_reply / on_bounce callbacks.
Callers (pipeline, FastAPI webhook route) pass in their own handlers;
this module never silently swallows events.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

import resend
from dotenv import load_dotenv

load_dotenv()

resend.api_key = os.getenv("RESEND_API_KEY")
SENDER = os.getenv("RESEND_SENDER", "onboarding@resend.dev")


class EmailSendError(Exception):
    """Raised when Resend rejects or fails to deliver an outbound email."""

    def __init__(self, message: str, status_code: int = 0, resend_error: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.resend_error = resend_error


class WebhookValidationError(ValueError):
    """Raised when an inbound webhook payload is missing required fields."""


# ── Validation helpers ────────────────────────────────────────────────────────

def _validate_send_params(to: str, subject: str, html_body: str) -> None:
    if not to or "@" not in to:
        raise ValueError(f"Invalid recipient address: {to!r}")
    if not subject or not subject.strip():
        raise ValueError("Email subject must not be empty")
    if not html_body or not html_body.strip():
        raise ValueError("Email body must not be empty")


def _validate_webhook_payload(payload: dict) -> None:
    """
    Resend delivers two distinct shapes:
      - Inbound reply  : top-level 'from', 'subject', 'text'/'html'
      - Event webhook  : top-level 'type' + 'data' sub-object

    We require at least one recognisable shape.
    """
    if not isinstance(payload, dict):
        raise WebhookValidationError(
            f"Webhook payload must be a dict, got {type(payload).__name__}"
        )

    has_event_shape = "type" in payload and "data" in payload
    has_reply_shape = "from" in payload and ("text" in payload or "html" in payload)

    if not has_event_shape and not has_reply_shape:
        raise WebhookValidationError(
            "Webhook payload missing required fields: "
            "expected either {'type','data'} (event) or {'from','text'/'html'} (reply)"
        )


# ── Public API ────────────────────────────────────────────────────────────────

def send_email(
    to: str,
    subject: str,
    html_body: str,
    prospect_id: str = None,
    variant: str = "generic",
    *,
    on_success: Optional[Callable[[dict], None]] = None,
    on_failure: Optional[Callable[[EmailSendError], None]] = None,
) -> dict:
    """
    Send an outbound email via Resend.

    Args:
        to:           Recipient email address.
        subject:      Email subject line.
        html_body:    HTML email body.
        prospect_id:  HubSpot contact ID for tracing.
        variant:      Email variant tag for A/B tracking.
        on_success:   Optional callback(response_dict) fired after a successful send.
        on_failure:   Optional callback(EmailSendError) fired on any send failure.
                      If on_failure is None and sending fails, EmailSendError is raised.

    Returns:
        dict with at least {'id': str, 'status': 'sent'} on success,
        or {'status': 'failed', 'error': str} when on_failure absorbs the error.
    """
    _validate_send_params(to, subject, html_body)

    try:
        response = resend.Emails.send({
            "from": SENDER,
            "to": to,
            "subject": subject,
            "html": html_body,
            "tags": [
                {"name": "prospect_id", "value": prospect_id or "unknown"},
                {"name": "variant", "value": variant},
                {"name": "status", "value": "draft"},
            ],
        })

        result = {**response, "status": "sent"}
        print(
            f"[email_handler] sent to={to} id={response.get('id')} variant={variant}"
        )

        if on_success:
            on_success(result)
        return result

    except resend.exceptions.ResendError as exc:
        # Resend SDK raises ResendError; the HTTP status lives in exc.code
        status_code = getattr(exc, "code", 0) or getattr(exc, "status_code", 0)
        err = EmailSendError(
            f"Resend API error sending to {to}: {exc}",
            status_code=status_code,
            resend_error=str(exc),
        )
        print(f"[email_handler] SEND FAILED to={to} status={status_code} err={exc}")
        if on_failure:
            on_failure(err)
            return {"status": "failed", "error": str(exc), "status_code": status_code}
        raise err from exc

    except Exception as exc:
        err = EmailSendError(f"Unexpected error sending to {to}: {exc}")
        print(f"[email_handler] SEND ERROR to={to} err={exc}")
        if on_failure:
            on_failure(err)
            return {"status": "failed", "error": str(exc)}
        raise err from exc


def handle_reply_webhook(
    payload: dict,
    *,
    on_reply: Optional[Callable[[dict], None]] = None,
    on_bounce: Optional[Callable[[dict], None]] = None,
) -> dict:
    """
    Parse an inbound webhook from Resend — handles reply, bounce, and delivery events.

    Args:
        payload:   Raw webhook payload dict from Resend.
        on_reply:  Optional callback(reply_dict) fired when a genuine reply arrives.
        on_bounce: Optional callback(bounce_dict) fired on bounce or spam complaint.

    Returns:
        Parsed event dict with 'event_type', 'from', 'subject', 'body', 'email_id'.

    Raises:
        WebhookValidationError: if the payload does not match a known Resend shape.
    """
    _validate_webhook_payload(payload)

    # ── Branch A: Resend event webhook ({'type': ..., 'data': {...}}) ─────────
    if "type" in payload and "data" in payload:
        event_type: str = payload["type"]
        data: dict = payload.get("data", {})

        event = {
            "event_type": event_type,
            "from": data.get("from", ""),
            "subject": data.get("subject", ""),
            "body": data.get("text", "") or data.get("html", ""),
            "email_id": data.get("email_id", "") or data.get("id", ""),
        }

        if event_type in ("email.bounced", "email.complained"):
            bounce = {
                **event,
                "bounce_type": data.get("bounce", {}).get("type", "unknown"),
                "bounce_message": data.get("bounce", {}).get("message", ""),
            }
            print(
                f"[email_handler] BOUNCE event={event_type} to={data.get('to', '?')}"
            )
            if on_bounce:
                on_bounce(bounce)
            return bounce

        print(
            f"[email_handler] event={event_type} email_id={event['email_id']}"
        )
        if event_type == "email.replied" and on_reply:
            on_reply(event)
        return event

    # ── Branch B: Resend inbound reply (direct inbound email) ────────────────
    reply = {
        "event_type": "inbound_reply",
        "from": payload.get("from", ""),
        "subject": payload.get("subject", ""),
        "body": payload.get("text", "") or payload.get("html", ""),
        "email_id": payload.get("inReplyTo", "") or payload.get("email_id", ""),
    }

    print(f"[email_handler] inbound reply from={reply['from']}")
    if on_reply:
        on_reply(reply)
    return reply


# ── Quick smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    def _on_success(r: dict) -> None:
        print(f"[hook] on_success fired: id={r.get('id')}")

    def _on_failure(e: EmailSendError) -> None:
        print(f"[hook] on_failure fired: {e} (status={e.status_code})")

    response = send_email(
        to=os.getenv("TEST_EMAIL", "test@example.com"),
        subject="Tenacious stack check — email handler",
        html_body="<p>Email handler is wired up.</p>",
        prospect_id="test-001",
        variant="generic",
        on_success=_on_success,
        on_failure=_on_failure,
    )
    print(response)
