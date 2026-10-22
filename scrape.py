import json
import os
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

URL = "https://thesummitleague.org/standings.aspx?path=wvball"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "site", "data.json")

def scrape_standings():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print(f"Fetching: {URL}")
        page.goto(URL, wait_until="networkidle", timeout=60000)

        # Wait for a table to appear
        try:
            page.wait_for_selector("table", timeout=20000)
        except Exception:
            print("WARNING: No table found within timeout — page may not have loaded correctly.")

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")

    if not tables:
        print("ERROR: No tables found on page.")
        return None

    all_tables = []

    for table in tables:
        headers = []
        rows = []

        # Extract headers
        header_row = table.find("thead")
        if header_row:
            headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]

        # Extract rows
        tbody = table.find("tbody")
        if tbody:
            for tr in tbody.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if any(cells):  # skip empty rows
                    rows.append(cells)

        if headers or rows:
            all_tables.append({
                "headers": headers,
                "rows": rows
            })

    return all_tables


def main():
    print("Starting scrape...")
    tables = scrape_standings()

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if tables:
        payload = {
            "status": "ok",
            "last_updated": timestamp,
            "tables": tables
        }
        print(f"Successfully scraped {len(tables)} table(s).")
    else:
        # Write a graceful error state so the site shows a helpful message
        payload = {
            "status": "error",
            "last_updated": timestamp,
            "tables": []
        }
        print("Scrape failed — writing error state to data.json.")

    # Ensure site/ directory exists
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"data.json written to: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
