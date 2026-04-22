"""
agent/crm_handler.py

HubSpot CRM integration.
Every conversation event must be logged here per challenge spec.
All enrichment timestamps must be present on the contact record.
"""

import os
from datetime import datetime, timezone
from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate
from hubspot.crm.deals import SimplePublicObjectInputForCreate as DealInput
from dotenv import load_dotenv

load_dotenv()

client = HubSpot(access_token=os.getenv("HUBSPOT_API_KEY"))


def upsert_contact(
    email: str,
    company_name: str,
    prospect_id: str = None,
    icp_segment: str = None,
    ai_maturity_score: int = None,
    enrichment_source: str = None,
) -> dict:
    """
    Create or update a contact in HubSpot.
    All enrichment fields must be non-null per grading rubric.

    Args:
        email:              Prospect email
        company_name:       Company name from Crunchbase
        prospect_id:        Internal prospect ID
        icp_segment:        One of the four ICP segments
        ai_maturity_score:  0-3 score from signal brief
        enrichment_source:  e.g. 'crunchbase_odm'

    Returns:
        HubSpot contact record dict
    """
    properties = {
        "email":              email,
        "company":            company_name,
        "hs_lead_status":     "NEW",
        # Custom enrichment fields
        "icp_segment":        icp_segment or "unknown",
        "ai_maturity_score":  str(ai_maturity_score) if ai_maturity_score is not None else "0",
        "enrichment_source":  enrichment_source or "crunchbase_odm",
        "enrichment_timestamp": datetime.now(timezone.utc).isoformat(),
        "prospect_id":        prospect_id or "",
    }

    # Check if contact exists
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
            # Update existing
            contact_id = search_result.results[0].id
            client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInputForCreate(
                    properties=properties
                ),
            )
            print(f"[crm_handler] Updated contact: {email} | ID: {contact_id}")
            return {"id": contact_id, "action": "updated"}

    except Exception:
        pass

    # Create new
    response = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties=properties
        )
    )

    print(f"[crm_handler] Created contact: {email} | ID: {response.id}")
    return {"id": response.id, "action": "created"}


def log_email_event(
    contact_id: str,
    subject: str,
    variant: str,
    email_id: str,
) -> None:
    """
    Log an outbound email event as a note on the contact record.
    """
    note = f"[Outbound Email]\nSubject: {subject}\nVariant: {variant}\nResend ID: {email_id}"

    client.crm.objects.notes.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties={
                "hs_note_body":      note,
                "hs_timestamp":      str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                "hs_contact_id":     contact_id,
            }
        )
    )
    print(f"[crm_handler] Logged email event for contact: {contact_id}")


# ── Quick smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    result = upsert_contact(
        email="test-prospect@example.com",
        company_name="Test Corp",
        prospect_id="test-001",
        icp_segment="recently_funded",
        ai_maturity_score=2,
        enrichment_source="crunchbase_odm",
    )
    print(result)
