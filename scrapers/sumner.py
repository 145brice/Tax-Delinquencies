"""
Sumner County scraper.

Tax Delinquent:
  Chancery Court page: https://sumnerchancerycourt.com/delinquent-taxes/  (Cloudflare-blocked)
  Real PDFs on county gov site: https://sumnercountytn.gov/
    - Delinquent-Tax-Sale-Exhibit-A-*.pdf  (scanned — needs OCR)
    - Tax-List-revised-*.pdf               (scanned — needs OCR)

Pre-Foreclosure: Sumner County Chancery Court / Register of Deeds
"""
import re
from datetime import date
from .base_scraper import BaseScraper, PropertyRecord, log

CHANCERY_URL = "https://sumnerchancerycourt.com/delinquent-taxes/"
COUNTY_SALE_URL = "https://sumnercountytn.gov/sale-of-delinquent-tax-committee-parcel/"
COUNTY_HOME = "https://sumnercountytn.gov/"
ROD_URL = "https://sumnerchancerycourt.com/"

# Known PDF URLs on sumnercountytn.gov
KNOWN_PDFS = [
    "https://sumnercountytn.gov/wp-content/uploads/2024/12/Delinquent-Tax-Sale-Exhibit-A-12-02-2024.pdf",
    "https://sumnercountytn.gov/wp-content/uploads/2024/12/Tax-List-revised-12-09-2024.pdf",
]


class SumnerScraper(BaseScraper):
    county_name = "Sumner"
    base_url = COUNTY_SALE_URL

    def scrape(self) -> list[PropertyRecord]:
        records = []
        records += self._scrape_tax_delinquent()
        records += self._scrape_preforeclosures()
        return records

    def _scrape_tax_delinquent(self) -> list[PropertyRecord]:
        records = []
        today = str(date.today())
        log.info(f"[{self.county_name}] Collecting PDF links from county site...")

        pdf_urls = list(KNOWN_PDFS)

        # Scrape county home for additional PDF links
        for url in [COUNTY_SALE_URL, COUNTY_HOME]:
            resp = self.get(url)
            if not resp:
                continue
            soup = self.soup(resp.text)
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href.lower() and any(kw in href.lower() for kw in ["delinquent", "tax-list", "tax_list", "taxsale", "tax-sale", "exhibit"]):
                    full_url = href if href.startswith("http") else f"https://sumnercountytn.gov{href}"
                    if full_url not in pdf_urls:
                        pdf_urls.append(full_url)

        # Also try chancery site (may be Cloudflare blocked but worth trying)
        resp = self.get(CHANCERY_URL)
        if resp and "cloudflare" not in resp.text.lower():
            soup = self.soup(resp.text)
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href.lower():
                    full_url = href if href.startswith("http") else f"https://sumnerchancerycourt.com{href}"
                    if full_url not in pdf_urls:
                        pdf_urls.append(full_url)

        log.info(f"[{self.county_name}] Parsing {len(pdf_urls)} PDFs (OCR fallback enabled)...")
        for pdf_url in pdf_urls:
            # Use auto: text extraction first, OCR fallback for scanned
            text = self.pdf_text_auto(pdf_url)
            if not text.strip():
                log.info(f"[{self.county_name}]   Skipping (empty after OCR): {pdf_url.split('/')[-1]}")
                continue
            recs = self._parse_sale_pdf(pdf_url, text, today)
            if recs:
                records.extend(recs)
                log.info(f"[{self.county_name}]   {pdf_url.split('/')[-1]} -> {len(recs)} records")

        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"PDFs are scanned images. Visit {COUNTY_SALE_URL} for current sale list.",
                source_url=COUNTY_SALE_URL,
                scraped_date=today,
                state="TN",
                city="Gallatin",
            ))

        log.info(f"[{self.county_name}] Tax delinquent records: {len(records)}")
        return records

    def _parse_sale_pdf(self, pdf_url: str, text: str, today: str) -> list[PropertyRecord]:
        records = []
        sale_date = ""
        m = re.search(r'(\w+ \d+,\s*\d{4})', text)
        if m:
            sale_date = m.group(1)

        lines = text.split("\n")
        in_data = False

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            if re.search(r'(owner|taxpayer|name).*(parcel|map|account)', stripped, re.I):
                in_data = True
                continue
            # Also start on first dollar-amount line even without header
            if not in_data and re.search(r'\$[\d,]+\.\d{2}', stripped):
                in_data = True

            if not in_data:
                continue

            # Skip boilerplate
            if any(kw in stripped for kw in ["DELINQUENT TAX", "CHANCERY COURT", "NOTICE", "GALLATIN", "SUMNER COUNTY", "Page "]):
                continue

            amount_m = re.search(r'\$[\d,]+\.\d{2}', stripped)
            if not amount_m:
                continue

            amount = amount_m.group(0)
            rest = stripped[:amount_m.start()].strip()
            rest = re.sub(r'\s+\d+\.\d+\s*$', '', rest).strip()

            parcel_m = re.search(r'\b(\d{3}[\w\-\.]+)\b', rest)
            owner = address = parcel = ""
            if parcel_m:
                owner = rest[:parcel_m.start()].strip().rstrip(',')
                address = rest[parcel_m.end():].strip()
                parcel = parcel_m.group(1)
            else:
                owner = rest

            if not owner and not parcel:
                continue

            city = "Gallatin"
            addr_parts = address.rsplit(',', 1)
            if len(addr_parts) == 2 and len(addr_parts[1].strip()) < 30:
                address = addr_parts[0].strip()
                city = addr_parts[1].strip()

            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                owner_name=owner,
                property_address=address,
                city=city,
                state="TN",
                parcel_id=parcel,
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
            notes=f"Search Lis Pendens at {ROD_URL} — Sumner County Chancery Court / Register of Deeds.",
            source_url=ROD_URL,
            scraped_date=today,
            state="TN",
            city="Gallatin",
        )]
