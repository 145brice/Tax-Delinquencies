"""
Williamson County scraper.

Tax Delinquent:
  https://www.williamsonchancery.org/DelinquentTaxes  (JS-rendered, falls back to search)
  Sale list PDFs linked from that page.

Pre-Foreclosures: Williamson County Register of Deeds
  https://www.williamsoncounty-tn.gov/323/Register-of-Deeds
"""
import re
from datetime import date
from .base_scraper import BaseScraper, PropertyRecord, log

DELINQUENT_URL = "https://www.williamsonchancery.org/DelinquentTaxes"
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
        records = []
        today = str(date.today())
        log.info(f"[{self.county_name}] Scraping delinquent tax page...")

        resp = self.get(DELINQUENT_URL)
        if resp:
            soup = self.soup(resp.text)

            # Parse any HTML tables present
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
                        source_url=DELINQUENT_URL,
                        scraped_date=today,
                        state="TN",
                        city="Franklin",
                    )
                    for i, h in enumerate(headers):
                        val = cells[i] if i < len(cells) else ""
                        if "owner" in h or "name" in h:
                            rec.owner_name = val
                        elif "address" in h:
                            rec.property_address = val
                        elif "parcel" in h or "map" in h:
                            rec.parcel_id = val
                        elif "amount" in h or "bid" in h or "balance" in h:
                            rec.amount_owed = val
                        elif "sale" in h and "date" in h:
                            rec.sale_date = val
                        elif "city" in h:
                            rec.city = val
                        elif "zip" in h:
                            rec.zip_code = val
                    if rec.owner_name or rec.parcel_id or rec.property_address:
                        records.append(rec)

            # Collect and parse any linked PDFs
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href.lower():
                    full_url = href if href.startswith("http") else f"https://www.williamsonchancery.org{href}"
                    pdf_records = self._parse_columnar_pdf(full_url, today)
                    records.extend(pdf_records)
                    if pdf_records:
                        log.info(f"[{self.county_name}]   PDF {full_url} -> {len(pdf_records)} records")

        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"Visit {DELINQUENT_URL} — page may require JavaScript. Check for current sale list PDFs.",
                source_url=DELINQUENT_URL,
                scraped_date=today,
                state="TN",
                city="Franklin",
            ))

        log.info(f"[{self.county_name}] Tax delinquent records: {len(records)}")
        return records

    def _parse_columnar_pdf(self, pdf_url: str, today: str) -> list[PropertyRecord]:
        """Parse Davidson-style columnar PDF (same format used region-wide)."""
        records = []
        text = self.pdf_text(pdf_url)
        if not text:
            return records

        sale_date = ""
        m = re.search(r'ON \w+,\s+(\w+ \d+,\s*\d{4})', text)
        if m:
            sale_date = m.group(1).strip()

        lines = text.split("\n")
        in_data = False
        for line in lines:
            if "Property Owner" in line and ("Map" in line or "Parcel" in line):
                in_data = True
                continue
            if not in_data:
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if any(kw in stripped for kw in ["PROPERTY TAX", "PURSUANT TO", "TERMS OF", "POTENTIAL PURCHASER", "Book & Page"]):
                continue

            amount_match = re.search(r'\$[\d,]+\.\d{2}\s*$', stripped)
            if not amount_match:
                continue

            amount = amount_match.group(0).strip()
            rest = stripped[:amount_match.start()].strip()
            acres_match = re.search(r'(\d+\.\d+)\s*$', rest)
            acres = ""
            if acres_match:
                acres = acres_match.group(1)
                rest = rest[:acres_match.start()].strip()

            parcel_match = re.search(r'\b(\d{5,}[A-Z0-9]*)\b', rest)
            parcel_id = owner = address = ""
            if parcel_match:
                owner = rest[:parcel_match.start()].strip().rstrip(',')
                after = rest[parcel_match.end():].strip()
                parcel_id = parcel_match.group(1)
                instr_m = re.match(r'^(\d{8}\s+\d{7})', after)
                address = after[instr_m.end():].strip() if instr_m else after
            else:
                owner = rest

            if not owner and not parcel_id:
                continue

            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                owner_name=owner,
                property_address=address,
                city="Franklin",
                state="TN",
                parcel_id=parcel_id,
                amount_owed=amount,
                sale_date=sale_date,
                notes=f"Acres: {acres}" if acres else "",
                source_url=pdf_url,
                scraped_date=today,
            ))
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
