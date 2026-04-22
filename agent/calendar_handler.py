"""
agent/calendar_handler.py

Cal.com REST API integration.
Used to book discovery calls between prospects and Tenacious delivery leads.
The agent's final objective is to book this call with a clear context brief.

All bookings are marked 'draft' in metadata per data-handling policy.
"""

import os
from datetime import date, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

_CALCOM_API_KEY = os.getenv("CALCOM_API_KEY")
_CALCOM_BASE_URL = os.getenv("CALCOM_BASE_URL", "https://api.cal.com/v1")
_DISCOVERY_EVENT_TYPE_ID = int(os.getenv("CALCOM_EVENT_TYPE_ID", "1"))


def get_available_slots(
    event_type_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    timezone: str = "UTC",
) -> list[dict]:
    """
    Fetch available booking slots for the Tenacious discovery call event.

    Args:
        event_type_id:  Cal.com event type ID (defaults to CALCOM_EVENT_TYPE_ID env)
        start_date:     ISO date string e.g. '2026-04-22' (defaults to today)
        end_date:       ISO date string e.g. '2026-04-29' (defaults to today + 7 days)
        timezone:       Prospect timezone e.g. 'America/New_York'

    Returns:
        List of slot dicts with startTime fields
    """
    event_id = event_type_id or _DISCOVERY_EVENT_TYPE_ID
    start = start_date or date.today().isoformat()
    end = end_date or (date.today() + timedelta(days=7)).isoformat()

    if not _CALCOM_API_KEY:
        print("[calendar_handler] CALCOM_API_KEY not set - returning mock slot")
        return [{"startTime": f"{start}T10:00:00Z", "endTime": f"{start}T10:30:00Z"}]

    try:
        response = requests.get(
            f"{_CALCOM_BASE_URL}/slots",
            params={
                "apiKey": _CALCOM_API_KEY,
                "eventTypeId": event_id,
                "startTime": start,
                "endTime": end,
                "timeZone": timezone,
            },
            timeout=10,
        )
        response.raise_for_status()
        slots_by_date = response.json().get("slots", {})

        flat_slots = []
        for _date, times in slots_by_date.items():
            for slot in times:
                flat_slots.append({
                    "startTime": slot.get("time"),
                    "date": _date,
                })

        print(f"[calendar_handler] {len(flat_slots)} slots available")
        return flat_slots

    except Exception as exc:
        print(f"[calendar_handler] Slot fetch failed: {exc}")
        return []


def book_discovery_call(
    name: str,
    email: str,
    start_time: str,
    context_brief: str,
    event_type_id: Optional[int] = None,
    timezone: str = "UTC",
) -> dict:
    """
    Book a discovery call for a qualified prospect.

    Args:
        name:           Prospect name
        email:          Prospect email
        start_time:     ISO datetime e.g. '2026-04-22T14:00:00Z'
        context_brief:  Summary of hiring signal brief attached to the booking
        event_type_id:  Cal.com event type ID (defaults to env)
        timezone:       Prospect timezone

    Returns:
        Cal.com booking confirmation dict
    """
    event_id = event_type_id or _DISCOVERY_EVENT_TYPE_ID

    if not _CALCOM_API_KEY:
        print("[calendar_handler] CALCOM_API_KEY not set - returning mock booking")
        return {
            "id": "mock-booking-001",
            "start_time": start_time,
            "attendee": email,
            "status": "mock",
        }

    payload = {
        "eventTypeId": event_id,
        "start": start_time,
        "timeZone": timezone,
        "responses": {
            "name": name,
            "email": email,
            "notes": context_brief[:1000],  # Cal.com notes field limit
        },
        "metadata": {
            "source": "tenacious-signalforge",
            "status": "draft",  # data-handling policy
        },
    }

    try:
        response = requests.post(
            f"{_CALCOM_BASE_URL}/bookings",
            params={"apiKey": _CALCOM_API_KEY},
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        booking = response.json()
        print(f"[calendar_handler] Booked call for {email} at {start_time}")
        return booking

    except Exception as exc:
        print(f"[calendar_handler] Booking failed: {exc}")
        return {"error": str(exc), "start_time": start_time, "attendee": email}


# ── Quick smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    slots = get_available_slots()
    print(f"Slots: {slots}")

    if slots:
        booking = book_discovery_call(
            name="Alex Test",
            email="test@example.com",
            start_time=slots[0].get("startTime", ""),
            context_brief="Test booking from smoke test.",
        )
        print(f"Booking: {booking}")
