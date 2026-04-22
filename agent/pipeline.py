"""
agent/pipeline.py

End-to-end orchestrator — wires all five agents together.

Flow:
  1. ResearchAgent     → Company + HiringSignalBrief
  2. InsightAgent      → narrative + competitor_gap_brief
  3. MessageAgent      → draft email
  4. GuardrailAgent    → approve / correct the draft
  5. (send via email_handler)
  6. On reply: ConversationAgent → follow-up / book / SMS

Usage (synthetic prospect, challenge week):
    python -m agent.pipeline --company "Acme Corp" --slug "acme-corp" --to "prospect@sink.example"

Kill-switch: set OUTBOUND_ENABLED=false to route all emails to SINK_EMAIL.
Default is OUTBOUND_ENABLED=false (staff sink) per data-handling policy.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from agent.agents.conversation_agent import ConversationAgent
from agent.agents.guardrail_agent import GuardrailAgent
from agent.agents.insight_agent import InsightAgent
from agent.agents.message_agent import MessageAgent
from agent.agents.research_agent import ResearchAgent
from agent.crm_handler import log_email_event, upsert_contact
from agent.email_handler import send_email
from agent.observability import flush, start_trace

# ── Kill-switch ───────────────────────────────────────────────────────────────
_OUTBOUND_ENABLED = os.getenv("OUTBOUND_ENABLED", "false").lower() == "true"
_SINK_EMAIL = os.getenv("SINK_EMAIL", "sink@example.com")


def run_outbound(
    company_name: str,
    *,
    wellfound_slug: Optional[str] = None,
    prospect_email: str,
    prospect_name: Optional[str] = None,
    prospect_role: Optional[str] = None,
    prior_job_snapshot: Optional[dict] = None,
    save_briefs: bool = True,
    llm_tier: str = "dev",
) -> dict:
    """
    Full outbound pipeline for one synthetic prospect.

    Returns a result dict suitable for logging and the evidence graph.
    """
    run_id = f"{company_name.lower().replace(' ', '_')}_{int(time.time())}"
    trace = start_trace(
        f"outbound_pipeline/{run_id}",
        metadata={
            "company": company_name,
            "prospect_email": prospect_email,
            "llm_tier": llm_tier,
        },
    )

    t_start = time.perf_counter()
    result: dict = {
        "run_id": run_id,
        "company": company_name,
        "prospect_email": prospect_email,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        # ── Agent 1: Research ─────────────────────────────────────────────────
        print(f"\n[pipeline] -- Agent 1: Research -- {company_name} --")
        research = ResearchAgent(use_job_scraper=bool(wellfound_slug))
        company, brief = research.run(
            company_name,
            wellfound_slug=wellfound_slug,
            prior_job_snapshot=prior_job_snapshot,
        )

        if save_briefs:
            brief_path = f"outputs/{run_id}_hiring_signal_brief.json"
            os.makedirs("outputs", exist_ok=True)
            research.save_brief(brief, brief_path)

        result["icp_segment"] = brief.icp.segment.value
        result["ai_maturity"] = brief.ai_maturity.score
        result["signal_confidence"] = brief.icp.confidence.value

        # ── Agent 2: Insight ──────────────────────────────────────────────────
        print(f"[pipeline] -- Agent 2: Insight --")
        insight_agent = InsightAgent(llm_tier=llm_tier)
        insight_path = f"outputs/{run_id}_insight.json" if save_briefs else None
        insight = insight_agent.run(company, brief, save_path=insight_path)

        result["pitch_angle"] = insight.get("pitch_angle")
        result["gap_count"] = len(insight.get("competitor_gap_brief", {}).get("gaps", []))

        # ── Agent 3: Message ──────────────────────────────────────────────────
        print(f"[pipeline] -- Agent 3: Message --")
        message_agent = MessageAgent(llm_tier=llm_tier)
        draft_email = message_agent.run(
            insight,
            prospect_name=prospect_name,
            prospect_role=prospect_role,
        )

        result["email_variant"] = draft_email.get("variant")

        # ── Agent 5: Guardrail ────────────────────────────────────────────────
        print(f"[pipeline] -- Agent 5: Guardrail --")
        guardrail = GuardrailAgent(llm_tier=llm_tier)
        check = guardrail.check(draft_email, insight.get("hiring_signal_brief"))

        result["guardrail_verdict"] = check["verdict"]
        result["guardrail_flags"] = check["flags"]

        # Apply corrections if any
        final_subject = (
            check.get("corrected_subject") or draft_email["subject"]
        )
        final_body_html = (
            f"<p>{check['corrected_body']}</p>"
            if check.get("corrected_body")
            else draft_email["html_body"]
        )

        # ── CRM: upsert contact ───────────────────────────────────────────────
        print(f"[pipeline] -- CRM: upsert contact --")
        contact_id = upsert_contact(
            email=prospect_email,
            name=prospect_name or company_name,
            company=company_name,
            icp_segment=brief.icp.segment.value,
            ai_maturity_score=brief.ai_maturity.score,
            enrichment_source="crunchbase_odm+layoffs_fyi+wellfound",
        )
        result["hubspot_contact_id"] = contact_id

        # ── Send email (or route to sink) ─────────────────────────────────────
        to_address = prospect_email if _OUTBOUND_ENABLED else _SINK_EMAIL
        if not _OUTBOUND_ENABLED:
            print(
                f"[pipeline] KILL-SWITCH ACTIVE: routing to sink {_SINK_EMAIL} "
                f"(set OUTBOUND_ENABLED=true to enable real outbound)"
            )

        print(f"[pipeline] -- Sending email to {to_address} --")
        email_resp = send_email(
            to=to_address,
            subject=final_subject,
            html_body=final_body_html,
            prospect_id=contact_id,
            variant=draft_email.get("variant", "generic"),
        )

        log_email_event(
            contact_id,
            event_type="outbound_email_sent",
            note=(
                f"subject={final_subject} | "
                f"variant={draft_email.get('variant')} | "
                f"guardrail={check['verdict']} | "
                f"resend_id={email_resp.get('id', 'n/a')}"
            ),
        )

        result["email_sent"] = True
        result["email_id"] = email_resp.get("id")
        result["routed_to_sink"] = not _OUTBOUND_ENABLED

    except Exception as exc:
        result["error"] = str(exc)
        result["email_sent"] = False
        print(f"[pipeline] ERROR: {exc}")
        raise

    finally:
        elapsed_ms = round((time.perf_counter() - t_start) * 1000)
        result["elapsed_ms"] = elapsed_ms
        result["completed_at"] = datetime.now(timezone.utc).isoformat()
        flush()
        print(f"\n[pipeline] Done in {elapsed_ms}ms - {company_name}")

    return result


def handle_webhook_reply(
    webhook_payload: dict,
    contact_id: str,
    thread_state: dict,
    insight: dict,
    prospect_phone: Optional[str] = None,
    llm_tier: str = "dev",
) -> dict:
    """
    Entry point for inbound reply webhooks.
    Called by the FastAPI webhook handler (agent/api/webhook.py).
    """
    from agent.email_handler import handle_reply_webhook

    reply = handle_reply_webhook(webhook_payload)
    if prospect_phone:
        thread_state["phone_number"] = prospect_phone

    conv_agent = ConversationAgent(llm_tier=llm_tier)
    action = conv_agent.handle_reply(
        reply=reply,
        contact_id=contact_id,
        thread_state=thread_state,
        insight=insight,
    )

    # If follow-up email is ready, pass through guardrail then send
    if action.get("next_email") and action["action"] == "follow_up":
        guardrail = GuardrailAgent(llm_tier=llm_tier)
        check = guardrail.check(action["next_email"])
        if check["verdict"] != "BLOCK":
            final_body = (
                check.get("corrected_body") or action["next_email"].get("text_body", "")
            )
            # Send through email_handler (sink routing applies)
            to = _SINK_EMAIL if not _OUTBOUND_ENABLED else thread_state.get("email", _SINK_EMAIL)
            send_email(
                to=to,
                subject=action["next_email"].get("subject", "Re: follow-up"),
                html_body=f"<p>{final_body}</p>",
                prospect_id=contact_id,
                variant="follow_up",
            )

    flush()
    return action


# ── CLI entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SignalForge outbound pipeline — synthetic prospect run"
    )
    parser.add_argument("--company", required=True, help="Company name")
    parser.add_argument("--slug", default=None, help="Wellfound slug for job scrape")
    parser.add_argument("--to", required=True, help="Prospect email (routes to sink unless OUTBOUND_ENABLED=true)")
    parser.add_argument("--name", default=None, help="Prospect first name")
    parser.add_argument("--role", default=None, help="Prospect title")
    parser.add_argument("--tier", default="dev", choices=["dev", "eval"], help="LLM tier")
    parser.add_argument(
        "--snapshot", default=None,
        help="Path to pre-scraped job_snapshot.json (skips live scrape)"
    )
    args = parser.parse_args()

    prior_snapshot = None
    if args.snapshot and os.path.exists(args.snapshot):
        with open(args.snapshot) as f:
            prior_snapshot = json.load(f)

    result = run_outbound(
        company_name=args.company,
        wellfound_slug=args.slug,
        prospect_email=args.to,
        prospect_name=args.name,
        prospect_role=args.role,
        prior_job_snapshot=prior_snapshot,
        llm_tier=args.tier,
    )

    print("\n── Pipeline Result ──")
    print(json.dumps(result, indent=2, default=str))
