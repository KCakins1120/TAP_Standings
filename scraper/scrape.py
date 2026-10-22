"""
scraper/scrape.py
Fetches the Summit League 2026 Women's Volleyball Standings
and writes site/data.json.
"""

import json
import os
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

SOURCE_URL = os.environ.get(
    "STANDINGS_URL",
    "https://thesummitleague.org/standings.aspx?path=wvball",
)

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "site", "data.json")


def parse_standings(html: str):
    soup = BeautifulSoup(html, "html.parser")

    # The standings live inside a <table> — grab the first one on the page
    table = soup.find("table")
    if table is None:
        raise ValueError("No <table> found on the page.")

    headers = [th.get_text(strip=True) for th in table.find_all("th")]

    rows = []
    for tr in table.find("tbody").find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if cells:
            rows.append(cells)

    if not rows:
        raise ValueError("Table found but contained no data rows.")

    return headers, rows


def main():
    existing = {}
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH) as f:
            existing = json.load(f)

    result = dict(existing)  # preserve any other sections

    print(f"[standings] fetching {SOURCE_URL}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(SOURCE_URL, wait_until="networkidle", timeout=30_000)
            # Wait until at least one table row is visible
            page.wait_for_selector("table tbody tr", timeout=15_000)
            html = page.content()
            browser.close()

        headers, rows = parse_standings(html)
        print(f"[standings] updated ({len(rows)} rows)")

        result["standings"] = {
            "status": "ok",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source": SOURCE_URL,
            "headers": headers,
            "rows": rows,
        }

    except Exception as exc:
        print(f"FAILED [standings]: {exc}")
        if "standings" in result:
            result["standings"]["status"] = "stale"
        else:
            result["standings"] = {
                "status": "stale",
                "updated_at": None,
                "source": SOURCE_URL,
                "headers": [],
                "rows": [],
            }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(result, f, indent=2)
    print(f"[standings] wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
