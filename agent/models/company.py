"""
agent/models/company.py

Raw company data exactly as sourced — no inference, no scoring.
Every field maps to a specific data source so the evidence graph stays clean.

Sources:
  - Crunchbase ODM sample  → firmographics, funding
  - layoffs.fyi CSV        → layoff events
  - Job post scrape        → open_roles_count, open_roles_snapshot_date
  - Press / Crunchbase     → leadership_changes
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class FundingInfo:
    """
    A single funding round as reported by Crunchbase.
    Multiple rounds may exist; store the most recent separately
    in Company.latest_funding for fast signal access.
    """
    round_type: str           # e.g. "Series A", "Series B", "Seed"
    amount_usd: Optional[int] # None if undisclosed
    announced_date: Optional[date]
    source_url: Optional[str] = None  # Crunchbase permalink or press link


@dataclass
class LayoffInfo:
    """
    A single layoff event as reported by layoffs.fyi.
    """
    event_date: date
    headcount_cut: Optional[int]       # absolute number if reported
    percentage_cut: Optional[float]    # 0.0–1.0 if reported
    source_url: Optional[str] = None


@dataclass
class LeadershipChange:
    """
    A detected CTO / VP Engineering appointment.
    Source: Crunchbase people data or press release.
    """
    role: str              # e.g. "CTO", "VP Engineering", "Head of Engineering"
    name: Optional[str]    # may be omitted if we only have the signal, not the name
    announced_date: Optional[date]
    source_url: Optional[str] = None


@dataclass
class Company:
    """
    Raw company record as ingested from public sources.
    No inferred signals or scores live here.

    Serialises to / from the Crunchbase ODM field names where possible.
    """

    # ── Identity ────────────────────────────────────────────────────────────
    name: str
    crunchbase_uuid: Optional[str] = None   # Crunchbase ODM primary key
    website: Optional[str] = None
    linkedin_url: Optional[str] = None

    # ── Firmographics ────────────────────────────────────────────────────────
    industry: Optional[str] = None          # Crunchbase category_list top entry
    sub_industry: Optional[str] = None      # second category if present
    hq_country: Optional[str] = None
    hq_city: Optional[str] = None
    employee_count_band: Optional[str] = None  # e.g. "11-50", "51-200"; Crunchbase band
    employee_count_exact: Optional[int] = None # if a point estimate is available

    # ── Funding ──────────────────────────────────────────────────────────────
    funding_rounds: list[FundingInfo] = field(default_factory=list)
    # Convenience shortcut — populated by the ingestion layer, not inferred
    latest_funding: Optional[FundingInfo] = None
    total_funding_usd: Optional[int] = None

    # ── Layoffs ───────────────────────────────────────────────────────────────
    layoff_events: list[LayoffInfo] = field(default_factory=list)

    # ── Job Posts ─────────────────────────────────────────────────────────────
    open_roles_count: Optional[int] = None
    open_roles_snapshot_date: Optional[date] = None
    # Raw role titles — used by signal layer for AI-maturity scoring
    open_role_titles: list[str] = field(default_factory=list)

    # ── Tech Stack ────────────────────────────────────────────────────────────
    # BuiltWith / Wappalyzer detected technologies (raw strings)
    detected_technologies: list[str] = field(default_factory=list)

    # ── Leadership ────────────────────────────────────────────────────────────
    leadership_changes: list[LeadershipChange] = field(default_factory=list)

    # ── Public AI Signals (raw text, not scored) ──────────────────────────────
    # Exec blog posts, keynotes, press releases mentioning AI — raw excerpts/URLs
    public_ai_mentions: list[str] = field(default_factory=list)
    github_org_url: Optional[str] = None   # for repo-level AI activity check later

    # ── Provenance ────────────────────────────────────────────────────────────
    data_fetched_at: Optional[date] = None  # when this record was last refreshed
