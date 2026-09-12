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
data/                      Raw scraper CSVs + masked storefront export
```

How a source is wired (each new county touches these):
1. A scraper class in `scrapers/<county>_<type>.py` returning `PropertyRecord`s.
2. A `SourceDefinition` entry in `source_registry.py` `SOURCES` (+ region).
3. A UI key in `UI_COUNTY_SOURCES` mapping `<county>-<state>` → source keys.
4. (Local UI only) a checkbox in `templates/admin.html`; optional label in `index.html`.

The two record types are `Tax Delinquent` and `Pre-Foreclosure`. For a county
with both a tax-foreclosure and a mortgage/sheriff source, keep them as separate
sources so the property sets do not overlap (e.g. Barry MI).

## Production: Railway

The storefront, accounts, admin portal, and fulfillment worker run on Railway.
The connected production branch is `main`. This project does not use Vercel.

Mount a persistent volume at `/data`. Leave `SQLITE_DB` unset to use
`/data/foreclosure.sqlite3`, or point it to a file inside the mounted volume.
The app refuses purchases and fails its health check if hosted storage is not
persistent. Runtime data is seeded only when missing, never overwritten by a
new deployment. Back up the volume before the first upgrade.

`railway.json` uses one replica and disables App Sleep so paid delivery and
skip-trace retries continue without a browser visit. `serve.py` runs Waitress.
The SQLite volume is the authority for listings, lead reservations, wallet
balances, subscription allowances, promo claims, and the recovery journal.
Appwrite (or Postgres) remains the account and purchased-order backend.

Every checkout reserves its leads before a payment URL is returned. Wallet
debits and reservations commit in one transaction; external order delivery is
retried from the journal. Unpaid card reservations are released only after
Stripe confirms expiration. Paid claims remain exclusive across re-scrapes.
Existing paid orders are imported into the claim registry before new purchases.

Admin authentication uses `ADMIN_TOKEN` (or `SKIPTRACE_ADMIN_TOKEN`) or an
explicit comma-separated `ADMIN_USER_IDS` list. Email addresses alone never
grant an admin role. Existing email-based admins can sign in with their admin
token and configure their immutable account IDs. Logout clears all privileges;
rotating the admin token revokes remembered token access.

County subscriptions include raw leads and 8/25/80 skip traces per billing
period. Additional skip traces charge the age-based skip/raw price difference
from wallet credit, shown for confirmation before purchase. Failed included
traces restore the slot in the same billing period; paid failed upgrades credit
only the actual upgrade charge. Retries do not consume another slot.

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

## Account and payment configuration

Set these in Railway service variables (and `.env` for local development):

```text
APPWRITE_ENDPOINT=https://nyc.cloud.appwrite.io/v1
APPWRITE_PROJECT_ID=<project id>
APPWRITE_API_KEY=<server api key>
APPWRITE_DATABASE_ID=tax_delinquencies
APPWRITE_USERS_COLLECTION_ID=users
APPWRITE_ORDERS_COLLECTION_ID=orders
SECRET_KEY=<long random session secret>
ADMIN_TOKEN=<long random admin token>
STRIPE_SECRET_KEY=<Stripe secret key>
STRIPE_WEBHOOK_SECRET=<Stripe webhook signing secret>
```

Alternatively, configure `DATABASE_URL` for the Postgres account/order backend.
Keep credentials out of source control. Configure Stripe's webhook for
`/webhook/stripe`, including checkout completion, expiration, subscription
updates/deletion, and paid invoices. Failed fulfillment returns HTTP 503 so
Stripe can retry. The durable worker also reconciles journaled purchases.

### Publishing inventory

The live storefront reads the volume's SQLite listings, seeded from
`listings.json` only on first use. Scraping through admin or the scheduled
ingestion API updates that inventory immediately. Deploying a changed CSV
does not replace live inventory or erase sold markers.

To publish an existing **raw scraper CSV**, set `SCRAPE_TARGET_URL` and
`ADMIN_TOKEN`, then run:

```bash
python scripts/publish_csv.py data/raw_run.csv --county duval-fl --source duval_jaxdailyrecord
```

Use a county/source pair from `scrapers/source_registry.py`. The publisher
validates raw columns and sends batches to the authenticated ingestion API.
`data/storefront_listings.csv` is a masked export for inspection, not purchase
inventory; the publisher rejects it. Other run/audit CSVs remain ignored.

### Validation

```bash
python -m unittest discover -s tests -v
```

Tests import the real app with temporary storage and fake Stripe/account
services. They never charge cards or mutate production data.

### Scheduled scraper jobs

Scheduled scrapes run as one short-lived GitHub Actions job per county. The
hourly planner in `.github/workflows/scheduled-scrape.yml` selects only counties
whose local posting window has just closed, starts isolated county jobs, and
limits concurrency to four. Each job retries a failed source once, sends the
completed records to `/api/scrape/ingest`, and exits. Posting windows and source
assignments live in `scripts/county_schedule.json`.

Set `EXTERNAL_SCRAPER_JOBS=true` on the hosted API to disable its old in-process
scrape endpoint. Keep App Sleep disabled because this service also owns
paid-order recovery. The repository Actions secrets
`SCRAPE_TARGET_URL` and `ADMIN_TOKEN` must match the hosted API configuration.

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
