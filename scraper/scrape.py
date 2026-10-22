#!/usr/bin/env python3
"""
Fetch the Summit League Women's Volleyball standings and write them to
docs/data.json. Modeled directly after womens-hockey-board/scraper/scrape.py.

Safe by design: if the scrape fails, the last good data stays in the file
and is marked "stale" so the site never goes blank.

Usage:
    python scraper/scrape.py
    python scraper/scrape.py --out docs/data.json
    python scraper/scrape.py --standings-html saved.html --out /tmp/data.json
"""

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

STANDINGS_URL = (
    "https://thesummitleague.org/standings.aspx?path=wvball"
)
UA = "Mozilla/5.0 (compatible; TAPStandings/1.0; personal project)"


# ── helpers ───────────────────────────────────────────────────────────────────

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


# ── parser ────────────────────────────────────────────────────────────────────

def parse_standings(html):
    soup = BeautifulSoup(html, "html.parser")

    # Find the standings table — look for one that has a "Team" column
    target = None
    for table in soup.find_all("table"):
        hdrs = header_cells(table)
        hdrs_lower = [h.lower() for h in hdrs]
        if any("team" in h or "school" in h for h in hdrs_lower):
            target = table
            break

    if target is None:
        raise ValueError("Standings table not found (page layout may have changed).")

    headers = header_cells(target)
    hdrs_lower = [h.lower() for h in headers]

    def idx(*candidates):
        """Return the index of the first matching header, or None."""
        for c in candidates:
            for i, h in enumerate(hdrs_lower):
                if c in h:
                    return i
        return None

    team_idx = idx("team", "school", "name")

    # Conference record — prefer a combined "Conf" column, fall back to split W / L
    conf_combined = idx("conf record", "conference record")
    if conf_combined is None:
        # Look for a column that is just "conf" or "conference" (not "conf w" etc.)
        for i, h in enumerate(hdrs_lower):
            if h in ("conf", "conference"):
                conf_combined = i
                break
    conf_w_idx = idx("conf w", "cw", "conference w") if conf_combined is None else None
    conf_l_idx = idx("conf l", "cl", "conference l") if conf_combined is None else None

    # Overall record — prefer combined, fall back to split
    ovr_combined = idx("overall record", "ovr record", "total record")
    if ovr_combined is None:
        for i, h in enumerate(hdrs_lower):
            if h in ("overall", "ovr", "total"):
                ovr_combined = i
                break
    ovr_w_idx = idx("overall w", "ow", "ovr w") if ovr_combined is None else None
    ovr_l_idx = idx("overall l", "ol", "ovr l") if ovr_combined is None else None

    rows = []
    for tr in body_rows(target):
        cells = tr.find_all(["td", "th"])

        def cell_text(i):
            if i is None or i >= len(cells):
                return ""
            return clean(cells[i].get_text())

        team = team_text(cells[team_idx]) if team_idx is not None else f"Team {len(rows)+1}"
        if not team:
            continue

        # Build "W-L" strings
        if conf_combined is not None:
            conf = cell_text(conf_combined).replace("–", "-") or "?-?"
        elif conf_w_idx is not None and conf_l_idx is not None:
            conf = f"{cell_text(conf_w_idx)}-{cell_text(conf_l_idx)}"
        else:
            conf = "?-?"

        if ovr_combined is not None:
            ovr = cell_text(ovr_combined).replace("–", "-") or "?-?"
        elif ovr_w_idx is not None and ovr_l_idx is not None:
            ovr = f"{cell_text(ovr_w_idx)}-{cell_text(ovr_l_idx)}"
        else:
            ovr = "?-?"

        rows.append({
            "rank": len(rows) + 1,
            "team": team,
            "conf": conf,   # e.g. "8-0"
            "ovr":  ovr,    # e.g. "14-2"
        })

    if len(rows) < 4:
        raise ValueError(f"Only found {len(rows)} team(s); expected at least 4.")

    return {
        "source": STANDINGS_URL,
        "name":   "Summit League Women's Volleyball Standings",
        "rows":   rows,
    }


# ── fetching ──────────────────────────────────────────────────────────────────

def fetch_rendered(url):
    """Summit League uses JavaScript to render the table, so use Playwright."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_selector("table tbody tr", timeout=45000)
        html = page.content()
        browser.close()
    return html


# ── main ──────────────────────────────────────────────────────────────────────

def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

def core(section):
    return {k: v for k, v in (section or {}).items() if k not in ("updated_at", "status", "error")}

def refresh(name, old, build):
    try:
        data = build()
    except Exception as exc:
        print(f"[{name}] FAILED: {exc}", file=sys.stderr)
        if old:
            return {**old, "status": "stale", "error": str(exc)[:300]}, False
        return {"status": "error", "error": str(exc)[:300], "rows": []}, False

    if old and old.get("status") == "ok" and core(old) == core(data):
        print(f"[{name}] no change")
        return old, True

    print(f"[{name}] updated ({len(data['rows'])} rows)")
    return {**data, "status": "ok", "updated_at": now_iso()}, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/data.json")
    ap.add_argument("--standings-html", help="parse this saved file instead of fetching live")
    args = ap.parse_args()

    out = Path(args.out)
    old = json.loads(out.read_text()) if out.exists() else {}

    def build_standings():
        html = (
            Path(args.standings_html).read_text()
            if args.standings_html
            else fetch_rendered(STANDINGS_URL)
        )
        return parse_standings(html)

    standings, ok = refresh("standings", old.get("standings"), build_standings)
    new = {"standings": standings}

    if json.dumps(new, sort_keys=True) != json.dumps({"standings": old.get("standings")}, sort_keys=True):
        new["generated_at"] = now_iso()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {out}")
    else:
        print("data.json unchanged")

    if not standings.get("rows"):
        sys.exit(1)


if __name__ == "__main__":
    main()
