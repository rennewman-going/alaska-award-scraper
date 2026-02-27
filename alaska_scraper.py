"""
Alaska Airlines Award Scraper – PHX (two-pass consistent points)
=================================================================
Pass 1: scan every month for a route → find the absolute lowest points price
        ever seen across the entire date range (e.g. DEN→PHX = 4,500).
Pass 2: re-scan every month → record ONLY dates where points == that minimum.
This means if 4.5k exists anywhere in the year for a route, only 4.5k dates
are ever recorded. Months where 7.5k is the cheapest get no dates logged.
Output: alaska_awards_PHX.csv matching your sheet format, plus 4 summary cols:
  Points (To PHX) | Points (From PHX) | Taxes (To PHX) | Taxes (From PHX)
Requirements:
    pip install playwright pandas
    playwright install chromium
Run:
    python alaska_scraper.py
"""
import asyncio
import re
from calendar import monthrange
from datetime import date
import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── Config ─────────────────────────────────────────────────────────────────────
AIRPORTS = {
    "ABQ": 4500, "ASE": 4500, "AUS": 7500, "BIL": 7500, "BOI": 5000,
    "DEN": 4500, "DFW": 7500, "DRO": 7500, "DSM": 7500, "EGE": 7500,
    "ELP": 7500, "EUG": 7500, "FAR": 7500, "FAT": 7500, "GEG": 7500,
    "GRR": 7500, "GTJ": 7500, "HNL": 7500, "IAH": 7500, "ICT": 7500,
    "IDA": 7500, "KOA": 7500, "LAS": 7500, "LAX": 7500, "LBB": 7500,
    "LIH": 7500, "LIT": 7500, "MCI": 7500, "MEM": 7500, "MSN": 7500,
    "MSP": 7500, "MSY": 7500, "OGG": 7500, "OKC": 7500, "OMA": 7500,
    "PDX": 7500, "PSP": 7500, "RDM": 7500, "RNO": 7500, "SAF": 7500,
    "SAN": 7500, "SAT": 7500, "SBA": 7500, "SEA": 7500, "SFO": 7500,
    "SJC": 7500, "SLC": 7500, "SMF": 7500, "STL": 7500, "STS": 7500,
    "SUN": 7500, "TUL": 7500, "XNA": 7500, "PVU": 7500,
}
DESTINATION = "PHX"
MONTHS = [
    (2026, 3), (2026, 4), (2026, 5), (2026, 6), (2026, 7),
    (2026, 8), (2026, 9), (2026, 10), (2026, 11), (2026, 12),
    (2027, 1),
]
MONTH_LABELS = {
    (2026, 3):  "Mar 2026",  (2026, 4):  "Apr 2026",
    (2026, 5):  "May 2026",  (2026, 6):  "Jun 2026",
    (2026, 7):  "Jul 2026",  (2026, 8):  "Aug 2026",
    (2026, 9):  "Sep 2026",  (2026, 10): "Oct 2026",
    (2026, 11): "Nov 2026",  (2026, 12): "Dec 2026",
    (2027, 1):  "Jan 2027",
}

# ── URL / parse helpers ────────────────────────────────────────────────────────
def award_url(origin, dest, year, month):
    dt = date(year, month, 1).strftime("%Y-%m-%d")
    return (
        f"https://www.alaskaair.com/search/results"
        f"?O={origin}&D={dest}&DT1={dt}&AT=MIL&RT=false&YT=1&SD=CALENDAR"
    )

def parse_cell(text: str):
    """
    Parse a cell like "1\n4.5k +$19" or "9\n20k +$6".
    Returns (day: int, points: int, tax: str|None) or None.
    """
    text = text.strip()
    day_m = re.match(r"^(\d{1,2})", text)
    if not day_m:
        return None
    day = int(day_m.group(1))
    pts_m = re.search(r"([\d.]+)k", text, re.IGNORECASE)
    if not pts_m:
        return None
    points = int(float(pts_m.group(1)) * 1000)
    tax_m = re.search(r"\+\s*\$(\d+(?:\.\d{1,2})?)", text)
    tax = f"${tax_m.group(1)}" if tax_m else None
    return day, points, tax

def compress_days(days: list) -> str:
    if not days:
        return ""
    days = sorted(set(days))
    parts = []
    start = prev = days[0]
    for d in days[1:]:
        if d == prev + 1:
            prev = d
        else:
            parts.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = d
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)

def fmt_points(pts: int) -> str:
    return f"{pts // 1000}k" if pts % 1000 == 0 else f"{pts / 1000:.1f}k"

# ── Page loading ───────────────────────────────────────────────────────────────
LOAD_SELS = [
    "[class*='calendar']", "[class*='Calendar']",
    "[class*='flight-result']", "[class*='flightResult']",
]
CELL_SELS = [
    "[class*='calendar-day']:not([class*='disabled']):not([class*='empty'])",
    "[class*='CalendarDay']:not([class*='disabled']):not([class*='outside'])",
    "[class*='day-cell']:not([class*='disabled']):not([class*='empty'])",
    "td:not([class*='disabled']):not([class*='empty'])",
]

async def wait_for_page(page, label):
    for sel in LOAD_SELS:
        try:
            await page.wait_for_selector(sel, timeout=18000)
            return True
        except PWTimeout:
            continue
    print(f"    ⚠ Nothing rendered: {label}")
    return False

async def get_cells(page):
    for sel in CELL_SELS:
        cells = await page.query_selector_all(sel)
        if cells:
            return cells
    return []

# ── Core fetch: one route, one month → raw cell data ──────────────────────────
async def fetch_month_raw(page, origin, dest, year, month):
    """
    Load the calendar and return a list of (day, points, tax) tuples
    for every non-disabled cell. No filtering applied here.
    """
    url   = award_url(origin, dest, year, month)
    label = f"{origin}→{dest} {MONTH_LABELS[(year,month)]}"
    rows  = []
    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
        if not await wait_for_page(page, label):
            return rows
        _, max_day = monthrange(year, month)
        for cell in await get_cells(page):
            try:
                parsed = parse_cell((await cell.inner_text()).strip())
                if not parsed:
                    continue
                day, pts, tax = parsed
                if 1 <= day <= max_day:
                    rows.append((day, pts, tax))
            except Exception:
                continue
    except PWTimeout:
        print(f"    ✗ Timeout: {label}")
    except Exception as e:
        print(f"    ✗ Error {label}: {e}")
    return rows

# ── Two-pass logic per route ───────────────────────────────────────────────────
async def scrape_route(page, origin, dest, max_pts):
    """
    Two-pass scrape for one route direction.
    Pass 1 — collect all raw cell data for every month,
              find the absolute lowest points price across the whole date range,
              capped at max_pts (the airport's threshold).
    Pass 2 — using the cached raw data, record only dates where
              points == absolute_min.
    Returns:
        month_days : dict  { "Mar 2026": [1,3,5,...], ... }
        abs_min    : int   absolute lowest points price (the consistent floor)
        typical_tax: str   most common tax seen across all months
    """
    print(f"\n  ── {origin}→{dest} (threshold {max_pts:,}) ──")
    # Pass 1: load and cache all months
    cache = {}   # { (year,month): [(day,pts,tax), ...] }
    all_pts  = []
    all_taxes = []
    for year, month in MONTHS:
        print(f"    Pass1 {MONTH_LABELS[(year,month)]} ...", end=" ")
        raw = await fetch_month_raw(page, origin, dest, year, month)
        cache[(year, month)] = raw
        for day, pts, tax in raw:
            if pts <= max_pts:          # only consider prices within threshold
                all_pts.append(pts)
            if tax:
                all_taxes.append(tax)
        found = [p for _, p, _ in raw if p <= max_pts]
        print(f"eligible prices: {sorted(set(found))}")
        await asyncio.sleep(1.2)
    if not all_pts:
        print(f"    No availability within threshold for {origin}→{dest}")
        return {lbl: "" for lbl in MONTH_LABELS.values()}, None, None
    abs_min = min(all_pts)
    typical_tax = max(set(all_taxes), key=all_taxes.count) if all_taxes else None
    print(f"    → Absolute min price: {fmt_points(abs_min)}  Tax: {typical_tax}")
    # Pass 2: record only dates matching abs_min exactly
    month_days = {}
    for year, month in MONTHS:
        lbl  = MONTH_LABELS[(year, month)]
        days = [day for day, pts, _ in cache[(year, month)] if pts == abs_min]
        month_days[lbl] = days
        if days:
            print(f"    Pass2 {lbl}: {compress_days(days)}")
    return month_days, abs_min, typical_tax

# ── Main ───────────────────────────────────────────────────────────────────────
async def run_all():
    results = {}   # { iata: { col: value } }
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()
        for iata, max_pts in AIRPORTS.items():
            print(f"\n{'='*60}")
            print(f"AIRPORT: {iata}  (threshold: {max_pts:,} pts)")
            print(f"{'='*60}")
            results[iata] = {}
            # Direction D: origin → PHX
            days_d, min_d, tax_d = await scrape_route(page, iata, DESTINATION, max_pts)
            for lbl, days in days_d.items():
                results[iata][f"{lbl} D"] = compress_days(days)
            results[iata]["pts_d"] = min_d
            results[iata]["tax_d"] = tax_d
            # Direction R: PHX → origin
            days_r, min_r, tax_r = await scrape_route(page, DESTINATION, iata, max_pts)
            for lbl, days in days_r.items():
                results[iata][f"{lbl} R"] = compress_days(days)
            results[iata]["pts_r"] = min_r
            results[iata]["tax_r"] = tax_r
        await browser.close()
    return results

# ── Build CSV ──────────────────────────────────────────────────────────────────
def build_dataframe(results):
    month_cols = []
    for ym in MONTHS:
        lbl = MONTH_LABELS[ym]
        month_cols += [f"{lbl} D", f"{lbl} R"]
    rows = []
    for iata, data in results.items():
        row = {
            "To": DESTINATION, "From": iata, "Alt Origins": "",
            "Feb 2026 D": "", "Feb 2026 R": "",
        }
        for col in month_cols:
            row[col] = data.get(col, "")
        row["Feb 2027 D"] = ""
        row["Feb 2027 R"] = ""
        pts_d = data.get("pts_d")
        pts_r = data.get("pts_r")
        row["Points (To PHX)"]   = fmt_points(pts_d) if pts_d else ""
        row["Points (From PHX)"] = fmt_points(pts_r) if pts_r else ""
        row["Taxes (To PHX)"]    = data.get("tax_d") or ""
        row["Taxes (From PHX)"]  = data.get("tax_r") or ""
        rows.append(row)
    all_cols = (
        ["To", "From", "Alt Origins", "Feb 2026 D", "Feb 2026 R"]
        + month_cols
        + ["Feb 2027 D", "Feb 2027 R",
           "Points (To PHX)", "Points (From PHX)",
           "Taxes (To PHX)",  "Taxes (From PHX)"]
    )
    return pd.DataFrame(rows, columns=all_cols)

# ── Entry ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    total_fetches = len(AIRPORTS) * len(MONTHS) * 2   # x2 for two passes
    print("Alaska Airlines Award Scraper (two-pass consistent points)")
    print(f"Destination : {DESTINATION}")
    print(f"Airports    : {len(AIRPORTS)}")
    print(f"Total fetches: {total_fetches}  (~{total_fetches * 3 // 60} min estimated)\n")
    data = asyncio.run(run_all())
    df   = build_dataframe(data)
    out = "alaska_awards_PHX.csv"
    df.to_csv(out, index=False)
    print(f"\n✅ Done! Saved to: {out}")
    preview = ["From", "Points (To PHX)", "Points (From PHX)",
               "Taxes (To PHX)", "Taxes (From PHX)"]
    print(df[preview].to_string(index=False))
