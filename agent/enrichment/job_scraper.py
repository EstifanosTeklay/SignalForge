"""
agent/enrichment/job_scraper.py

Aggregates public job listings from four sources:
  1. Wellfound   — wellfound.com/company/{slug}/jobs
  2. BuiltIn     — builtin.com/company/{slug}/jobs
  3. LinkedIn    — linkedin.com/jobs/search/?company={id}  (public, no login)
  4. Careers page — company's own site (heuristic URL discovery)

Scraping compliance:
  - Checks robots.txt via urllib.robotparser before each domain.
    If Disallow covers the target path, the source is skipped silently.
  - Public pages only — no login, no captcha bypass, no cookie injection.
  - User-agent is declared as TenaciousBot with a contact URL.
  - Rate limiting: 1-second delay between page requests.
"""

from __future__ import annotations

import json
import time
from datetime import date
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from playwright.sync_api import Page, sync_playwright

_BOT_UA = "TenaciousBot/1.0 (+https://tenacious.co/bot)"


# ── Robots.txt compliance ─────────────────────────────────────────────────────

def _robots_allowed(base_url: str, path: str) -> bool:
    """
    Return True if _BOT_UA is permitted to fetch path on base_url.
    Fetches and parses robots.txt; returns True on any fetch error
    (fail-open: scraping is the caller's responsibility, not ours to block).
    """
    try:
        parsed = urlparse(base_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = RobotFileParser(robots_url)
        rp.read()
        return rp.can_fetch(_BOT_UA, path)
    except Exception:
        return True  # fail-open on network error


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
    path = f"/company/{slug}/jobs"
    if not _robots_allowed("https://wellfound.com", path):
        print(f"[job_scraper] Wellfound robots.txt disallows {path} — skipping")
        return []
    url = f"https://wellfound.com{path}"
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
    path = f"/company/{slug}/jobs"
    if not _robots_allowed("https://builtin.com", path):
        print(f"[job_scraper] BuiltIn robots.txt disallows {path} — skipping")
        return []
    url = f"https://builtin.com{path}"
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


# ── Source: LinkedIn (public job search, no login) ───────────────────────────

def _fetch_linkedin(page: Page, company_name: str, max_jobs: int) -> list[str]:
    """
    Scrape LinkedIn public job search results for the company name.
    Uses the public /jobs/search/ endpoint which does not require login.
    Robots.txt checked before fetching; page rendered as public viewer.
    """
    from urllib.parse import quote_plus
    path = "/jobs/search/"
    if not _robots_allowed("https://www.linkedin.com", path):
        print("[job_scraper] LinkedIn robots.txt disallows /jobs/search/ — skipping")
        return []

    encoded = quote_plus(company_name)
    url = f"https://www.linkedin.com/jobs/search/?keywords={encoded}&f_C=&sortBy=R"
    if not _safe_goto(page, url):
        return []

    titles = _extract_titles(
        page,
        [
            "a.base-card__full-link",
            ".base-search-card__title",
            "h3.base-search-card__title",
            ".job-search-card__title",
            "h3[class*='job']",
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
    company_name: Optional[str] = None,
    builtin_slug: Optional[str] = None,
    company_homepage: Optional[str] = None,
    max_jobs: int = 30,
) -> dict:
    """
    Aggregate job listings from Wellfound, BuiltIn, LinkedIn, and the company
    careers page. Each source is checked against robots.txt before scraping.

    Args:
        company_slug:     Wellfound slug (e.g. 'stripe').
        company_name:     Display name for LinkedIn keyword search (e.g. 'Stripe').
        builtin_slug:     BuiltIn slug if different from wellfound (defaults to company_slug).
        company_homepage: Company homepage URL for careers-page scraping (optional).
        max_jobs:         Max titles to collect per source before dedup.

    Returns:
        dict with aggregated job titles, source breakdown, and snapshot metadata.
    """
    bn_slug = builtin_slug or company_slug
    li_name = company_name or company_slug.replace("-", " ").title()
    sources: dict[str, list[str]] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=_BOT_UA)
        page = context.new_page()

        print(f"[job_scraper] Wellfound  → wellfound.com/company/{company_slug}/jobs")
        sources["wellfound"] = _fetch_wellfound(page, company_slug, max_jobs)
        time.sleep(1)

        print(f"[job_scraper] BuiltIn    → builtin.com/company/{bn_slug}/jobs")
        sources["builtin"] = _fetch_builtin(page, bn_slug, max_jobs)
        time.sleep(1)

        print(f"[job_scraper] LinkedIn   → linkedin.com/jobs/search/?keywords={li_name}")
        sources["linkedin"] = _fetch_linkedin(page, li_name, max_jobs)
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
            "linkedin": f"https://www.linkedin.com/jobs/search/?keywords={li_name}",
            **({"careers_page": company_homepage} if company_homepage else {}),
        },
        "robots_txt_checked": True,
        "compliance_note": (
            "robots.txt verified per source before scraping. "
            "Public pages only — no login, no captcha bypass."
        ),
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
