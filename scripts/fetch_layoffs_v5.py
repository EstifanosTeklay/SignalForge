"""
scripts/fetch_layoffs_v5.py

Fetches all layoffs.fyi data by intercepting Airtable's readSharedViewData
API call and forcing a JSON response (instead of msgpack) via route rewriting.

Working approach:
  1. Load layoffs.fyi in Playwright
  2. Intercept the readSharedViewData request before it leaves
  3. Rewrite the URL to strip allowMsgpackOfResult=true and remove the
     x-airtable-accept-msgpack header so Airtable returns plain JSON
  4. Parse data.table.rows + column choice maps to build clean CSV rows

Run: python scripts/fetch_layoffs_v5.py
Output: data/layoffs.csv
"""

import csv
import json
import os
import urllib.parse

OUTPUT_PATH = "data/layoffs.csv"
FIELDNAMES = [
    "Company", "Location_HQ", "Industry", "Laid_Off_Count",
    "Percentage", "Date", "Source", "Country", "Stage", "Funds_Raised_USD"
]

TARGET_VIEW_ID = "viwN3RMGptp84mfag"


def scrape() -> list[dict]:
    from playwright.sync_api import sync_playwright

    captured = []

    def handle_route(route):
        url = route.request.url
        if TARGET_VIEW_ID in url and "readSharedViewData" in url:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            if "stringifiedObjectParams" in qs:
                obj_params = json.loads(qs["stringifiedObjectParams"][0])
                obj_params["allowMsgpackOfResult"] = False
                qs["stringifiedObjectParams"] = [json.dumps(obj_params)]
            new_query = urllib.parse.urlencode({k: v[0] for k, v in qs.items()})
            new_url = parsed._replace(query=new_query).geturl()
            headers = dict(route.request.headers)
            headers.pop("x-airtable-accept-msgpack", None)
            print(f"  [ROUTE] rewritten -> JSON response requested")
            route.continue_(url=new_url, headers=headers)
        else:
            route.continue_()

    def on_response(response):
        url = response.url
        if TARGET_VIEW_ID in url and "readSharedViewData" in url:
            try:
                body = response.json()
                captured.append(body)
                rows_count = len((body.get("data") or {}).get("table", {}).get("rows", []))
                print(f"  [CAPTURED] JSON response, {rows_count} rows")
            except Exception as exc:
                print(f"  [CAPTURE FAIL] {exc}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.route("**", handle_route)
        page.on("response", on_response)

        print("Loading layoffs.fyi ...")
        try:
            page.goto("https://layoffs.fyi", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(12000)
        except Exception as exc:
            print(f"  Load note: {exc}")

        os.makedirs("outputs", exist_ok=True)
        page.screenshot(path="outputs/layoffs_v5_debug.png")
        browser.close()

    rows = []
    for payload in captured:
        rows.extend(_parse_payload(payload))
    return rows


def _parse_payload(body: dict) -> list[dict]:
    table = (body.get("data") or {}).get("table") or {}
    col_list = table.get("columns") or []
    row_list = table.get("rows") or []

    col_name = {c["id"]: c["name"] for c in col_list if isinstance(c, dict)}
    choice_maps = {}
    for c in col_list:
        if not isinstance(c, dict):
            continue
        opts = c.get("typeOptions") or {}
        choices_raw = opts.get("choices") or {}
        if choices_raw:
            choice_maps[c["id"]] = {k: v.get("name", k) for k, v in choices_raw.items()}

    # name -> col_id lookup
    name_to_id = {v: k for k, v in col_name.items()}

    def col(field_name):
        return name_to_id.get(field_name, "")

    def decode(col_id, val):
        if val is None:
            return ""
        if col_id in choice_maps:
            cmap = choice_maps[col_id]
            if isinstance(val, list):
                return ", ".join(cmap.get(v, v) for v in val)
            return cmap.get(val, str(val))
        if isinstance(val, str) and "T" in val and val.endswith("Z"):
            return val[:10]
        if isinstance(val, float) and val == int(val):
            return str(int(val))
        return str(val)

    print(f"  Columns: {[c['name'] for c in col_list]}")

    rows = []
    for row in row_list:
        cells = row.get("cellValuesByColumnId") or {}
        company = decode(col("Company"), cells.get(col("Company")))
        if not company:
            continue
        rows.append({
            "Company": company,
            "Location_HQ": decode(col("Location HQ"), cells.get(col("Location HQ"))),
            "Industry": decode(col("Industry"), cells.get(col("Industry"))),
            "Laid_Off_Count": decode(col("# Laid Off"), cells.get(col("# Laid Off"))),
            "Percentage": decode(col("%"), cells.get(col("%"))),
            "Date": decode(col("Date"), cells.get(col("Date"))),
            "Source": decode(col("Source"), cells.get(col("Source"))),
            "Country": decode(col("Country"), cells.get(col("Country"))),
            "Stage": decode(col("Stage"), cells.get(col("Stage"))),
            "Funds_Raised_USD": decode(col("$ Raised (mm)"), cells.get(col("$ Raised (mm)"))),
        })
    return rows


def save_csv(rows: list[dict], path: str) -> None:
    seen = set()
    unique = []
    for r in rows:
        k = (r.get("Company", "").lower().strip(), r.get("Date", "")[:7])
        if k[0] and k not in seen:
            seen.add(k)
            unique.append(r)
    unique.sort(key=lambda r: r.get("Date", ""), reverse=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(unique)
    print(f"Saved {len(unique)} records to {path}")


if __name__ == "__main__":
    print("=== layoffs.fyi scraper v5 (route rewrite -> JSON) ===")
    rows = scrape()
    print(f"\nTotal records captured: {len(rows)}")

    if len(rows) >= 20:
        save_csv(rows, OUTPUT_PATH)
        print("\nSample:")
        for r in rows[:5]:
            print(f"  {r['Company']:25} | {r['Date']:12} | {r.get('Laid_Off_Count','?'):6} | {r.get('Industry','')}")
    else:
        print(f"Only {len(rows)} rows captured -- keeping existing layoffs.csv")
