"""
Cheatham County scraper.

Tax Delinquent:
  Live property table: https://www.cheathamcountytn.gov/delinquent_tax_property.html
    Table cols: Map | Parcel | Minimum Bid | Details
  PDF notice (when posted): https://cheathamcountytn.gov/assets/pdfs/delinquent_tax_sale.pdf
    Format: lettered paragraphs "A. Owner Property - Parcel XXX ... known as ADDRESS ... Judgment Amount $X"

Pre-Foreclosure: Register of Deeds portal reference.
"""
import re
from datetime import date
from .base_scraper import BaseScraper, PropertyRecord, log

DELINQUENT_PAGE = "https://www.cheathamcountytn.gov/delinquent_tax_property.html"
TAX_SALE_PDF = "https://cheathamcountytn.gov/assets/pdfs/delinquent_tax_sale.pdf"
ROD_URL = "https://www.cheathamcountytn.gov/court_chancery.html"

# Regex: matches lettered items like "A. Owner Name Property - Parcel XXX ... known as ADDRESS ... Judgment Amount $X"
PARCEL_RE = re.compile(
    r'[A-Z]\.\s+'                                   # letter prefix
    r'(.+?)\s+(?:Property|Heirs?)\s*[–\-]'         # owner
    r'.*?Parcel\s+([\w\-\.]+)'                       # parcel id
    r'.*?known as\s+(.+?),\s+(?:\w[\w\s]+,\s+)?'   # address
    r'Current Owner[^,\n]+[,.]?\s*'
    r'Judgment Amount\s+(\$[\d,]+\.\d{2})',
    re.DOTALL | re.IGNORECASE,
)
# Simpler fallback for each paragraph block
BLOCK_RE = re.compile(
    r'[A-Z]\.\s+(.+?)\s+[–\-]\s+Parcel\s+([\w\-\. ]+?)[\s(]'
    r'.*?known as\s+([^\n,]+)'
    r'.*?Judgment Amount\s+(\$[\d,]+\.\d{2})',
    re.DOTALL | re.IGNORECASE,
)


class CheathamScraper(BaseScraper):
    county_name = "Cheatham"
    base_url = DELINQUENT_PAGE

    def scrape(self) -> list[PropertyRecord]:
        records = []
        records += self._scrape_html_table()
        records += self._scrape_pdf()
        records += self._scrape_preforeclosures()
        return records

    # ------------------------------------------------------------------
    def _scrape_html_table(self) -> list[PropertyRecord]:
        records = []
        today = str(date.today())
        log.info(f"[{self.county_name}] Scraping live HTML delinquent table...")

        resp = self.get(DELINQUENT_PAGE)
        if not resp:
            return records

        soup = self.soup(resp.text)
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
            if not any(h in headers for h in ["map", "parcel", "bid", "minimum"]):
                continue
            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if not cells or all(c == "" for c in cells):
                    continue
                rec = PropertyRecord(
                    county=self.county_name,
                    record_type="Tax Delinquent",
                    source_url=DELINQUENT_PAGE,
                    scraped_date=today,
                    state="TN",
                    city="Ashland City",
                )
                for i, h in enumerate(headers):
                    val = cells[i] if i < len(cells) else ""
                    if "map" in h:
                        rec.parcel_id = val
                    elif "parcel" in h:
                        rec.parcel_id = (rec.parcel_id + " " + val).strip()
                    elif "bid" in h or "minimum" in h or "amount" in h:
                        rec.amount_owed = val
                    elif "owner" in h or "name" in h:
                        rec.owner_name = val
                    elif "address" in h:
                        rec.property_address = val
                if rec.parcel_id or rec.owner_name:
                    records.append(rec)

        log.info(f"[{self.county_name}] HTML table records: {len(records)}")
        return records

    # ------------------------------------------------------------------
    def _scrape_pdf(self) -> list[PropertyRecord]:
        records = []
        today = str(date.today())
        log.info(f"[{self.county_name}] Parsing delinquent tax sale PDF...")

        text = self.pdf_text(TAX_SALE_PDF)
        if not text:
            log.info(f"[{self.county_name}] PDF empty or unavailable.")
            return records

        # Extract sale date
        sale_date = ""
        m = re.search(r'(\d+:\d+ [ap]\.m\. on \w+,\s+\w+ \d+,\s*\d{4})', text, re.IGNORECASE)
        if m:
            sale_date = m.group(1)

        # Extract case number
        case_num = ""
        cm = re.search(r'Civil Action\s+No\.\s+([\w\s]+?)(?:\n|,)', text)
        if cm:
            case_num = cm.group(1).strip()

        # Split into lettered blocks: "A. ... B. ..."
        blocks = re.split(r'\n(?=[A-Z]\.\s+)', text)

        for block in blocks:
            block = block.strip()
            if not block or not re.match(r'^[A-Z]\.', block):
                continue

            # Owner: text before "Property -" or "Property –"
            owner = ""
            om = re.match(r'^[A-Z]\.\s+(.+?)\s+(?:Property\s*)?[–\-]', block)
            if om:
                owner = om.group(1).strip()
                # Clean trailing "Property" word
                owner = re.sub(r'\s+Property$', '', owner, flags=re.IGNORECASE).strip()

            # Parcel ID
            parcel = ""
            pm = re.search(r'Parcel\s+([\w\-\.]+(?:\s*\([\w\-\. ]+\))?)', block)
            if pm:
                parcel = pm.group(1).split('(')[0].strip()

            # Address: "known as ADDRESS,"
            address = ""
            am = re.search(r'known as\s+([^\n]+?)(?:,\s+Current Owner|,\s+Judgment)', block, re.IGNORECASE)
            if am:
                address = am.group(1).strip().rstrip(',')

            # Judgment amount
            amount = ""
            jm = re.search(r'Judgment Amount\s*[\-–]?\s*(\$[\d,]+\.\d{2})', block, re.IGNORECASE)
            if jm:
                amount = jm.group(1)

            if not (owner or parcel) or not amount:
                continue

            # Try to split city from address (last comma segment if it looks like a city)
            city = "Ashland City"
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
                case_number=case_num,
                source_url=TAX_SALE_PDF,
                scraped_date=today,
            ))

        log.info(f"[{self.county_name}] PDF records: {len(records)}")
        return records

    def _scrape_preforeclosures(self) -> list[PropertyRecord]:
        today = str(date.today())
        return [PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            notes=f"Search Lis Pendens at {ROD_URL} — Chancery Court / Register of Deeds.",
            source_url=ROD_URL,
            scraped_date=today,
            state="TN",
            city="Ashland City",
        )]
