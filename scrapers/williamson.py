"""
Williamson County scraper.

Tax Delinquent:
  Chancery court delinquent-tax search (williamsonchancery.org/taxes.html),
  a Catalis Wildfire portal — see scrapers/catalis_wildfire.py for the API.

Pre-Foreclosures: Williamson County Register of Deeds
  https://www.williamsoncounty-tn.gov/323/Register-of-Deeds
"""
from datetime import date

from .base_scraper import BaseScraper, PropertyRecord, log
from .catalis_wildfire import fetch_wildfire_delinquents

DELINQUENT_URL = "https://www.williamsonchancery.org/taxes.html"
WILDFIRE_CLIENT = "b2d7cab9-cd29-433f-ad7e-3c565a6184b5"
ROD_URL = "https://www.williamsoncounty-tn.gov/323/Register-of-Deeds"
SHERIFF_SALES_URL = "https://williamsoncountysherifftn.com/services/sheriff-sales/"


class WilliamsonScraper(BaseScraper):
    county_name = "Williamson"
    base_url = DELINQUENT_URL

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
            origin="https://www.williamsonchancery.org",
            default_city="Franklin",
            source_url=DELINQUENT_URL,
        )
        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"Wildfire API returned no records. Visit {DELINQUENT_URL}",
                source_url=DELINQUENT_URL,
                scraped_date=today,
                state="TN",
                city="Franklin",
            ))
        log.info(f"[{self.county_name}] Tax delinquent records: {len(records)}")
        return records

    def _scrape_preforeclosures(self) -> list[PropertyRecord]:
        today = str(date.today())
        return [PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            notes=f"Search Lis Pendens at {ROD_URL} — Register of Deeds portal.",
            source_url=ROD_URL,
            scraped_date=today,
            state="TN",
            city="Franklin",
        )]
