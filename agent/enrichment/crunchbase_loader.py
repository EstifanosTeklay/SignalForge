"""
agent/enrichment/crunchbase_loader.py

Loads companies from the Crunchbase ODM CSV.
Expected path: data/crunchbase_data.csv  (set via CRUNCHBASE_CSV env var)

Column mapping based on actual ODM export format:
  name, uuid, url, about, industries (JSON), num_employees, country_code,
  location (JSON), funding_rounds_list (JSON), funds_total (JSON),
  builtwith_tech (JSON), leadership_hire (JSON)
"""

from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime
from typing import Optional

from agent.models.company import Company, FundingInfo, LeadershipChange

_CRUNCHBASE_CSV = os.getenv("CRUNCHBASE_CSV", "data/crunchbase_data.csv")

_BAND_NORMALIZE = {
    "1-10": "1-10", "11-50": "11-50", "51-100": "51-200",
    "51-200": "51-200", "101-250": "51-200", "201-500": "201-500",
    "251-500": "201-500", "501-1000": "501-1000", "1001-5000": "1001-5000",
    "5001-10000": "5001-10000", "10001+": "10001+",
}


def _safe_json(s: str) -> object:
    if not s or s.strip() in ("", "null", "[]", "{}"):
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _parse_date(s: str) -> Optional[date]:
    if not s or s.strip() in ("", "None", "null"):
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip()[:19], fmt[:len(s.strip()[:19])]).date()
        except ValueError:
            continue
    return None


def _extract_industry(industries_json: str) -> tuple[Optional[str], Optional[str]]:
    """Parse industries JSON array → (primary, secondary)."""
    data = _safe_json(industries_json)
    if not isinstance(data, list) or not data:
        return None, None
    primary = data[0].get("value") if isinstance(data[0], dict) else str(data[0])
    secondary = (
        data[1].get("value") if len(data) > 1 and isinstance(data[1], dict) else None
    )
    return primary, secondary


def _extract_city(location_json: str) -> Optional[str]:
    """Parse location JSON array → city name."""
    data = _safe_json(location_json)
    if not isinstance(data, list) or not data:
        return None
    # First entry is usually the most specific (city), last is country
    entry = data[0] if isinstance(data[0], dict) else {}
    return entry.get("name")


def _extract_funding(funding_rounds_list: str) -> tuple[list[FundingInfo], Optional[FundingInfo]]:
    """Parse funding_rounds_list JSON → (list, latest)."""
    data = _safe_json(funding_rounds_list)
    if not isinstance(data, list) or not data:
        return [], None

    rounds: list[FundingInfo] = []
    for r in data:
        if not isinstance(r, dict):
            continue
        amount_data = r.get("raised_usd") or r.get("money_raised") or {}
        amount = (
            amount_data.get("value_usd") or amount_data.get("value")
            if isinstance(amount_data, dict) else amount_data
        )
        announced = _parse_date(str(r.get("announced_on") or r.get("date") or ""))
        rounds.append(FundingInfo(
            round_type=r.get("funding_type") or r.get("round_type") or "Unknown",
            amount_usd=int(amount) if amount else None,
            announced_date=announced,
            source_url=r.get("cb_url") or r.get("url"),
        ))

    if not rounds:
        return [], None

    rounds.sort(key=lambda x: x.announced_date or date.min, reverse=True)
    return rounds, rounds[0]


def _extract_tech_stack(builtwith_tech: str) -> list[str]:
    """Parse builtwith_tech JSON array → list of tech names."""
    data = _safe_json(builtwith_tech)
    if not isinstance(data, list):
        return []
    return [
        item.get("name", "") for item in data
        if isinstance(item, dict) and item.get("name")
    ]


def _extract_leadership(leadership_hire: str) -> list[LeadershipChange]:
    """Parse leadership_hire JSON array → LeadershipChange list."""
    data = _safe_json(leadership_hire)
    if not isinstance(data, list):
        return []

    changes: list[LeadershipChange] = []
    exec_roles = {"cto", "vp engineering", "vp of engineering", "chief technology",
                  "head of engineering", "chief scientist", "vp data", "head of ai"}

    for item in data:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or item.get("role") or "").lower()
        if any(role in title for role in exec_roles):
            changes.append(LeadershipChange(
                role=item.get("title") or item.get("role") or "Executive",
                name=item.get("name"),
                announced_date=_parse_date(str(item.get("date") or item.get("started_on") or "")),
                source_url=item.get("url"),
            ))
    return changes


def _row_to_company(row: dict) -> Company:
    industry, sub_industry = _extract_industry(row.get("industries", ""))
    city = _extract_city(row.get("location", ""))
    funding_rounds, latest_funding = _extract_funding(row.get("funding_rounds_list", ""))
    tech_stack = _extract_tech_stack(row.get("builtwith_tech", ""))
    leadership = _extract_leadership(row.get("leadership_hire", ""))

    # funds_total is a JSON like {"value_usd": 5000000, "currency": "USD"}
    funds_data = _safe_json(row.get("funds_total", ""))
    total_usd = None
    if isinstance(funds_data, dict):
        total_usd = funds_data.get("value_usd") or funds_data.get("value")

    band = row.get("num_employees", "").strip()
    band = _BAND_NORMALIZE.get(band, band or None)

    # AI-related public mentions from 'about' field
    about = row.get("about", "") or row.get("full_description", "") or ""
    ai_mentions = [about] if about and any(
        kw in about.lower() for kw in ["ai", "machine learning", "ml", "artificial intelligence"]
    ) else []

    return Company(
        name=row.get("name", "").strip(),
        crunchbase_uuid=row.get("uuid"),
        website=row.get("website"),
        linkedin_url=None,  # not in this ODM export
        industry=industry,
        sub_industry=sub_industry,
        hq_country=row.get("country_code"),
        hq_city=city,
        employee_count_band=band,
        funding_rounds=funding_rounds,
        latest_funding=latest_funding,
        total_funding_usd=int(total_usd) if total_usd else None,
        detected_technologies=tech_stack,
        leadership_changes=leadership,
        public_ai_mentions=ai_mentions,
        data_fetched_at=date.today(),
    )


def load_all_companies(csv_path: str = _CRUNCHBASE_CSV) -> list[Company]:
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Crunchbase CSV not found at {csv_path}")

    companies: list[Company] = []
    with open(csv_path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                companies.append(_row_to_company(row))
            except Exception:
                continue
    return companies


def load_company_by_name(name: str, csv_path: str = _CRUNCHBASE_CSV) -> Optional[Company]:
    if not os.path.exists(csv_path):
        return None

    name_lower = name.lower().strip()
    best: Optional[Company] = None

    with open(csv_path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_name = row.get("name", "").strip().lower()
            if row_name == name_lower:
                return _row_to_company(row)
            if not best and name_lower in row_name:
                best = _row_to_company(row)

    return best


def load_companies_by_industry(
    industry: str,
    csv_path: str = _CRUNCHBASE_CSV,
    limit: int = 50,
) -> list[Company]:
    if not os.path.exists(csv_path):
        return []

    industry_lower = industry.lower()
    results: list[Company] = []

    with open(csv_path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            industries_raw = row.get("industries", "").lower()
            if industry_lower in industries_raw:
                try:
                    results.append(_row_to_company(row))
                except Exception:
                    continue
            if len(results) >= limit:
                break

    return results
