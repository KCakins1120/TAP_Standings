#!/usr/bin/env python3
"""
Fetch standings from multiple Summit League, PFL, and NCHC sports pages,
extracting Team, Conference record (W-L), and Overall record (W-L) for each.
Writes structured output to docs/data.json.

Safe by design: if any individual source fails, the last good data for that sport
stays in the file marked "stale", so the website never goes blank.

Usage:
    python scraper/scrape.py
    python scraper/scrape.py --out docs/data.json
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ── Target Sports Configuration ────────────────────────────────────────────────
SPORTS_CONFIG = [
    {
        "id": "wvball",
        "name": "Summit League Women's Volleyball",
        "url": "https://thesummitleague.org/standings.aspx?path=wvball",
    },
    {
        "id": "msoc",
        "name": "Summit League Men's Soccer",
        "url": "https://thesummitleague.org/standings.aspx?path=msoc",
    },
    {
        "id": "wsoc",
        "name": "Summit League Women's Soccer",
        "url": "https://thesummitleague.org/standings.aspx?path=wsoc",
    },
    {
        "id": "pfl",
        "name": "Pioneer Football League",
        "url": "https://pioneer-football.org/standings.aspx?standings=19",
    },
    {
        "id": "mbball",
        "name": "Summit League Men's Basketball",
        "url": "https://thesummitleague.org/standings.aspx?path=mbball",
    },
    {
        "id": "wbball",
        "name": "Summit League Women's Basketball",
        "url": "https://thesummitleague.org/standings.aspx?path=wbball",
    },
    {
        "id": "mhockey",
        "name": "NCHC Men's Ice Hockey",
        "url": "https://nchchockey.com/standings.aspx?path=mhockey",
    },
    {
        "id": "summit_195",
        "name": "Summit League Baseball / Standings 195",
        "url": "https://thesummitleague.org/standings.aspx?standings=195",
    },
    {
        "id": "summit_193",
        "name": "Summit League Softball / Standings 193",
        "url": "https://thesummitleague.org/standings.aspx?standings=193",
    },
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()

def header_cells(table):
    row = table.select_one("thead tr:last-of-type") or table.find("tr")
    return [clean(c.get_text()) for c in row.find_all(["th", "td"])] if row else []

def body_rows(table):
    rows = table.select("tbody tr") or table.find_all("tr")[1:]
    return [r for r in rows if r.find_all(["td", "th"])]

def team_text(cell):
    link = cell.find("a")
    return clean((link or cell).get_text())


# ── Generic Sidearm Standings Table Parser ────────────────────────────────────

def parse_standings_table(html, sport_meta):
    soup = BeautifulSoup(html, "html.parser")

    target = None
    for table in soup.find_all("table"):
        hdrs = header_cells(table)
        hdrs_lower = [h.lower() for h in hdrs]
        if any("team" in h or "school" in h or "member" in h for h in hdrs_lower):
            target = table
            break

    if target is None:
        raise ValueError(f"Standings table not found for {sport_meta['name']}")

    headers = header_cells(target)
    hdrs_lower = [h.lower() for h in headers]

    def idx(*candidates):
        for c in candidates:
            for i, h in enumerate(hdrs_lower):
                if c == h or c in h:
                    return i
        return None

    team_idx = idx("team", "school", "member", "institution", "name")

    # Conference record detection (combined vs split)
    conf_combined = idx("conf record", "conference record", "league", "conf")
    conf_w_idx = idx("conf w", "cw", "conference w", "w-conf") if conf_combined is None else None
    conf_l_idx = idx("conf l", "cl", "conference l", "l-conf") if conf_combined is None else None

    # Overall record detection (combined vs split)
    ovr_combined = idx("overall record", "ovr record", "total record", "overall", "ovr")
    ovr_w_idx = idx("overall w", "ow", "ovr w", "w-overall", "w-ovr") if ovr_combined is None else None
    ovr_l_idx = idx("overall l", "ol", "ovr l", "l-overall", "l-ovr") if ovr_combined is None else None

    rows = []
    for tr in body_rows(target):
        cells = tr.find_all(["td", "th"])

        def cell_text(i):
            if i is None or i >= len(cells):
                return ""
            return clean(cells[i].get_text())

        team = team_text(cells[team_idx]) if team_idx is not None else ""
        if not team or team.lower() in ("team", "school", "member"):
            continue

        # Extract Conf record
        if conf_combined is not None:
            conf = cell_text(conf_combined).replace("–", "-") or "0-0"
        elif conf_w_idx is not None and conf_l_idx is not None:
            conf = f"{cell_text(conf_w_idx)}-{cell_text(conf_l_idx)}"
        else:
            conf = "—"

        # Extract Overall record
        if ovr_combined is not None:
            ovr = cell_text(ovr_combined).replace("–", "-") or "0-0"
        elif ovr_w_idx is not None and ovr_l_idx is not None:
            ovr = f"{cell_text(ovr_w_idx)}-{cell_text(ovr_l_idx)}"
        else:
            ovr = "—"

        rows.append({
            "rank": len(rows) + 1,
            "team": team,
            "conf": conf,
            "ovr": ovr,
        })

    if not rows:
        raise ValueError(f"No team rows parsed for {sport_meta['name']}")

    return {
        "id": sport_meta["id"],
        "name": sport_meta["name"],
        "source": sport_meta["url"],
        "rows": rows,
    }


# ── Render Page using Playwright Browser Session ──────────────────────────────

def fetch_all_rendered(sports):
    """
    Launches a single Chromium browser session and fetches/parses each page sequentially
    to save overhead and run fast in GitHub Actions.
    """
    from playwright.sync_api import sync_playwright

    scraped_data = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA)
        page = context.new_page()

        # Abort images, fonts, media for faster page loading
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font", "stylesheet")
            else route.continue_(),
        )

        for sport in sports:
            sport_id = sport["id"]
            url = sport["url"]
            print(f"Fetching [{sport['name']}] -> {url}...")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_selector("table", timeout=15000)
                except Exception:
                    pass
                html = page.content()
                data = parse_standings_table(html, sport)
                scraped_data[sport_id] = {"status": "ok", **data}
                print(f"  ✓ Successfully parsed {len(data['rows'])} rows.")
            except Exception as exc:
                print(f"  ✗ Error parsing {sport_id}: {exc}", file=sys.stderr)
                scraped_data[sport_id] = {"status": "error", "error": str(exc)[:300], "rows": []}

        browser.close()

    return scraped_data


# ── Main / Stale-Safe Persistence ─────────────────────────────────────────────

def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/data.json", help="Path to output data.json")
    args = ap.parse_args()

    out_path = Path(args.out)
    old_file_data = {}
    if out_path.exists():
        try:
            old_file_data = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            old_file_data = {}

    old_sports = old_file_data.get("sports", {})
    new_scraped = fetch_all_rendered(SPORTS_CONFIG)

    current_timestamp = now_iso()
    final_sports = {}

    for sport in SPORTS_CONFIG:
        s_id = sport["id"]
        res = new_scraped.get(s_id)
        old_res = old_sports.get(s_id)

        if res and res.get("status") == "ok":
            res["updated_at"] = current_timestamp
            final_sports[s_id] = res
        elif old_res and old_res.get("rows"):
            print(f"[{s_id}] Using previous data marked as stale.")
            final_sports[s_id] = {
                **old_res,
                "status": "stale",
                "error": res.get("error") if res else "Fetch failed",
            }
        else:
            final_sports[s_id] = res or {
                "id": s_id,
                "name": sport["name"],
                "source": sport["url"],
                "status": "error",
                "rows": [],
            }

    output_payload = {
        "generated_at": current_timestamp,
        "sports": final_sports,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\n✓ Successfully wrote all {len(final_sports)} sports to {out_path}")


if __name__ == "__main__":
    main()
