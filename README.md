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

> The tables above are a partial snapshot. **`scrapers/source_registry.py` is the
> single source of truth** for which counties/sources exist. Current coverage
> also includes: CA (Los Angeles, Orange, Riverside, San Bernardino, Ventura,
> Sacramento, Alameda, Santa Clara, Kern, Fresno, Contra Costa, San Mateo),
> AZ (Maricopa — trustee sales + two legal-notice papers), TX (Harris tax sale),
> TX major metros (Harris, Collin, Bexar), NV (Clark/Las Vegas sheriff sales),
> FL major metros (Miami-Dade, Broward, Palm Beach, Orange, Hillsborough) plus
> northeast Florida (Duval/St. Johns/Nassau/Clay via Jax Daily Record), and
> **MI (26 counties via statewide foreclosure notices, plus Barry tax auctions)**.

## Repository Structure

```text
app.py                     Flask app: storefront + admin portal + scrape API
scraper_runner.py          CLI/entry that runs registry scrapers and writes CSV
scraper.py                 Legacy Playwright scrapers (Davidson/Wilson/HUD)
scrapers/
  base_scraper.py          BaseScraper: polite HTTP, UA rotation, PDF/OCR, PropertyRecord
  ca_legalnotices_base.py  Shared base for CA capublicnotice.com county scrapers
  source_registry.py       SOURCES + UI_COUNTY_SOURCES — the source of truth
  <county>_*.py            One module per county/source scraper class
templates/                 Jinja templates (index = storefront; admin = local-only)
data/                      Output CSVs + storefront_listings.csv deploy artifact
```

How a source is wired (each new county touches these):
1. A scraper class in `scrapers/<county>_<type>.py` returning `PropertyRecord`s.
2. A `SourceDefinition` entry in `source_registry.py` `SOURCES` (+ region).
3. A UI key in `UI_COUNTY_SOURCES` mapping `<county>-<state>` → source keys.
4. (Local UI only) a checkbox in `templates/admin.html`; optional label in `index.html`.

The two record types are `Tax Delinquent` and `Pre-Foreclosure`. For a county
with both a tax-foreclosure and a mortgage/sheriff source, keep them as separate
sources so the property sets do not overlap (e.g. Barry MI).

## Branching & Deployment Model

| Branch | Role | Deploys to |
|--------|------|------------|
| `master` | Working branch — where development is committed | nothing (no auto-deploy) |
| `main` | Production storefront branch | **Vercel** (auto-deploy on push) |

- **Vercel is storefront-only.** `app.py` sets `STOREFRONT_ONLY = IS_VERCEL`,
  where `IS_VERCEL` is auto-detected from the `VERCEL`/`VERCEL_ENV` env vars.
  Admin, Data Explorer, scraper controls, raw listings JSON, and CSV exports are
  disabled automatically when running on Vercel. Nothing you push can flip this —
  it is environment-detected, not committed.
- **Scrapers/admin run locally** (and, once stood up, on **Railway**). The
  registry can grow freely without affecting the live store.
- **Updating the store** = push `main` with a refreshed `data/storefront_listings.csv`.
  Pushing only `master` lands code in the repo and leaves Vercel untouched.

### Push playbook

```bash
# Land backend/scraper work without redeploying the store:
git add scrapers/...            # only the coherent code set
git commit -m "..."
git push origin master          # main/Vercel untouched

# Update the live store (when ready):
git checkout main && git merge master   # or cherry-pick
git push origin main            # triggers Vercel deploy
```

When committing the registry, **include every scraper it imports** — a clean
checkout (e.g. a Vercel build) imports `source_registry.py` at startup, so a
missing module crashes the deploy even though Vercel never runs the scrapers.

### Push history

- **2026-06-10** — Added Barry County MI (tax-foreclosure auction via
  tax-sale.info + Hastings Banner foreclosure notices via mipublicnotices.com).
  Also committed previously-untracked CA/AZ/TX scrapers the registry already
  imported, so the import resolves on a clean checkout. **Pushed to `master`
  only; `main`/Vercel left untouched** (rest of the stack stays local until
  Railway is up). Commit `6795239`.

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

Buyer accounts, saved purchases, and Stripe unlock records are stored in
Appwrite Databases. Configure these environment variables locally and in
Vercel:

```text
APPWRITE_ENDPOINT=https://nyc.cloud.appwrite.io/v1
APPWRITE_PROJECT_ID=<project id>
APPWRITE_API_KEY=<server api key>
APPWRITE_DATABASE_ID=tax_delinquencies
APPWRITE_USERS_COLLECTION_ID=users
APPWRITE_ORDERS_COLLECTION_ID=orders
SECRET_KEY=<long random flask session secret>
STRIPE_SECRET_KEY=<stripe secret key>
STRIPE_WEBHOOK_SECRET=<stripe webhook secret>
```

The app creates the `tax_delinquencies` database plus `users` and `orders`
collections if they do not exist. Keep API keys in `.env` or Vercel env vars;
never commit them.

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
