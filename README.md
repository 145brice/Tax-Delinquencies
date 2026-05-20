# Nashville TN — Tax Delinquency & Pre-Foreclosure Scraper

Scrapes 7 Nashville-area counties for **tax delinquent properties** and **pre-foreclosure (Lis Pendens)** records, exports to CSV, and serves a sortable/filterable admin portal.

## Counties Covered

| County | County Seat | Sources |
|--------|-------------|---------|
| Davidson (Nashville) | Nashville | Chancery Clerk tax sale page + Register of Deeds Lis Pendens |
| Williamson | Franklin | County delinquent tax page + Register of Deeds |
| Rutherford | Murfreesboro | RC Chancery Court delinquent tax page |
| Wilson | Lebanon | County Chancery Court + Trustee |
| Sumner | Gallatin | County Chancery Court + Trustee |
| Robertson | Springfield | County website + Trustee |
| Cheatham | Ashland City | County website + Trustee |

## Setup

```bash
# 1. Create virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate    # Mac/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the scraper (saves CSV to data/)
python scraper_runner.py

# 4. Start the admin portal
python app.py
# Open http://localhost:5000
```

## Scraper CLI Usage

```bash
# All counties
python scraper_runner.py

# One or more specific counties
python scraper_runner.py --county davidson williamson

# Custom output path
python scraper_runner.py --output my_output.csv
```

## Admin Portal Features

- **Sortable columns** — click any column header
- **Filter by county** — dropdown
- **Filter by record type** — Tax Delinquent / Pre-Foreclosure
- **Full-text search** — searches all fields
- **CSV download** — download filtered or full CSV
- **In-browser scrape trigger** — click "Run Scraper", select counties, runs in background
- **Multiple CSV files** — switch between historical runs

## Notes on Data Sources

### Tax Delinquent Records
County chancery courts post delinquent tax sale lists (often as PDFs or HTML tables) before each auction. The scraper:
1. Parses any HTML tables it finds on sale schedule pages
2. Logs downloadable PDF/Excel list links as records so you can access them directly

### Pre-Foreclosure (Lis Pendens)
Filed at each county's Register of Deeds when a lender initiates foreclosure. The scraper attempts to query deed search portals. Where APIs are not publicly accessible, it logs the direct portal URL with search instructions.

For maximum coverage on pre-foreclosures, supplement this tool with:
- **Tennessee Secretary of State** UCC/lien search
- **PACER** (federal court filings)
- Third-party aggregators like PropertyRadar, ATTOM, or PropStream for bulk Lis Pendens data
