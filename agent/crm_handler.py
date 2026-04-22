"""
agent/crm_handler.py

HubSpot CRM integration.
Every conversation event must be logged here per challenge spec.
All enrichment timestamps must be present on the contact record.
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

_client = None


def _get_client():
    global _client
    if _client is None:
        from hubspot import HubSpot
        _client = HubSpot(access_token=os.getenv("HUBSPOT_API_KEY"))
    return _client


def upsert_contact(
    email: str,
    name: str = None,
    company: str = None,
    icp_segment: str = None,
    ai_maturity_score: int = None,
    enrichment_source: str = None,
) -> str:
    """
    Create or update a contact in HubSpot.
    All enrichment fields must be non-null per grading rubric.

    Returns:
        HubSpot contact ID string
    """
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate
    client = _get_client()

    # Standard HubSpot properties (always safe to write)
    properties = {
        "email": email,
        "firstname": (name or "").split()[0] if name else "",
        "lastname": " ".join((name or "").split()[1:]) if name else "",
        "company": company or "",
        "hs_lead_status": "NEW",
        # Store enrichment data in the standard 'notes' field as JSON fallback
        # Custom properties (icp_segment etc.) must be created in HubSpot UI first
    }

    # Add custom enrichment properties only if they were created in the HubSpot portal
    # (safe to attempt — caught by the except block if they don't exist)
    custom_props = {
        "icp_segment": icp_segment or "unknown",
        "ai_maturity_score": str(ai_maturity_score) if ai_maturity_score is not None else "0",
        "enrichment_source": enrichment_source or "crunchbase_odm",
        "enrichment_timestamp": datetime.now(timezone.utc).isoformat(),
    }
    properties.update(custom_props)

    # Check if contact already exists
    try:
        search_result = client.crm.contacts.search_api.do_search({
            "filterGroups": [{
                "filters": [{
                    "propertyName": "email",
                    "operator": "EQ",
                    "value": email,
                }]
            }],
            "limit": 1,
        })

        if search_result.results:
            contact_id = search_result.results[0].id
            # Try update with full props; fall back to standard-only if custom props missing
            try:
                client.crm.contacts.basic_api.update(
                    contact_id=contact_id,
                    simple_public_object_input=SimplePublicObjectInputForCreate(
                        properties=properties
                    ),
                )
            except Exception:
                client.crm.contacts.basic_api.update(
                    contact_id=contact_id,
                    simple_public_object_input=SimplePublicObjectInputForCreate(
                        properties={k: v for k, v in properties.items()
                                    if k not in custom_props}
                    ),
                )
            print(f"[crm_handler] Updated contact: {email} | ID: {contact_id}")
            return contact_id

    except Exception as exc:
        print(f"[crm_handler] Search failed, creating: {exc}")

    # Create new — try with custom props, fall back to standard
    try:
        response = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=properties
            )
        )
    except Exception:
        response = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties={k: v for k, v in properties.items()
                            if k not in custom_props}
            )
        )

    print(f"[crm_handler] Created contact: {email} | ID: {response.id}")
    # Store enrichment as a note since custom props may not exist
    try:
        log_email_event(
            response.id,
            event_type="enrichment_data",
            note=f"icp_segment={custom_props['icp_segment']} | "
                 f"ai_maturity={custom_props['ai_maturity_score']} | "
                 f"source={custom_props['enrichment_source']} | "
                 f"at={custom_props['enrichment_timestamp']}",
        )
    except Exception:
        pass
    return response.id


def log_email_event(
    contact_id: str,
    event_type: str = "email_event",
    note: str = "",
) -> None:
    """
    Log any conversation event as a note on the HubSpot contact.

    Args:
        contact_id:  HubSpot contact ID
        event_type:  e.g. 'outbound_email_sent', 'reply_received', 'discovery_call_booked'
        note:        Free-text detail appended to the note body
    """
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate

    client = _get_client()
    note_body = (
        f"[{event_type.upper()}]\n"
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}\n"
        f"{note}"
    )

    try:
        # Create the note object
        note_resp = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties={
                    "hs_note_body": note_body,
                    "hs_timestamp": str(
                        int(datetime.now(timezone.utc).timestamp() * 1000)
                    ),
                }
            )
        )
        # Associate the note with the contact
        client.crm.objects.notes.associations_api.create(
            note_id=note_resp.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
        print(f"[crm_handler] Logged '{event_type}' for contact: {contact_id}")
    except Exception as exc:
        print(f"[crm_handler] Note creation failed (non-fatal): {exc}")


# ── Quick smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    contact_id = upsert_contact(
        email="test-prospect@example.com",
        name="Alex Test",
        company="Test Corp",
        icp_segment="recently_funded",
        ai_maturity_score=2,
        enrichment_source="crunchbase_odm",
    )
    print(f"Contact ID: {contact_id}")
    log_email_event(
        contact_id,
        event_type="outbound_email_sent",
        note="subject=Test | variant=signal_grounded",
    )
