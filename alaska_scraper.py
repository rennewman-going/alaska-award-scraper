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

# ── Parse / compress helpers ───────────────────────────────────────────────────
def parse_cell(text: str):
    """
    Parse a calendar cell like "1\n4.5k +$19" or "9\n20k +$6".
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

# ── Form interaction ───────────────────────────────────────────────────────────
SEARCH_BASE = "https://www.alaskaair.com/search/results"

_datepicker_debug_saved = False

# ── JavaScript helpers that pierce shadow DOM ──────────────────────────────────
_JS_FIND = """
function findDeep(root, selector) {
    var el = root.querySelector(selector);
    if (el) return el;
    var all = root.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
        if (all[i].shadowRoot) {
            el = findDeep(all[i].shadowRoot, selector);
            if (el) return el;
        }
    }
    return null;
}
"""

async def fill_and_search(page, origin, dest, year, month):
    """
    Navigate to the search form, fill it (Flexible dates + Use points +
    1 passenger + departure month), and click Search flights.
    Returns True if the form was submitted.
    """
    search_date = date(year, month, 1)
    date_str = f"{year}{month:02d}01"
    # Include all known params — DT1/A/AT/FD may pre-populate date, passengers,
    # award mode, and flexible dates, reducing how much UI interaction is needed.
    url = (f"{SEARCH_BASE}?O={origin}&D={dest}&RT=false"
           f"&DT1={date_str}&A=1&C=0&AT=MIL&FD=1")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        pass
    await asyncio.sleep(5)   # Auro web components need extra init time

    # Save a one-time snapshot of the raw form
    global _form_debug_saved
    if not _form_debug_saved:
        _form_debug_saved = True
        try:
            await page.screenshot(path="debug_form.png", full_page=False)
            with open("debug_form.html", "w", encoding="utf-8") as f:
                f.write(await page.content())
            print("    📄 Saved debug_form.png/.html")
        except Exception:
            pass

    # ── 1. Flexible dates ─────────────────────────────────────────────────────
    try:
        await page.get_by_label("Flexible dates").check(timeout=3000)
    except Exception:
        try:
            await page.locator("text=Flexible dates").click(timeout=3000)
        except Exception:
            pass
    await asyncio.sleep(0.5)

    # ── 2. Use points ─────────────────────────────────────────────────────────
    try:
        await page.get_by_label("Use points").check(timeout=3000)
    except Exception:
        try:
            await page.locator("text=Use points").click(timeout=3000)
        except Exception:
            pass
    await asyncio.sleep(0.5)

    # ── 3. Date — use JS to pierce shadow DOM and open/navigate the picker ────
    global _datepicker_debug_saved
    try:
        # Check if URL param DT1 already pre-populated the date field
        date_pre_set = await page.evaluate(f"""
            (() => {{
                {_JS_FIND}
                var trigger = findDeep(document, "div[slot='trigger']");
                if (!trigger) return false;
                var txt = (trigger.textContent || '').trim().toLowerCase();
                return txt !== '' && txt !== 'date' && txt.length > 4;
            }})()
        """)

        if not date_pre_set:
            # Open date picker — JS click pierces shadow DOM pointer-event issues
            await page.evaluate(f"""
                (() => {{
                    {_JS_FIND}
                    var trigger = findDeep(document, "div[slot='trigger']") ||
                                  findDeep(document, "auro-datepicker");
                    if (trigger) trigger.click();
                }})()
            """)
            await asyncio.sleep(2)

            # Save datepicker debug screenshot
            if not _datepicker_debug_saved:
                _datepicker_debug_saved = True
                try:
                    await page.screenshot(path="debug_datepicker.png", full_page=False)
                    print("    📄 Saved debug_datepicker.png")
                except Exception:
                    pass

            # Navigate picker forward until target month is visible
            target = search_date.strftime("%B %Y").lower()   # "march 2026"
            for _ in range(18):
                visible = await page.evaluate(f"""
                    (() => {{
                        var headers = document.querySelectorAll(
                            '[aria-live], [class*="month"], [class*="calendar"]'
                        );
                        for (var i = 0; i < headers.length; i++) {{
                            if ((headers[i].textContent || '').toLowerCase()
                                    .indexOf('{target}') >= 0) return true;
                        }}
                        return false;
                    }})()
                """)
                if visible:
                    break
                # Click Next-month arrow via JS
                clicked = await page.evaluate("""
                    (() => {
                        var btns = document.querySelectorAll('button');
                        for (var i = 0; i < btns.length; i++) {
                            var lbl = (btns[i].getAttribute('aria-label') || '').toLowerCase();
                            if (lbl.indexOf('next month') >= 0 || lbl === 'next') {
                                btns[i].click(); return true;
                            }
                        }
                        return false;
                    })()
                """)
                if not clicked:
                    # Playwright fallback
                    for sel in ["button[aria-label*='Next month' i]",
                                "button[aria-label='Next']"]:
                        try:
                            await page.locator(sel).last.click(timeout=500)
                            break
                        except Exception:
                            continue
                await asyncio.sleep(0.5)

            # Click day "1" via JS (exact text match, skip disabled/outside days)
            await page.evaluate("""
                (() => {
                    var candidates = document.querySelectorAll(
                        'td, button, [role="gridcell"]'
                    );
                    for (var i = 0; i < candidates.length; i++) {
                        var el = candidates[i];
                        var txt = (el.textContent || '').trim();
                        var cls = (el.className || '').toLowerCase();
                        if (txt === '1' && !el.disabled &&
                            cls.indexOf('disabled') < 0 &&
                            cls.indexOf('other') < 0 &&
                            cls.indexOf('outside') < 0) {
                            el.click(); return;
                        }
                    }
                })()
            """)
            await asyncio.sleep(0.5)
    except Exception as e:
        print(f"(date: {e}) ", end="")

    # ── 4. Passengers: ensure 1 adult ─────────────────────────────────────────
    try:
        # Check if URL param A=1 already pre-populated passengers
        pax_pre_set = await page.evaluate(f"""
            (() => {{
                {_JS_FIND}
                var el = findDeep(document, "div[slot='valueText']") ||
                         findDeep(document, "[slot='valueText']");
                if (!el) return false;
                return (el.textContent || '').toLowerCase().indexOf('1 adult') >= 0;
            }})()
        """)

        if not pax_pre_set:
            # Open passenger dropdown via JS
            await page.evaluate(f"""
                (() => {{
                    {_JS_FIND}
                    var pax = findDeep(document, "div[slot='valueText']") ||
                              findDeep(document, "[slot='valueText']") ||
                              findDeep(document, "[class*='passenger']");
                    if (pax) pax.click();
                }})()
            """)
            await asyncio.sleep(1)

            # Click the + (Add adult) button via JS
            await page.evaluate("""
                (() => {
                    var btns = document.querySelectorAll('button');
                    for (var i = 0; i < btns.length; i++) {
                        var lbl = (btns[i].getAttribute('aria-label') || '').toLowerCase();
                        if (lbl.indexOf('add adult') >= 0 ||
                            lbl.indexOf('increase adult') >= 0 ||
                            (lbl.indexOf('adult') >= 0 && lbl.indexOf('add') >= 0)) {
                            btns[i].click(); return;
                        }
                    }
                    // Fallback: first visible + button
                    for (var i = 0; i < btns.length; i++) {
                        if ((btns[i].textContent || '').trim() === '+' &&
                            btns[i].offsetParent !== null) {
                            btns[i].click(); return;
                        }
                    }
                })()
            """)
            await asyncio.sleep(0.4)

            # Close dropdown
            try:
                await page.locator("button:has-text('Done')").click(timeout=1500)
            except Exception:
                await page.keyboard.press("Escape")
    except Exception as e:
        print(f"(pax: {e}) ", end="")
    await asyncio.sleep(0.5)

    # ── 5. Submit — JS click first (most reliable), Playwright as fallback ─────
    try:
        submitted = await page.evaluate("""
            (() => {
                var btns = document.querySelectorAll('button, [role="button"]');
                for (var i = 0; i < btns.length; i++) {
                    if ((btns[i].textContent || '').indexOf('Search flights') >= 0) {
                        btns[i].click(); return true;
                    }
                }
                return false;
            })()
        """)
        if submitted:
            return True
    except Exception:
        pass

    for submit_sel in [
        "button:has-text('Search flights')",
        "[role='button']:has-text('Search flights')",
        "button[type='submit']",
    ]:
        try:
            btn = page.locator(submit_sel).first
            try:
                await btn.click(force=True, timeout=4000)
            except Exception:
                await btn.dispatch_event("click")
            return True
        except Exception:
            continue

    print("    ✗ Could not submit form", end=" ")
    return False

# ── Calendar navigation ────────────────────────────────────────────────────────
async def click_next_month(page):
    """Click the calendar's Next Month button. Returns True if clicked."""
    for sel in [
        "button[aria-label*='Next month' i]",
        "button[aria-label='Next']",
        "button[title*='Next month' i]",
        "button[class*='next-month']",
        "button[class*='nextMonth']",
        "button[class*='arrow-right']",
        "button[class*='arrowRight']",
        "[class*='next-month'] button",
        "[class*='nextMonth'] button",
        # last chevron/arrow button on page as fallback
        "button:has(svg) >> nth=-1",
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                return True
        except Exception:
            continue
    return False

# ── Results detection ──────────────────────────────────────────────────────────
LOAD_SELS = [
    "[class*='calendar']", "[class*='Calendar']",
    "[class*='flight-result']", "[class*='flightResult']",
    "[class*='availability']", "[class*='Availability']",
    "[role='grid']", "table",
]
CELL_SELS = [
    "[class*='calendar-day']:not([class*='disabled']):not([class*='empty'])",
    "[class*='CalendarDay']:not([class*='disabled']):not([class*='outside'])",
    "[class*='day-cell']:not([class*='disabled']):not([class*='empty'])",
    "[role='gridcell']:not([class*='disabled']):not([class*='empty'])",
    "td:not([class*='disabled']):not([class*='empty']):not([class*='outside'])",
]

_debug_saved = False
_form_debug_saved = False

async def wait_for_results(page, label):
    """Wait for search results to appear (search form gone + results present)."""
    global _debug_saved

    # After submit, wait briefly then check if form validation blocked navigation.
    # If the Search button is still visible AND validation errors exist, fail fast.
    await asyncio.sleep(3)
    try:
        still_on_form = await page.locator(
            "button:has-text('Search flights')"
        ).is_visible(timeout=1000)
        if still_on_form:
            # Check for validation error text
            err_text = await page.evaluate("""
                (() => {
                    var sels = ['[class*="error"]', '[class*="validation"]',
                                '[aria-live="assertive"]', '[class*="alert"]'];
                    for (var i = 0; i < sels.length; i++) {
                        var els = document.querySelectorAll(sels[i]);
                        for (var j = 0; j < els.length; j++) {
                            var t = (els[j].textContent || '').trim();
                            if (t.length > 10) return t.substring(0, 120);
                        }
                    }
                    return '';
                })()
            """)
            if err_text:
                print(f"    ✗ Validation error: {err_text[:80]}")
                return False
    except Exception:
        pass

    # Wait for search button to disappear (navigation / results loading)
    try:
        await page.wait_for_selector(
            "button:has-text('Search flights')", state="hidden", timeout=25000
        )
    except PWTimeout:
        pass
    await asyncio.sleep(2)

    # Wait for any results indicator
    for sel in LOAD_SELS:
        try:
            await page.wait_for_selector(sel, timeout=12000)
            # Save a one-time debug screenshot of the results page
            if not _debug_saved:
                _debug_saved = True
                try:
                    await page.screenshot(path="debug_results.png", full_page=False)
                    html = await page.content()
                    with open("debug_results.html", "w", encoding="utf-8") as f:
                        f.write(html)
                    print("    📄 Saved debug_results.png/.html (results page)")
                except Exception:
                    pass
            return True
        except PWTimeout:
            continue

    print(f"    ⚠ Nothing rendered: {label}")
    if not _debug_saved:
        _debug_saved = True
        try:
            await page.screenshot(path="debug_results.png", full_page=False)
            html = await page.content()
            with open("debug_results.html", "w", encoding="utf-8") as f:
                f.write(html)
            print("    📄 Saved debug_results.png/.html — share these to fix selectors")
        except Exception as e:
            print(f"    (debug save failed: {e})")
    return False

async def get_cells(page):
    for sel in CELL_SELS:
        cells = await page.query_selector_all(sel)
        if cells:
            return cells
    return []

async def parse_current_month(page, year, month):
    """Extract (day, pts, tax) rows from whatever is currently rendered."""
    rows = []
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
    return rows

# ── Route scraping ─────────────────────────────────────────────────────────────
async def scrape_route(page, origin, dest, max_pts):
    """
    Two-pass scrape for one route direction.

    Pass 1 — Submit search once, then click Next Month through all months,
              cache raw cell data for each.  Find absolute lowest price
              within max_pts across the full date range.
    Pass 2 — From the cache, record only dates where points == absolute_min.

    Returns:
        month_days : dict  { "Mar 2026": [1,3,5,...], ... }
        abs_min    : int   absolute lowest points price
        typical_tax: str   most common tax seen
    """
    print(f"\n  ── {origin}→{dest} (threshold {max_pts:,}) ──")

    first_year, first_month = MONTHS[0]

    # ── Submit form for first month ───────────────────────────────────────────
    print(f"    Submitting form... ", end="", flush=True)
    ok = await fill_and_search(page, origin, dest, first_year, first_month)
    if not ok:
        return {lbl: "" for lbl in MONTH_LABELS.values()}, None, None

    print("waiting for results... ", end="", flush=True)
    if not await wait_for_results(page, f"{origin}→{dest} {MONTH_LABELS[MONTHS[0]]}"):
        return {lbl: "" for lbl in MONTH_LABELS.values()}, None, None
    print("ok")

    # ── Pass 1: iterate through all months ────────────────────────────────────
    cache    = {}
    all_pts  = []
    all_taxes = []

    for i, (year, month) in enumerate(MONTHS):
        lbl   = MONTH_LABELS[(year, month)]
        label = f"{origin}→{dest} {lbl}"
        print(f"    Pass1 {lbl} ...", end=" ", flush=True)

        if i > 0:
            clicked = await click_next_month(page)
            if clicked:
                await asyncio.sleep(3)
            else:
                # Fallback: re-submit form for this month
                print(f"(re-submitting) ", end="", flush=True)
                await fill_and_search(page, origin, dest, year, month)
                await wait_for_results(page, label)

        raw = await parse_current_month(page, year, month)
        cache[(year, month)] = raw

        for _, pts, tax in raw:
            if pts <= max_pts:
                all_pts.append(pts)
            if tax:
                all_taxes.append(tax)

        found = sorted(set(p for _, p, _ in raw if p <= max_pts))
        print(f"eligible: {found}")
        await asyncio.sleep(1)

    if not all_pts:
        print(f"    No availability within threshold for {origin}→{dest}")
        return {lbl: "" for lbl in MONTH_LABELS.values()}, None, None

    abs_min     = min(all_pts)
    typical_tax = max(set(all_taxes), key=all_taxes.count) if all_taxes else None
    print(f"    → Absolute min: {fmt_points(abs_min)}  Tax: {typical_tax}")

    # ── Pass 2: filter from cache (no more navigation) ────────────────────────
    month_days = {}
    for year, month in MONTHS:
        lbl  = MONTH_LABELS[(year, month)]
        days = [d for d, pts, _ in cache[(year, month)] if pts == abs_min]
        month_days[lbl] = days
        if days:
            print(f"    Pass2 {lbl}: {compress_days(days)}")

    return month_days, abs_min, typical_tax

# ── Main ───────────────────────────────────────────────────────────────────────
async def run_all():
    results = {}
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

            # Direction D: other airport → PHX
            days_d, min_d, tax_d = await scrape_route(page, iata, DESTINATION, max_pts)
            for lbl, days in days_d.items():
                results[iata][f"{lbl} D"] = compress_days(days)
            results[iata]["pts_d"] = min_d
            results[iata]["tax_d"] = tax_d

            # Direction R: PHX → other airport
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
    print("Alaska Airlines Award Scraper (two-pass consistent points)")
    print(f"Destination : {DESTINATION}")
    print(f"Airports    : {len(AIRPORTS)}")
    print(f"Routes      : {len(AIRPORTS) * 2} (each airport ↔ PHX)\n")
    data = asyncio.run(run_all())
    df   = build_dataframe(data)
    out  = "alaska_awards_PHX.csv"
    df.to_csv(out, index=False)
    print(f"\n✅ Done! Saved to: {out}")
    preview = ["From", "Points (To PHX)", "Points (From PHX)",
               "Taxes (To PHX)", "Taxes (From PHX)"]
    print(df[preview].to_string(index=False))
