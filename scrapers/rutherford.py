"""
Rutherford County scraper.

Tax Delinquent:
  Chancery court delinquent-tax search (rcchancery.com/tax-bill-receipts),
  a Catalis Wildfire portal — see scrapers/catalis_wildfire.py for the API.
  This county's portal only carries unpaid (in-court delinquent) bills.

Pre-Foreclosure: rodweb.rutherfordcountytn.gov
"""
from datetime import date

from .base_scraper import BaseScraper, PropertyRecord, log
from .catalis_wildfire import fetch_wildfire_delinquents

SEARCH_URL = "https://rcchancery.com/tax-bill-receipts"
WILDFIRE_CLIENT = "ca0a6090-72c1-4f8b-9668-ab999f19e875"
ROD_URL = "https://rodweb.rutherfordcountytn.gov/"


class RutherfordScraper(BaseScraper):
    county_name = "Rutherford"
    base_url = SEARCH_URL

    def scrape(self) -> list[PropertyRecord]:
        records = []
        records += self._scrape_tax_delinquent()
        records += self._scrape_preforeclosures()
        return records

    def _scrape_tax_delinquent(self) -> list[PropertyRecord]:
        today = str(date.today())
        log.info(f"[{self.county_name}] Querying Wildfire delinquent-tax API...")
        records = fetch_wildfire_delinquents(
            self, WILDFIRE_CLIENT,
            origin="https://rcchancery.com",
            default_city="Murfreesboro",
            source_url=SEARCH_URL,
        )
        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"Wildfire API returned no records. Visit {SEARCH_URL}",
                source_url=SEARCH_URL,
                scraped_date=today,
                state="TN",
                city="Murfreesboro",
            ))
        log.info(f"[{self.county_name}] Tax delinquent records: {len(records)}")
        return records

    def _scrape_preforeclosures(self) -> list[PropertyRecord]:
        today = str(date.today())
        return [PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            notes=f"Search Lis Pendens at {ROD_URL} - Register of Deeds. Filter doc type 'LIS PENDENS'.",
            source_url=ROD_URL,
            scraped_date=today,
            state="TN",
            city="Murfreesboro",
        )]
