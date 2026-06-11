"""Barry County (MI) — delinquent-tax foreclosure auction via tax-sale.info.

The county treasurer's tax-foreclosed parcels are auctioned through Title Check
LLC (tax-sale.info). Each auction has a per-county catalog with a downloadable
CSV. The yearly catalog id changes, so we discover Barry's current id from the
auctions index (an anchor like `/listings/catalog/2833">Barry<`) and fall back
to a configurable default if the page layout changes.

These are post-foreclosure parcels the county now owns and is auctioning — a
distinct property set from the mortgage/sheriff notices, so the two Barry
sources do not overlap.
"""
import csv
import io
import os
import re
from datetime import date

from .base_scraper import BaseScraper, PropertyRecord, log

AUCTIONS_URL = "https://www.tax-sale.info/auctions"
CSV_URL = "https://www.tax-sale.info/catalog/getCsv/id/{cid}"
CATALOG_URL = "https://www.tax-sale.info/listings/catalog/{cid}"
_CATALOG_RE = re.compile(r'/listings/catalog/(\d+)"\s*>\s*Barry\s*<', re.I)
DEFAULT_CATALOG_ID = os.getenv("BARRY_TAXSALE_CATALOG_ID", "2833")


class BarryTaxForeclosureScraper(BaseScraper):
    county_name = "Barry"
    base_url = AUCTIONS_URL

    def _catalog_id(self) -> str:
        """Find Barry's current catalog id from the auctions index."""
        resp = self.get(AUCTIONS_URL)
        if resp:
            m = _CATALOG_RE.search(resp.text)
            if m:
                return m.group(1)
            log.warning(f"[{self.county_name}] Barry catalog link not found on "
                        f"auctions page; using default {DEFAULT_CATALOG_ID}")
        return DEFAULT_CATALOG_ID

    def scrape(self) -> list[PropertyRecord]:
        today = str(date.today())
        cid = self._catalog_id()
        catalog_url = CATALOG_URL.format(cid=cid)

        resp = self.get(CSV_URL.format(cid=cid))
        if not resp or not resp.text.strip():
            return [self._stub(today, catalog_url)]

        records: list[PropertyRecord] = []
        reader = csv.DictReader(io.StringIO(resp.text))
        for row in reader:
            street, city = self._split_address((row.get("Address") or "").strip())

            min_bid = (row.get("Minimum Bid") or "").strip()
            amount = f"${min_bid}" if min_bid and not min_bid.startswith("$") else min_bid

            township = (row.get("Township") or "").strip()
            local_unit = (row.get("Local Unit") or "").strip()
            legal = (row.get("Legal Description") or "").strip()

            note_parts = []
            lot = (row.get("Lot Number") or "").strip()
            if lot:
                note_parts.append(f"Lot {lot}")
            if local_unit:
                note_parts.append(local_unit)
            sev = (row.get("SEV") or "").strip()
            if sev:
                note_parts.append(f"SEV {sev}")
            if legal:
                note_parts.append(f"Legal: {legal[:200]}")

            records.append(PropertyRecord(
                county="Barry",
                record_type="Tax Delinquent",
                property_address=street,
                city=city or local_unit.replace("TOWNSHIP", "").strip().title(),
                state="MI",
                parcel_id=(row.get("Parcel Id") or "").strip(),
                amount_owed=amount,
                source_url=catalog_url,
                scraped_date=today,
                notes=" | ".join(note_parts),
            ))

        log.info(f"[{self.county_name}] Tax-foreclosure catalog {cid}: "
                 f"{len(records)} parcels")

        if not records:
            records.append(self._stub(today, catalog_url))
        return records

    @staticmethod
    def _split_address(address: str) -> tuple[str, str]:
        """CSV 'Address' packs street + city like '5959 LACEY RD  BELLEVUE'.

        Street and city are separated by 2+ spaces; return (street, City).
        """
        if not address:
            return "", ""
        parts = re.split(r"\s{2,}", address)
        if len(parts) >= 2:
            return parts[0].strip(), parts[-1].strip().title()
        return address, ""

    def _stub(self, today: str, catalog_url: str) -> PropertyRecord:
        return PropertyRecord(
            county="Barry",
            record_type="Tax Delinquent",
            state="MI",
            notes="No Barry County MI tax-foreclosure parcels available yet "
                  "(catalogs typically post June–July). "
                  f"Browse at {catalog_url}",
            source_url=catalog_url,
            scraped_date=today,
        )
