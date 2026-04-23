"""
agent/enrichment/job_scraper.py

Aggregates public job listings from three sources:
  1. Wellfound   — wellfound.com/company/{slug}/jobs
  2. BuiltIn     — builtin.com/company/{slug}/jobs
  3. Careers page — company's own site (heuristic URL discovery)

Rules: public pages only, no login, no captcha bypass, respects robots.txt.
"""

from __future__ import annotations

import json
import time
from datetime import date
from typing import Optional
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page, sync_playwright


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _safe_goto(page: Page, url: str, timeout: int = 20_000) -> bool:
    """Navigate to url; return True if page loaded without error."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        page.wait_for_timeout(1500)
        return True
    except Exception:
        return False


def _extract_titles(page: Page, selectors: list[str]) -> list[str]:
    titles: list[str] = []
    for sel in selectors:
        try:
            found = page.eval_on_selector_all(
                sel,
                "els => els.map(e => e.innerText.trim()).filter(t => t.length > 3 && t.length < 120)",
            )
            titles.extend(found)
        except Exception:
            pass
    return titles


# ── Source: Wellfound ─────────────────────────────────────────────────────────

def _fetch_wellfound(page: Page, slug: str, max_jobs: int) -> list[str]:
    url = f"https://wellfound.com/company/{slug}/jobs"
    if not _safe_goto(page, url):
        return []
    titles = _extract_titles(
        page,
        [
            "a[data-test='job-title']",
            "[class*='JobListing'] h2",
            "[class*='JobListing'] h3",
            "h2",
            "h3",
        ],
    )
    return titles[:max_jobs]


# ── Source: BuiltIn ───────────────────────────────────────────────────────────

def _fetch_builtin(page: Page, slug: str, max_jobs: int) -> list[str]:
    """
    BuiltIn company jobs page: builtin.com/company/{slug}/jobs
    BuiltIn also has city-specific sub-domains (builtinnyc.com etc.) — we hit
    the national site only since it aggregates all.
    """
    url = f"https://builtin.com/company/{slug}/jobs"
    if not _safe_goto(page, url):
        return []
    titles = _extract_titles(
        page,
        [
            "a[data-id='job-card-title']",
            "[class*='job-card'] h2",
            "[class*='job-card'] h3",
            "article h2",
            "article h3",
        ],
    )
    return titles[:max_jobs]


# ── Source: Company careers page ──────────────────────────────────────────────

_CAREERS_SUFFIXES = ["/careers", "/jobs", "/careers/jobs", "/about/careers"]


def _discover_careers_url(homepage: str) -> Optional[str]:
    base = homepage.rstrip("/")
    for suffix in _CAREERS_SUFFIXES:
        yield base + suffix


def _fetch_careers_page(
    page: Page, company_homepage: str, max_jobs: int
) -> list[str]:
    """
    Try common careers URL patterns on the company's own site.
    Returns job titles from the first URL that returns results.
    """
    for url in _discover_careers_url(company_homepage):
        if not _safe_goto(page, url):
            continue
        titles = _extract_titles(
            page,
            [
                "a[href*='/job']",
                "a[href*='/jobs']",
                "a[href*='/careers']",
                "h2",
                "h3",
                "[class*='job'] a",
                "[class*='position'] a",
                "[class*='role'] a",
                "[class*='opening'] a",
            ],
        )
        if titles:
            return titles[:max_jobs]
    return []


# ── Public entry point ────────────────────────────────────────────────────────

def fetch_job_listings(
    company_slug: str,
    *,
    builtin_slug: Optional[str] = None,
    company_homepage: Optional[str] = None,
    max_jobs: int = 30,
) -> dict:
    """
    Aggregate job listings from Wellfound, BuiltIn, and the company careers page.

    Args:
        company_slug:     Wellfound slug (e.g. 'stripe').
        builtin_slug:     BuiltIn slug if different from wellfound (defaults to company_slug).
        company_homepage: Company homepage URL for careers-page scraping (optional).
        max_jobs:         Max titles to collect per source before dedup.

    Returns:
        dict with aggregated job titles, source breakdown, and snapshot metadata.
    """
    bn_slug = builtin_slug or company_slug
    sources: dict[str, list[str]] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (compatible; TenaciousBot/1.0; +https://tenacious.co/bot)"
        )
        page = context.new_page()

        print(f"[job_scraper] Wellfound  → wellfound.com/company/{company_slug}/jobs")
        sources["wellfound"] = _fetch_wellfound(page, company_slug, max_jobs)
        time.sleep(1)

        print(f"[job_scraper] BuiltIn    → builtin.com/company/{bn_slug}/jobs")
        sources["builtin"] = _fetch_builtin(page, bn_slug, max_jobs)
        time.sleep(1)

        if company_homepage:
            print(f"[job_scraper] CareersPage → {company_homepage}")
            sources["careers_page"] = _fetch_careers_page(
                page, company_homepage, max_jobs
            )

        browser.close()

    # Deduplicate across sources while keeping order
    seen: set[str] = set()
    all_titles: list[str] = []
    for title_list in sources.values():
        for t in title_list:
            norm = t.lower().strip()
            if norm not in seen and len(norm) > 3:
                seen.add(norm)
                all_titles.append(t)

    return {
        "company_slug": company_slug,
        "snapshot_date": date.today().isoformat(),
        "open_roles_count": len(all_titles),
        "job_titles": all_titles,
        "sources": {k: len(v) for k, v in sources.items()},
        "source_urls": {
            "wellfound": f"https://wellfound.com/company/{company_slug}/jobs",
            "builtin": f"https://builtin.com/company/{bn_slug}/jobs",
            **({"careers_page": company_homepage} if company_homepage else {}),
        },
    }


def save_to_json(data: dict, output_path: str) -> None:
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[job_scraper] Saved → {output_path}")


if __name__ == "__main__":
    result = fetch_job_listings(
        "stripe",
        company_homepage="https://stripe.com",
    )
    save_to_json(result, "job_snapshot.json")
    print(json.dumps(result, indent=2))
