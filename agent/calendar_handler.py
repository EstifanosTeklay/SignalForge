"""
agent/calendar_handler.py

Cal.com Cloud integration.
Used to book discovery calls between prospects and Tenacious delivery leads.
The agent's final objective is to book this call with a clear context brief.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

CALCOM_API_KEY = os.getenv("CALCOM_API_KEY")
CALCOM_BASE_URL = "https://api.cal.com/v1"


def get_available_slots(
    event_type_id: int,
    start_date: str,
    end_date: str,
    timezone: str = "UTC",
) -> list[dict]:
    """
    Fetch available booking slots for a given event type.

    Args:
        event_type_id:  Cal.com event type ID (discovery call)
        start_date:     ISO date string e.g. '2026-04-22'
        end_date:       ISO date string e.g. '2026-04-29'
        timezone:       Prospect's timezone e.g. 'America/New_York'

    Returns:
        List of available slot dicts with start/end times
    """
    response = requests.get(
        f"{CALCOM_BASE_URL}/slots",
        params={
            "apiKey":      CALCOM_API_KEY,
            "eventTypeId": event_type_id,
            "startTime":   start_date,
            "endTime":     end_date,
            "timeZone":    timezone,
        },
    )
    response.raise_for_status()
    slots = response.json().get("slots", {})

    # Flatten into list
    flat_slots = []
    for date, times in slots.items():
        for slot in times:
            flat_slots.append({"date": date, "time": slot.get("time")})

    print(f"[calendar_handler] Found {len(flat_slots)} available slots")
    return flat_slots


def book_discovery_call(
    event_type_id: int,
    start_time: str,
    prospect_name: str,
    prospect_email: str,
    context_brief: str,
    timezone: str = "UTC",
) -> dict:
    """
    Book a discovery call for a qualified prospect.

    Args:
        event_type_id:   Cal.com event type ID
        start_time:      ISO datetime string e.g. '2026-04-22T14:00:00Z'
        prospect_name:   Full name of prospect
        prospect_email:  Email of prospect
        context_brief:   Summary of hiring signal brief — attached to booking
        timezone:        Prospect's timezone

    Returns:
        Cal.com booking confirmation dict
    """
    payload = {
        "eventTypeId": event_type_id,
        "start":       start_time,
        "timeZone":    timezone,
        "responses": {
            "name":  prospect_name,
            "email": prospect_email,
            "notes": context_brief,
        },
        "metadata": {
            "source":  "tenacious-signal-forge",
            "status":  "draft",  # data-handling policy
        },
    }

    response = requests.post(
        f"{CALCOM_BASE_URL}/bookings",
        params={"apiKey": CALCOM_API_KEY},
        json=payload,
    )
    response.raise_for_status()
    booking = response.json()

    print(f"[calendar_handler] Booked call for {prospect_email} at {start_time}")
    return booking


# ── Quick smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Just verify API key works by fetching event types
    response = requests.get(
        f"{CALCOM_BASE_URL}/event-types",
        params={"apiKey": CALCOM_API_KEY},
    )
    print(f"[calendar_handler] Status: {response.status_code}")
    print(response.json())
