"""
Sumner County scraper.

Tax Delinquent:
  Chancery Court page has a direct PDF link to the current tax sale list:
  http://sumnerchancerycourt.com/wp-content/uploads/2025/01/TAX-SALE-LIST-Updated-1-7-25.pdf
  We scrape the page to find the most current PDF, then parse it.

Pre-Foreclosure: Sumner County Chancery Court
"""
import re
from datetime import date
from .base_scraper import BaseScraper, PropertyRecord, log

CHANCERY_URL = "https://sumnerchancerycourt.com/delinquent-taxes/"
ROD_URL = "https://sumnerchancerycourt.com/"

# Known current sale list PDF — scraper also auto-discovers newer ones
KNOWN_PDF = "http://sumnerchancerycourt.com/wp-content/uploads/2025/01/TAX-SALE-LIST-Updated-1-7-25.pdf"


class SumnerScraper(BaseScraper):
    county_name = "Sumner"
    base_url = CHANCERY_URL

    def scrape(self) -> list[PropertyRecord]:
        records = []
        records += self._scrape_tax_delinquent()
        records += self._scrape_preforeclosures()
        return records

    def _scrape_tax_delinquent(self) -> list[PropertyRecord]:
        today = str(date.today())
        records = []

        # Discover PDFs from the page
        pdf_urls = [KNOWN_PDF]
        resp = self.get(CHANCERY_URL)
        if resp:
            soup = self.soup(resp.text)
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href.lower() and ("tax" in href.lower() or "sale" in href.lower() or "list" in href.lower()):
                    if href not in pdf_urls:
                        pdf_urls.append(href)

        log.info(f"[{self.county_name}] Parsing {len(pdf_urls)} PDF(s)...")
        for pdf_url in pdf_urls:
            recs = self._parse_sale_pdf(pdf_url, today)
            if recs:
                records.extend(recs)
                log.info(f"[{self.county_name}] {pdf_url.split('/')[-1]} -> {len(recs)} records")

        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"No parseable sale list found. Visit {CHANCERY_URL}",
                source_url=CHANCERY_URL,
                scraped_date=today,
                city="Gallatin",
                state="TN",
            ))

        log.info(f"[{self.county_name}] Tax delinquent records: {len(records)}")
        return records

    def _parse_sale_pdf(self, pdf_url: str, today: str) -> list[PropertyRecord]:
        records = []
        text = self.pdf_text_auto(pdf_url)
        if not text.strip():
            return records

        sale_date = ""
        m = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}', text)
        if m:
            sale_date = m.group(0)

        lines = text.split("\n")
        for line in lines:
            stripped = line.strip()
            if not stripped or len(stripped) < 10:
                continue

            # Skip header/boilerplate lines
            if any(kw in stripped.upper() for kw in ["TAX SALE", "MAP NO", "PARCEL", "OWNER NAME", "PAGE", "SUMNER COUNTY", "COURT"]):
                continue

            # Look for dollar amounts — real records have a $ amount
            amount_m = re.search(r'\$[\d,]+(?:\.\d{2})?', stripped)
            if not amount_m:
                continue

            amount = amount_m.group(0)
            rest = stripped[:amount_m.start()].strip()

            # Parcel ID: typically map-group-parcel format like 123 045.06 000
            parcel_m = re.search(r'\b(\d{3}\s+\d{3}(?:\.\d{2})?\s+\d{3}(?:\.\d{2})?|\d{3}-\d{3}-\d{3})\b', rest)
            parcel_id = owner = address = ""
            if parcel_m:
                parcel_id = parcel_m.group(1).strip()
                before = rest[:parcel_m.start()].strip()
                after = rest[parcel_m.end():].strip()
                # Heuristic: longer fragment is likely the address, shorter is owner
                if len(before) > len(after):
                    address = before
                    owner = after
                else:
                    owner = before
                    address = after
            else:
                owner = rest

            if not (owner or parcel_id):
                continue

            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                owner_name=owner,
                property_address=address,
                city="Gallatin",
                state="TN",
                parcel_id=parcel_id,
                amount_owed=amount,
                sale_date=sale_date,
                source_url=pdf_url,
                scraped_date=today,
            ))

        return records

    def _scrape_preforeclosures(self) -> list[PropertyRecord]:
        today = str(date.today())
        return [PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            notes=f"Search Lis Pendens at {ROD_URL} — Chancery Court Register of Deeds.",
            source_url=ROD_URL,
            scraped_date=today,
            city="Gallatin",
            state="TN",
        )]
