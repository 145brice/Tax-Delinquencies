"""
Robertson County scraper.

Tax Delinquent:
  https://robertsoncountytn.gov/departments/clerk_and_master/chancery_auction_sale.php
  Lists posted ~1 month before each sale. Uses GovEase for online bidding.
  PDF terms of sale linked on page.

Pre-Foreclosure: Robertson County Register of Deeds
"""
import re
from datetime import date
from .base_scraper import BaseScraper, PropertyRecord, log

SALE_PAGE = "https://robertsoncountytn.gov/departments/clerk_and_master/chancery_auction_sale.php"
GOVEASE_URL = "https://liveauctions.govease.com/tennessee/robertson-county"
ROD_URL = "https://robertsoncountytn.gov/departments/register_of_deeds/"
BASE = "https://robertsoncountytn.gov"


class RobertsonScraper(BaseScraper):
    county_name = "Robertson"
    base_url = SALE_PAGE

    def scrape(self) -> list[PropertyRecord]:
        records = []
        records += self._scrape_sale_page()
        records += self._scrape_preforeclosures()
        return records

    def _scrape_sale_page(self) -> list[PropertyRecord]:
        records = []
        today = str(date.today())
        log.info(f"[{self.county_name}] Scraping chancery auction sale page...")

        resp = self.get(SALE_PAGE)
        if not resp:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"Visit {SALE_PAGE} — list posted ~1 month before each sale. Online bidding: govease.com",
                source_url=SALE_PAGE,
                scraped_date=today,
                state="TN",
                city="Springfield",
            ))
            return records

        soup = self.soup(resp.text)

        # Parse tables
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if not cells or all(c == "" for c in cells):
                    continue
                rec = PropertyRecord(
                    county=self.county_name,
                    record_type="Tax Delinquent",
                    source_url=SALE_PAGE,
                    scraped_date=today,
                    state="TN",
                    city="Springfield",
                )
                for i, h in enumerate(headers):
                    val = cells[i] if i < len(cells) else ""
                    if any(k in h for k in ["owner", "name"]):
                        rec.owner_name = val
                    elif "address" in h:
                        rec.property_address = val
                    elif any(k in h for k in ["parcel", "map", "account"]):
                        rec.parcel_id = val
                    elif any(k in h for k in ["amount", "bid", "balance"]):
                        rec.amount_owed = val
                    elif "sale" in h and "date" in h:
                        rec.sale_date = val
                if rec.owner_name or rec.parcel_id or rec.property_address:
                    records.append(rec)

        # Parse any linked PDFs
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ".pdf" in href.lower():
                full_url = href if href.startswith("http") else f"{BASE}{href}"
                # Skip terms/rules PDFs — only parse sale list PDFs
                if any(skip in href.lower() for skip in ["terms", "announcements", "rules", "voting", "holiday", "board", "committee"]):
                    continue
                log.info(f"[{self.county_name}] Parsing PDF: {full_url}")
                text = self.pdf_text(full_url)
                if text and any(kw in text for kw in ["Parcel", "PARCEL", "Judgment", "JUDGMENT"]):
                    pdf_records = self._parse_sale_pdf(full_url, text, today)
                    records.extend(pdf_records)

        # Extract plain text property listings from the page body
        body_text = soup.get_text(separator="\n")
        for block in re.split(r'\n{2,}', body_text):
            block = block.strip()
            parcel_m = re.search(r'Parcel\s+([\w\-\.]+)', block, re.I)
            amount_m = re.search(r'Judgment Amount\s+\$?([\d,]+\.\d{2})', block, re.I)
            address_m = re.search(r'known as\s+([^\n,]+)', block, re.I)
            if parcel_m and amount_m:
                owner_m = re.match(r'^([A-Z][^\n]+?)(?:\s+Property|\s+\-)', block)
                records.append(PropertyRecord(
                    county=self.county_name,
                    record_type="Tax Delinquent",
                    owner_name=owner_m.group(1).strip() if owner_m else "",
                    property_address=address_m.group(1).strip() if address_m else "",
                    parcel_id=parcel_m.group(1),
                    amount_owed=f"${amount_m.group(1)}",
                    source_url=SALE_PAGE,
                    scraped_date=today,
                    state="TN",
                    city="Springfield",
                ))

        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"Visit {SALE_PAGE} — list posted ~1 month before each sale.",
                source_url=SALE_PAGE,
                scraped_date=today,
                state="TN",
                city="Springfield",
            ))

        log.info(f"[{self.county_name}] Tax delinquent records: {len(records)}")
        return records

    def _parse_sale_pdf(self, pdf_url: str, text: str, today: str) -> list[PropertyRecord]:
        records = []
        sale_date = ""
        m = re.search(r'(\w+ \d+,\s*\d{4})', text)
        if m:
            sale_date = m.group(1)

        for block in re.split(r'\n{2,}', text):
            block = block.strip()
            parcel_m = re.search(r'Parcel\s+([\w\-\.]+)', block, re.I)
            amount_m = re.search(r'Judgment Amount\s*[\-–]?\s*\$?([\d,]+\.\d{2})', block, re.I)
            address_m = re.search(r'known as\s+([^\n,]+)', block, re.I)
            if not (parcel_m and amount_m):
                continue
            owner_m = re.match(r'^([A-Z][^\n–\-]+?)(?:\s+Property|\s*[–\-])', block)
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                owner_name=owner_m.group(1).strip() if owner_m else "",
                property_address=address_m.group(1).strip() if address_m else "",
                parcel_id=parcel_m.group(1),
                amount_owed=f"${amount_m.group(1)}",
                sale_date=sale_date,
                source_url=pdf_url,
                scraped_date=today,
                state="TN",
                city="Springfield",
            ))
        return records

    def _scrape_preforeclosures(self) -> list[PropertyRecord]:
        today = str(date.today())
        return [PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            notes=f"Search Lis Pendens at {ROD_URL} — Robertson County Register of Deeds.",
            source_url=ROD_URL,
            scraped_date=today,
            state="TN",
            city="Springfield",
        )]
