"""
Orange County (CA) — Tax-defaulted property sales.

Same MyTaxSale.com platform as San Diego. Scrapes the public prior-sale
results table at octaxauction.mytaxsale.com/reports/total_sales.
"""
from datetime import date
from .base_scraper import BaseScraper, PropertyRecord, log

PRIOR_SALES_URL = "https://octaxauction.mytaxsale.com/reports/total_sales"
AUCTION_PORTAL_URL = "https://octaxauction.mytaxsale.com/"


class OrangeTaxSaleScraper(BaseScraper):
    county_name = "Orange"
    base_url = PRIOR_SALES_URL

    def scrape(self) -> list[PropertyRecord]:
        today = str(date.today())
        records: list[PropertyRecord] = []

        records.append(PropertyRecord(
            county=self.county_name,
            record_type="Tax Delinquent",
            notes="Upcoming auction parcel list is gated behind a registered "
                  "bidder account. Register at the auction portal to download.",
            source_url=AUCTION_PORTAL_URL,
            scraped_date=today,
            city="Santa Ana",
            state="CA",
        ))

        log.info(f"[{self.county_name}] Fetching prior sales table...")
        resp = self.get(PRIOR_SALES_URL)
        if not resp:
            return records

        soup = self.soup(resp.text)
        table = soup.find("table")
        if not table:
            log.warning(f"[{self.county_name}] No results table found.")
            return records

        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else []
        for tr in rows:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 5:
                continue
            deed_id, apn, sale_date, opening_bid, winning_bid = cells[:5]
            note = cells[5] if len(cells) > 5 else ""
            if not apn:
                continue

            note_parts = [f"Deed#: {deed_id}", f"Opening bid: {opening_bid}"]
            if winning_bid:
                note_parts.append(f"Winning bid: {winning_bid}")
            if note:
                note_parts.append(note)

            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                parcel_id=apn,
                amount_owed=winning_bid or opening_bid,
                sale_date=sale_date,
                notes=" | ".join(note_parts),
                source_url=PRIOR_SALES_URL,
                scraped_date=today,
                city="Santa Ana",
                state="CA",
            ))

        log.info(f"[{self.county_name}] Tax sale records: {len(records)}")
        return records
