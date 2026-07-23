"""
Harris County TX - Delinquent Property Tax Sales via Harris County Tax Office.

Source: hctax.net/Property/listings/taxsalelisting
Auction held first Tuesday of each month at Bayou City Event Center.
Listing page shows ~100-150 properties per sale. Each property has a detail
page with the owner name and street address.

Approach:
  1. Fetch the main listing page to extract all HCAD account numbers plus
     available metadata (cause#, precinct, min bid, adjudged value).
  2. Fetch each detail page to get owner name and full street address.
     Detail fetches are capped at MAX_DETAIL_FETCHES to keep runtime reasonable.
"""
import os
import re
from datetime import date

from .base_scraper import BaseScraper, PropertyRecord, log

LISTING_URL = "https://www.hctax.net/Property/listings/taxsalelisting"
DETAIL_URL  = "https://www.hctax.net/property/listings/saledetail"

MAX_DETAIL_FETCHES = int(os.getenv("HARRIS_MAX_DETAILS", "200"))

_ACCT_RE   = re.compile(r"(?:account[#:\s=]+|account\s*[=])\s*<[^>]*>?\s*(\d{10,})", re.I)
_ACCT_TEXT = re.compile(r"Account#[:\s]+(\d{10,})", re.I)
_ACCT_HREF = re.compile(r"saledetail\?account=(\d+)", re.I)
_SALE_DATE = re.compile(r"Sale Date[:\s]+([A-Za-z]+ \d{1,2},?\s*\d{4})", re.I)
_ADDR_RE   = re.compile(r"(\d+\s+\S+(?:\s+\S+){1,6})\s+(HOUSTON|HARRIS|SPRING|PEARLAND|KATY|HUMBLE|PASADENA|BAYTOWN|FRIENDSWOOD|LEAGUE CITY|GALENA PARK|DEER PARK|LA PORTE|SEABROOK|JERSEY VILLAGE|BELLAIRE|SOUTH HOUSTON|WEST UNIVERSITY PLACE|Missouri CITY)\s+TX\s+(\d{5})", re.I)


def _clean(v: str) -> str:
    return re.sub(r"\s+", " ", (v or "")).strip(" ,.;")


class HarrisTaxSaleScraper(BaseScraper):
    county_name = "Harris"
    base_url = LISTING_URL

    def scrape(self) -> list[PropertyRecord]:
        today = str(date.today())
        records: list[PropertyRecord] = []

        resp = self.get(LISTING_URL)
        if not resp:
            return [self._stub(today, "Could not fetch Harris County tax sale listing page.")]

        html = resp.text
        soup = self.soup(html)

        # Extract sale date from page header
        sale_date = ""
        m = _SALE_DATE.search(html)
        if m:
            sale_date = _clean(m.group(1))

        # Each property is represented by a pair of tables.
        # Even-index table: 2-row header with account link.
        # Odd-index table: 10-row detail (precinct, sale#, type, cause#, judgment,
        #                   tax years, min bid, adjudged value, HCAD link, detail link).
        tables = soup.find_all("table")
        properties: list[dict] = []

        for i, tbl in enumerate(tables):
            rows = tbl.find_all("tr")
            if len(rows) != 10:
                continue
            cells = {
                tr.find_all(["td", "th"])[0].get_text(" ", strip=True): (
                    tr.find_all(["td", "th"])[1].get_text(" ", strip=True)
                    if len(tr.find_all(["td", "th"])) > 1 else ""
                )
                for tr in rows
                if len(tr.find_all(["td", "th"])) >= 1
            }

            # Pull account number from the preceding 2-row header table
            acct = ""
            if i > 0:
                prev_str = str(tables[i - 1])
                for pat in (_ACCT_HREF, _ACCT_TEXT, _ACCT_RE):
                    m = pat.search(prev_str)
                    if m:
                        acct = m.group(1).strip()
                        break
                # Also check in the detail links within this 10-row table
                if not acct:
                    m = _ACCT_HREF.search(str(tbl))
                    if m:
                        acct = m.group(1).strip()

            precinct   = _clean(cells.get("Precinct:", ""))
            sale_no    = _clean(cells.get("Sale#:", ""))
            rec_type   = _clean(cells.get("Type:", ""))
            cause_no   = _clean(cells.get("Cause#:", ""))
            judgment   = _clean(cells.get("Judgment:", ""))
            tax_years  = _clean(cells.get("Tax Years in Judgement:", ""))
            min_bid    = _clean(cells.get("Minimum Bid:", ""))
            adj_value  = _clean(cells.get("Adjudged Value:", ""))

            if not cause_no:
                continue

            properties.append({
                "acct": acct,
                "precinct": precinct,
                "sale_no": sale_no,
                "rec_type": rec_type,
                "cause_no": cause_no,
                "judgment": judgment,
                "tax_years": tax_years,
                "min_bid": min_bid,
                "adj_value": adj_value,
            })

        log.info(f"[{self.county_name}] Listing page: {len(properties)} properties found")

        # Fetch detail pages for addresses/owners (capped)
        fetched = 0
        for prop in properties:
            acct = prop["acct"]
            address = owner = city = zip_code = ""

            if acct and fetched < MAX_DETAIL_FETCHES:
                detail_resp = self.get(DETAIL_URL, params={"account": acct})
                if detail_resp:
                    fetched += 1
                    detail_text = detail_resp.text
                    detail_soup = self.soup(detail_text)
                    detail_lines = [
                        ln.strip()
                        for ln in detail_soup.get_text("\n", strip=True).split("\n")
                        if ln.strip()
                    ]

                    # Detail page layout (after "Tax Sale Property Information"):
                    # Header row:  Account Number | Current As Of: | Assessed Owner | Property Description
                    # Data row:    061-039-000-0020 | date | OWNER NAME | address line
                    # City/zip:    HOUSTON TX  77018
                    try:
                        idx = next(
                            j for j, l in enumerate(detail_lines)
                            if "Tax Sale Property Information" in l
                        )
                        # Skip 4 column-header lines and find data lines
                        # After "Tax Sale Property Information" comes: "Back", then 4 col headers,
                        # then formatted acct#, date, owner, address, city-state-zip
                        block = detail_lines[idx + 1 : idx + 15]
                        # Find the formatted account number line (NNN-NNN-NNN-NNNN pattern)
                        acct_line_idx = next(
                            (j for j, l in enumerate(block) if re.match(r"\d{3}-\d{3}-\d{3}-\d{4}", l)),
                            None
                        )
                        if acct_line_idx is not None:
                            after = block[acct_line_idx + 1:]  # date, owner, address, city-zip
                            # skip the date line (starts with digit/month)
                            data = [l for l in after if l.strip() and not re.match(r"\d+/\d+/\d{4}", l)]
                            if data:
                                owner = _clean(data[0]).title()
                            if len(data) > 1:
                                address = _clean(data[1])
                            if len(data) > 2:
                                cz = _clean(data[2])
                                cz_m = re.match(r"([A-Za-z ]+?)\s+TX\s+(\d{5})", cz, re.I)
                                if cz_m:
                                    city     = cz_m.group(1).strip().title()
                                    zip_code = cz_m.group(2)
                    except StopIteration:
                        pass

                    if not owner:
                        # Fallback regex on raw text
                        owner_m = re.search(
                            r"Assessed Owner\s*\n\s*(.+?)\n",
                            detail_text, re.I
                        )
                        if owner_m:
                            owner = _clean(owner_m.group(1)).title()

            note_parts = []
            if prop["precinct"]:
                note_parts.append(prop["precinct"])
            if prop["tax_years"]:
                note_parts.append(f"Tax years: {prop['tax_years']}")
            if prop["judgment"]:
                note_parts.append(f"Judgment: {prop['judgment']}")
            if prop["adj_value"]:
                note_parts.append(f"Adjudged value: {prop['adj_value']}")
            if prop["cause_no"]:
                note_parts.append(f"Cause#: {prop['cause_no']}")

            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                owner_name=owner,
                property_address=address,
                city=city or "Houston",
                state="TX",
                zip_code=zip_code,
                parcel_id=prop["acct"],
                case_number=prop["cause_no"],
                amount_owed=prop["min_bid"],
                sale_date=sale_date,
                source_url=LISTING_URL,
                scraped_date=today,
                notes=" | ".join(note_parts),
            ))

        log.info(
            f"[{self.county_name}] Tax sale: {len(records)} records "
            f"({fetched} detail pages fetched)"
        )

        if not records:
            return [self._stub(today, "No properties found on Harris County tax sale listing page.")]
        return records

    def _stub(self, today: str, msg: str = "") -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type="Tax Delinquent",
            notes=msg or "No Harris County tax sale properties found.",
            source_url=LISTING_URL,
            scraped_date=today,
            city="Houston",
            state="TX",
        )

