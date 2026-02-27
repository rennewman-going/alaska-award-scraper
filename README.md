# Alaska Award Scraper

Scrapes Alaska Airlines award flight availability for 54 airports ↔ PHX (Phoenix) across March 2026 – January 2027, using a two-pass strategy for price consistency.

**How it works:**
- **Pass 1** — scans every month per route and finds the absolute lowest points price across the full date range (capped at each airport's threshold).
- **Pass 2** — re-scans and records *only* dates where points equal that minimum. If 4.5k exists anywhere in the year for a route, only 4.5k dates are logged — months where 7.5k is the floor are skipped.

Output: `alaska_awards_PHX.csv` with compressed date ranges (e.g. `1,3-5,7`) and summary columns for points and taxes in each direction.

## Requirements

- Python 3.11+
- [Playwright](https://playwright.dev/python/) (headless Chromium)

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install the Chromium browser used by Playwright
playwright install chromium
```

## Run

```bash
python alaska_scraper.py
```

The script will print progress for each airport and month, then write results to `alaska_awards_PHX.csv`.

Estimated runtime: **~55 airports × 11 months × 2 directions × ~3 s/page ≈ 60–90 minutes**.

## Output columns

| Column | Description |
|---|---|
| `To` / `From` | PHX and the remote airport code |
| `Alt Origins` | (reserved, left blank) |
| `Feb 2026 D` / `R` | Days with award availability for that month, departure / return |
| … (Mar 2026 – Jan 2027) | One D and R column per month |
| `Feb 2027 D` / `R` | (reserved, left blank) |
| `Points (To PHX)` | Consistent minimum points price toward PHX |
| `Points (From PHX)` | Consistent minimum points price away from PHX |
| `Taxes (To PHX)` | Most common cash tax seen toward PHX |
| `Taxes (From PHX)` | Most common cash tax seen away from PHX |

Dates are compressed: `1,3-5,7` means days 1, 3, 4, 5, and 7 have availability.

## Notes

- The `output/` directory and `*.csv` files are excluded from version control via `.gitignore`.
- Alaska's site is JS-rendered; Playwright handles this automatically.
- If selectors break after an Alaska site update, adjust `LOAD_SELS` and `CELL_SELS` in `alaska_scraper.py`.
- Run the script from within the virtual environment so the correct Playwright browser is used.
