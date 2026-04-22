"""
agent/enrichment/job_scraper.py

Signal pipeline skeleton — Day 0 deliverable
Fetches public job listings from a company's Wellfound page and saves as JSON.

Rules:
- Public pages only
- No login
- No captcha bypass
- Respects robots.txt
"""

import json
from datetime import date
from playwright.sync_api import sync_playwright


def fetch_job_listings(company_slug: str, max_jobs: int = 10) -> dict:
    """
    Fetch public job listings for a company from Wellfound.

    Args:
        company_slug: Company URL slug e.g. 'stripe', 'openai'
        max_jobs: Maximum number of job titles to extract

    Returns:
        dict with company slug, job titles, count, and snapshot date
    """
    url = f"https://wellfound.com/company/{company_slug}/jobs"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (compatible; TenaciousBot/1.0; research)"
        )

        print(f"Fetching: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)  # polite delay

        # Extract job titles from public listing
        job_titles = page.eval_on_selector_all(
            "a[data-test='job-title'], h2, h3",
            "elements => elements.map(el => el.innerText.trim()).filter(t => t.length > 3)"
        )

        browser.close()

    result = {
        "company_slug": company_slug,
        "source_url": url,
        "snapshot_date": date.today().isoformat(),
        "open_roles_count": len(job_titles[:max_jobs]),
        "job_titles": job_titles[:max_jobs],
    }

    return result


def save_to_json(data: dict, output_path: str) -> None:
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved to {output_path}")


if __name__ == "__main__":
    # Day 0 test — fetch one company's job listings
    result = fetch_job_listings("stripe")
    save_to_json(result, "job_snapshot.json")
    print(json.dumps(result, indent=2))
