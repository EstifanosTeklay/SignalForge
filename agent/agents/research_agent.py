"""
agent/agents/research_agent.py

🧠 Agent 1 — Research / Signal Agent

Role:
  - Accepts a company name (and optional Crunchbase UUID / website)
  - Pulls raw data from all public sources
  - Runs deterministic signal computation
  - Returns: Company record + HiringSignalBrief

This agent does NOT call an LLM. All outputs are deterministically
derived from public data so they are traceable to a source.

Data sources hit in order:
  1. Crunchbase ODM CSV (firmographics + funding)
  2. layoffs.fyi CSV (layoff events)
  3. Wellfound job scraper (open roles, AI-adjacent titles)
  4. Company.public_ai_mentions — populated from press / Crunchbase description
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import date
from typing import Optional

from agent.enrichment.crunchbase_loader import load_company_by_name
from agent.enrichment.job_scraper import fetch_job_listings
from agent.enrichment.layoffs_parser import get_layoff_events
from agent.enrichment.signal_computer import compute_signals
from agent.models.company import Company
from agent.models.signals import HiringSignalBrief
from agent.observability import traced


class ResearchAgent:
    """
    Orchestrates data collection for a single prospect company.
    Designed to be called once per prospect before any outreach.
    """

    def __init__(self, use_job_scraper: bool = True):
        # Set False in tests or when using frozen job-post snapshots
        self.use_job_scraper = use_job_scraper

    @traced("research_agent.run")
    def run(
        self,
        company_name: str,
        wellfound_slug: Optional[str] = None,
        prior_job_snapshot: Optional[dict] = None,
    ) -> tuple[Company, HiringSignalBrief]:
        """
        Full enrichment pipeline for one company.

        Args:
            company_name:       Human-readable company name (used for CSV lookup)
            wellfound_slug:     Optional Wellfound URL slug for job scraping
            prior_job_snapshot: Pre-scraped snapshot dict (skips live scrape)

        Returns:
            (Company, HiringSignalBrief) — raw record + derived signals
        """
        t0 = time.perf_counter()

        # ── Step 1: Crunchbase firmographics ─────────────────────────────────
        company = load_company_by_name(company_name)
        if company is None:
            # Fallback: create a minimal stub so the rest of the pipeline works
            company = Company(name=company_name, data_fetched_at=date.today())
            print(
                f"[research_agent] '{company_name}' not found in Crunchbase ODM. "
                "Proceeding with stub record."
            )

        # ── Step 2: Layoff events ─────────────────────────────────────────────
        layoffs = get_layoff_events(company_name)
        company.layoff_events = layoffs

        # ── Step 3: Job posts ──────────────────────────────────────────────────
        if prior_job_snapshot:
            snapshot = prior_job_snapshot
        elif self.use_job_scraper and wellfound_slug:
            try:
                snapshot = fetch_job_listings(wellfound_slug)
            except Exception as exc:
                print(f"[research_agent] Job scrape failed for {wellfound_slug}: {exc}")
                snapshot = {}
        else:
            snapshot = {}

        if snapshot:
            company.open_roles_count = snapshot.get("open_roles_count", 0)
            company.open_role_titles = snapshot.get("job_titles", [])
            date_str = snapshot.get("snapshot_date")
            if date_str:
                try:
                    company.open_roles_snapshot_date = date.fromisoformat(date_str)
                except ValueError:
                    pass

        # ── Step 4: Compute signals ───────────────────────────────────────────
        brief = compute_signals(company)

        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        print(
            f"[research_agent] '{company_name}' enriched in {elapsed_ms}ms | "
            f"segment={brief.icp.segment.value} | "
            f"ai_maturity={brief.ai_maturity.score}/3 | "
            f"confidence={brief.icp.confidence.value}"
        )

        return company, brief

    def to_json(self, brief: HiringSignalBrief, indent: int = 2) -> str:
        """Serialize HiringSignalBrief to JSON string for file output."""

        def _default(obj):
            if isinstance(obj, date):
                return obj.isoformat()
            if hasattr(obj, "value"):  # Enum
                return obj.value
            return str(obj)

        return json.dumps(asdict(brief), indent=indent, default=_default)

    def save_brief(self, brief: HiringSignalBrief, path: str) -> None:
        """Write hiring_signal_brief.json to disk."""
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json(brief))
        print(f"[research_agent] Brief saved to {path}")
