"""
Shared client for Catalis "Wildfire" county tax-search portals.

Several TN chancery courts host their delinquent-tax search on this platform
(one CloudFront distribution, per-county client GUIDs). The Angular front end
calls a JSON API we can use directly:

  POST https://d1ebsyxxbc7tep.cloudfront.net/data/<client-guid>/Wildfire/Records
  body: {"value": "", "skip": N, "facets": {...}, "direct": false}

Gotchas discovered the hard way:
  - The API returns an HTML shell unless the request has Accept: application/json.
  - Pagination past page 1 401s unless the page-1 SearchToken is echoed back
    in a `searchtoken` request header.
"""
import time
from datetime import date

from .base_scraper import PropertyRecord, ScraperKilled, is_kill_requested, log

WILDFIRE_DATA_BASE = "https://d1ebsyxxbc7tep.cloudfront.net/data"

PAGE_SIZE = 20
MAX_RECORDS = 5000


def _situs_to_address(situs: dict) -> str:
    """Wildfire situs lines come as 'STREET NAME 123' — move the trailing
    house number to the front so it reads like a normal address."""
    import re
    line = (situs.get("Line1") or "").strip()
    m = re.match(r"^(.*?)\s+(\d+[A-Z]?)$", line)
    if m:
        return f"{m.group(2)} {m.group(1)}"
    return line


def fetch_wildfire_delinquents(scraper, client_guid: str, origin: str,
                               default_city: str, source_url: str) -> list[PropertyRecord]:
    """Fetch all Unpaid + Real Property records for one county portal.

    `scraper` is the calling BaseScraper (for its session and county_name).
    Paginates the CDN-backed API directly with a light delay — the BaseScraper
    polite gap (3-7s/request) would make ~150 pages take 10+ minutes.
    """
    county = scraper.county_name
    today = str(date.today())
    url = f"{WILDFIRE_DATA_BASE}/{client_guid}/Wildfire/Records"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": origin,
        "Referer": origin + "/",
    }
    facets = {"Status": {"Unpaid": True}, "Type": {"Real Property": True}, "Years": {}}

    records: list[PropertyRecord] = []
    search_token = ""
    skip = 0
    total = None

    while total is None or (skip < total and skip < MAX_RECORDS):
        if is_kill_requested():
            raise ScraperKilled("Kill requested during Wildfire pagination")
        req_headers = dict(headers)
        if search_token:
            req_headers["searchtoken"] = search_token
        data = None
        for attempt in range(3):
            try:
                resp = scraper.session.post(
                    url,
                    json={"value": "", "skip": skip, "facets": facets, "direct": False},
                    headers=req_headers, timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                log.warning(f"[{county}] Wildfire page skip={skip} attempt {attempt+1}: {e}")
                time.sleep(2 * (attempt + 1))
        if data is None:
            break
        search_token = data.get("SearchToken") or search_token
        if total is None:
            total = int(data.get("TotalRecords") or 0)
            log.info(f"[{county}] Wildfire: {total} unpaid real-property records")
        page = data.get("Records") or []
        if not page:
            break
        for rec in page:
            situs = rec.get("SitusAddress") or {}
            values = rec.get("Values") or {}
            owner = " ".join(p for p in [rec.get("OwnerName1", ""), rec.get("OwnerName2", "")] if p).strip()
            address = _situs_to_address(situs)
            # Never fall back to the owner's mailing city — out-of-area owners
            # (the best leads) would get their mailing city stamped on the
            # property. The county seat is the safer default.
            city = (situs.get("City") or default_city).strip().title()
            zip_code = (situs.get("Zip") or "").strip()
            amount = values.get("AmountDue")
            parcel = ((rec.get("MapNumber") or {}).get("Unformatted") or "").strip()
            if not (owner or parcel or address):
                continue
            records.append(PropertyRecord(
                county=county,
                record_type="Tax Delinquent",
                owner_name=owner,
                property_address=address,
                city=city,
                state="TN",
                zip_code=zip_code,
                parcel_id=parcel,
                tax_year=str(rec.get("Year") or ""),
                amount_owed=f"${amount:,.2f}" if isinstance(amount, (int, float)) and amount else "",
                source_url=source_url,
                scraped_date=today,
                notes=f"Appraised value: ${values.get('TotalValue'):,}" if values.get("TotalValue") else "",
            ))
        skip += PAGE_SIZE
        time.sleep(0.4)

    return records
