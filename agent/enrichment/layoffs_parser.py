"""
agent/enrichment/layoffs_parser.py

Parses layoffs.fyi CC-BY dataset.
Expected path: data/layoffs.csv  (set via LAYOFFS_CSV env var)

CSV columns (layoffs.fyi format):
  Company, Location_HQ, Industry, Laid_Off_Count, Percentage, Date,
  Funds_Raised, Stage, Date_Added, Country, Lat, Lng, List_of_Employees_Laid_Off

Returns LayoffInfo objects for a named company, window-filtered.
"""

from __future__ import annotations

import csv
import os
from datetime import date, datetime, timedelta
from typing import Optional

from agent.models.company import LayoffInfo

_LAYOFFS_CSV = os.getenv("LAYOFFS_CSV", "data/layoffs.csv")

# Active signal window per challenge spec
_WINDOW_DAYS = 120


def _parse_date(s: str) -> Optional[date]:
    if not s or s.strip() in ("", "None", "nan"):
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_pct(s: str) -> Optional[float]:
    if not s or s.strip() in ("", "None", "nan"):
        return None
    try:
        val = float(s.strip().replace("%", ""))
        return val / 100.0 if val > 1.0 else val
    except ValueError:
        return None


def _parse_int(s: str) -> Optional[int]:
    if not s or s.strip() in ("", "None", "nan"):
        return None
    try:
        return int(float(s.strip().replace(",", "")))
    except ValueError:
        return None


def get_layoff_events(
    company_name: str,
    csv_path: str = _LAYOFFS_CSV,
    window_days: int = _WINDOW_DAYS,
) -> list[LayoffInfo]:
    """
    Return all layoff events for a company within the lookback window.
    Name matching: case-insensitive, partial match.
    """
    if not os.path.exists(csv_path):
        return []

    name_lower = company_name.lower().strip()
    cutoff = date.today() - timedelta(days=window_days)
    events: list[LayoffInfo] = []

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_company = (
                row.get("Company", "") or row.get("company", "")
            ).strip().lower()

            if name_lower not in row_company and row_company not in name_lower:
                continue

            event_date = _parse_date(
                row.get("Date", "") or row.get("date", "")
            )
            if event_date is None or event_date < cutoff:
                continue

            events.append(
                LayoffInfo(
                    event_date=event_date,
                    headcount_cut=_parse_int(
                        row.get("Laid_Off_Count", "") or row.get("laid_off_count", "")
                    ),
                    percentage_cut=_parse_pct(
                        row.get("Percentage", "") or row.get("percentage", "")
                    ),
                    source_url=None,
                )
            )

    return events


def get_all_recent_layoffs(
    csv_path: str = _LAYOFFS_CSV,
    window_days: int = _WINDOW_DAYS,
) -> dict[str, list[LayoffInfo]]:
    """
    Load all layoffs within the window.
    Returns {company_name: [LayoffInfo, ...]}
    Used by the market-space map (Act V stretch).
    """
    if not os.path.exists(csv_path):
        return {}

    cutoff = date.today() - timedelta(days=window_days)
    result: dict[str, list[LayoffInfo]] = {}

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            company = (row.get("Company", "") or row.get("company", "")).strip()
            if not company:
                continue

            event_date = _parse_date(row.get("Date", "") or row.get("date", ""))
            if event_date is None or event_date < cutoff:
                continue

            info = LayoffInfo(
                event_date=event_date,
                headcount_cut=_parse_int(row.get("Laid_Off_Count", "")),
                percentage_cut=_parse_pct(row.get("Percentage", "")),
            )
            result.setdefault(company, []).append(info)

    return result
