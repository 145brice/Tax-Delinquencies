# Tax Delinquency & Pre-Foreclosure Scraper

Scrapes county-level **tax delinquent properties** and **pre-foreclosure / trustee-sale** records, exports to CSV, and serves a sortable/filterable admin portal.

## Counties Covered

### Nashville TN region
| County | County Seat | Sources |
|--------|-------------|---------|
| Davidson (Nashville) | Nashville | Chancery Clerk tax sale page + Register of Deeds Lis Pendens |
| Williamson | Franklin | County delinquent tax page + Register of Deeds |
| Rutherford | Murfreesboro | RC Chancery Court delinquent tax page |
| Wilson | Lebanon | County Chancery Court + Trustee |
| Sumner | Gallatin | County Chancery Court + Trustee |
| Robertson | Springfield | County website + Trustee |
| Cheatham | Ashland City | County website + Trustee |

### San Diego CA region
| Source | Scraper key | What it returns |
|--------|-------------|-----------------|
| SDTTC Prior Sale Results (sdttc.mytaxsale.com) | `sandiego_taxsale` | Most recent tax-sale auction parcels: APN, sale date, opening/winning bid |
| CA Public Notice — Notice of Trustee Sale (capublicnotice.com) | `sandiego_legalnotices` | Last ~120 days of San Diego County trustee-sale notices: TS#, publication, post date, description preview |

Upcoming SD tax-sale parcel lists are gated behind a registered bidder account; the scraper emits a single record linking to the auction portal for those. Auction.com listings are JS-only and not yet supported.

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
# Open http://localhost:8095
```

## Production Setup: Vercel Storefront Only

Vercel serves only the buyer-facing storefront. Admin, Data Explorer, scraper
controls, raw listings JSON, and CSV exports are disabled automatically on
Vercel.

Run the Python app locally for scraping and data work:

```bash
python app.py
# Open http://127.0.0.1:8095/admin
```

Local scraper/admin state is stored in SQLite at `foreclosure_local.sqlite3`
by default. Set `SQLITE_DB=path/to/file.sqlite3` if you want a different local
database path.

The local app also writes one deploy artifact after each scraper save:

```text
data/storefront_listings.csv
```

That CSV is sorted in the same deterministic order the storefront uses:
county, status, city, date, owner, parcel/APN, then address. Push that file to
update the Vercel storefront data. Other run/audit CSVs stay ignored so
`git add .` does not accidentally publish scratch data.

## Scraper CLI Usage

```bash
# All counties (Nashville + San Diego)
python scraper_runner.py

# One or more specific counties
python scraper_runner.py --county davidson williamson

# San Diego only — output goes to data/sandiego_<date>.csv
python scraper_runner.py --county sandiego_taxsale sandiego_legalnotices

# Custom output path
python scraper_runner.py --output my_output.csv

# Respect the same search-depth concept used by the admin slider
python scraper_runner.py --county riverside_legalnotices --lookback-days 90
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
