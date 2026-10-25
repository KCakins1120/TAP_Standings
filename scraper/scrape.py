#!/usr/bin/env python3
"""
Fetch standings from multiple Summit League, PFL, and NCHC sports pages.
Extracts Team, Conference record (W-L), Overall record (W-L) for all sports.
For MSOC and WSOC, also extracts CPTS (conference points).
For WSOC, skips the Pods table and uses the full 8-team table.

Writes structured output to docs/data.json.

Safe by design: if any individual source fails, the last good data for that
sport stays in the file marked "stale", so the website never goes blank.

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
        "include_cpts": False,
        "skip_pods": False,
    },
    {
        "id": "msoc",
        "name": "Summit League Men's Soccer",
        "url": "https://thesummitleague.org/standings.aspx?path=msoc",
        "include_cpts": True,
        "skip_pods": False,
    },
    {
        "id": "wsoc",
        "name": "Summit League Women's Soccer",
        "url": "https://thesummitleague.org/standings.aspx?path=wsoc",
        "include_cpts": True,
        "skip_pods": True,   # ← skip the Pods table, use the full 8-team table
    },
    {
        "id": "pfl",
        "name": "Pioneer Football League",
        "url": "https://pioneer-football.org/standings.aspx?standings=19",
        "include_cpts": False,
        "skip_pods": False,
    },
    {
        "id": "mbball",
        "name": "Summit League Men's Basketball",
        "url": "https://thesummitleague.org/standings.aspx?path=mbball",
        "include_cpts": False,
        "skip_pods": False,
    },
    {
        "id": "wbball",
        "name": "Summit League Women's Basketball",
        "url": "https://thesummitleague.org/standings.aspx?path=wbball",
        "include_cpts": False,
        "skip_pods": False,
    },
    {
        "id": "mhockey",
        "name": "NCHC Men's Ice Hockey",
        "url": "https://nchchockey.com/standings.aspx?path=mhockey",
        "include_cpts": False,
        "skip_pods": False,
    },
    {
        "id": "summit_195",
        "name": "Summit League Baseball",
        "url": "https://thesummitleague.org/standings.aspx?standings=195",
        "include_cpts": False,
        "skip_pods": False,
    },
    {
        "id": "summit_193",
        "name": "Summit League Softball",
        "url": "https://thesummitleague.org/standings.aspx?standings=193",
        "include_cpts": False,
        "skip_pods": False,
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

def is_pods_table(table):
    """
    Returns True if this table looks like a Pods/group table rather than
    the full standings table. Heuristics:
      - Fewer than 6 team rows in tbody
      - Header contains "pod" or "group"
      - Table has a caption or preceding heading mentioning "pod"
    """
    hdrs = [h.lower() for h in header_cells(table)]
    if any("pod" in h or "group" in h for h in hdrs):
        return True

    # Check for a preceding sibling heading that mentions "pod"
    prev = table.find_previous(["h2", "h3", "h4", "caption", "th"])
    if prev and "pod" in prev.get_text().lower():
        return True

    # If the table has fewer than 6 data rows it's likely a pod sub-table
    rows = body_rows(table)
    if len(rows) < 6:
        return True

    return False


# ── Generic Sidearm Standings Table Parser ────────────────────────────────────

def parse_standings_table(html, sport_meta):
    soup = BeautifulSoup(html, "html.parser")
    include_cpts = sport_meta.get("include_cpts", False)
    skip_pods    = sport_meta.get("skip_pods", False)

    # ── Find the right table ──────────────────────────────────────────────────
    candidate_tables = []
    for table in soup.find_all("table"):
        hdrs = header_cells(table)
        hdrs_lower = [h.lower() for h in hdrs]
        if any("team" in h or "school" in h or "member" in h for h in hdrs_lower):
            candidate_tables.append(table)

    if not candidate_tables:
        raise ValueError(f"No standings table found for {sport_meta['name']}")

    target = None
    if skip_pods:
        # For WSOC: skip any pods/group tables, pick the first full 8-team table
        for table in candidate_tables:
            if not is_pods_table(table):
                target = table
                break
        # Fallback: just take the largest table by row count
        if target is None:
            target = max(candidate_tables, key=lambda t: len(body_rows(t)))
    else:
        target = candidate_tables[0]

    if target is None:
        raise ValueError(f"Could not identify standings table for {sport_meta['name']}")

    # ── Map column indices ────────────────────────────────────────────────────
    headers    = header_cells(target)
    hdrs_lower = [h.lower() for h in headers]

    def idx(*candidates):
        """Return index of first header that contains any candidate string."""
        for c in candidates:
            for i, h in enumerate(hdrs_lower):
                if c in h:
                    return i
        return None

    def exact_idx(*candidates):
        """Return index of first header that exactly matches a candidate string."""
        for c in candidates:
            for i, h in enumerate(hdrs_lower):
                if h == c:
                    return i
        return None

    team_idx = idx("team", "school", "member", "institution", "name")

    # Conference record — prefer combined column, fall back to split W / L
    conf_combined = exact_idx("conf", "conference", "league")
    if conf_combined is None:
        conf_combined = idx("conf record", "conference record")
    conf_w_idx = idx("conf w", "cw", "conference w") if conf_combined is None else None
    conf_l_idx = idx("conf l", "cl", "conference l") if conf_combined is None else None

    # Overall record — prefer combined, fall back to split
    ovr_combined = exact_idx("overall", "ovr", "total")
    if ovr_combined is None:
        ovr_combined = idx("overall record", "ovr record", "total record")
    ovr_w_idx = idx("overall w", "ow", "ovr w") if ovr_combined is None else None
    ovr_l_idx = idx("overall l", "ol", "ovr l") if ovr_combined is None else None

    # CPTS — conference points (soccer only)
    cpts_idx = None
    if include_cpts:
        cpts_idx = exact_idx("cpts", "pts", "points", "conf pts", "conf. pts")
        if cpts_idx is None:
            cpts_idx = idx("cpts", "conf pts", "conf. pts")

    # ── Parse rows ────────────────────────────────────────────────────────────
    rows = []
    for tr in body_rows(target):
        cells = tr.find_all(["td", "th"])

        def cell_text(i):
            if i is None or i >= len(cells):
                return ""
            return clean(cells[i].get_text())

        team = team_text(cells[team_idx]) if team_idx is not None else ""
        if not team or team.lower() in ("team", "school", "member", "institution"):
            continue

        # Conference record
        if conf_combined is not None:
            conf = cell_text(conf_combined).replace("–", "-") or "0-0"
        elif conf_w_idx is not None and conf_l_idx is not None:
            conf = f"{cell_text(conf_w_idx)}-{cell_text(conf_l_idx)}"
        else:
            conf = "—"

        # Overall record
        if ovr_combined is not None:
            ovr = cell_text(ovr_combined).replace("–", "-") or "0-0"
        elif ovr_w_idx is not None and ovr_l_idx is not None:
            ovr = f"{cell_text(ovr_w_idx)}-{cell_text(ovr_l_idx)}"
        else:
            ovr = "—"

        row_data = {
            "rank": len(rows) + 1,
            "team": team,
            "conf": conf,
            "ovr":  ovr,
        }

        # CPTS (soccer only)
        if include_cpts and cpts_idx is not None:
            row_data["cpts"] = cell_text(cpts_idx) or "0"

        rows.append(row_data)

    if not rows:
        raise ValueError(f"No team rows parsed for {sport_meta['name']}")

    return {
        "id":     sport_meta["id"],
        "name":   sport_meta["name"],
        "source": sport_meta["url"],
        "rows":   rows,
    }


# ── Render Pages using a Single Playwright Browser Session ────────────────────

def fetch_all_rendered(sports):
    """
    Launches one Chromium browser and fetches/parses each page sequentially.
    Aborts images, fonts, and media for faster loading in GitHub Actions.
    """
    from playwright.sync_api import sync_playwright

    scraped_data = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA)
        page    = context.new_page()

        # Speed up: skip images, fonts, media
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font", "stylesheet")
            else route.continue_(),
        )

        for sport in sports:
            sport_id = sport["id"]
            url      = sport["url"]
            print(f"Fetching [{sport['name']}] -> {url} ...")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_selector("table", timeout=15000)
                except Exception:
                    print(f"  ⚠ No table selector found within timeout for {sport_id}")
                html = page.content()
                data = parse_standings_table(html, sport)
                scraped_data[sport_id] = {"status": "ok", **data}
                cpts_note = " (with CPTS)" if sport.get("include_cpts") else ""
                print(f"  ✓ Parsed {len(data['rows'])} rows{cpts_note}.")
            except Exception as exc:
                print(f"  ✗ Error [{sport_id}]: {exc}", file=sys.stderr)
                scraped_data[sport_id] = {
                    "status": "error",
                    "error":  str(exc)[:300],
                    "rows":   [],
                }

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

    # Load previous data for stale fallback
    old_sports = {}
    if out_path.exists():
        try:
            old_sports = json.loads(out_path.read_text(encoding="utf-8")).get("sports", {})
        except Exception:
            pass

    new_scraped       = fetch_all_rendered(SPORTS_CONFIG)
    current_timestamp = now_iso()
    final_sports      = {}

    for sport in SPORTS_CONFIG:
        s_id  = sport["id"]
        res   = new_scraped.get(s_id)
        old   = old_sports.get(s_id)

        if res and res.get("status") == "ok":
            res["updated_at"] = current_timestamp
            final_sports[s_id] = res
        elif old and old.get("rows"):
            print(f"[{s_id}] Scrape failed — keeping previous data as stale.")
            final_sports[s_id] = {
                **old,
                "status": "stale",
                "error":  res.get("error") if res else "Fetch failed",
            }
        else:
            final_sports[s_id] = res or {
                "id":     s_id,
                "name":   sport["name"],
                "source": sport["url"],
                "status": "error",
                "rows":   [],
            }

    payload = {
        "generated_at": current_timestamp,
        "sports":       final_sports,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\n✓ Wrote {len(final_sports)} sports to {out_path}")


if __name__ == "__main__":
    main()
