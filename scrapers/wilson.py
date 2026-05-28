"""
Wilson County scraper.

Tax Delinquent:
  https://wilcoclerkandmaster.com/court-sales/
  Lists active chancery court auction properties.

Pre-Foreclosure: Wilson County Register of Deeds
  https://www.wilsoncountytn.gov/174/Court-Public-Auctions-Delinquent-Tax-Sal
"""
import re
from datetime import date
from .base_scraper import BaseScraper, PropertyRecord, log

COURT_SALES_URL = "https://wilcoclerkandmaster.com/court-sales/"
COUNTY_SALES_URL = "https://www.wilsoncountytn.gov/174/Court-Public-Auctions-Delinquent-Tax-Sal"
ROD_URL = "https://www.wilsoncountytn.gov/173/Clerk-Master"


class WilsonScraper(BaseScraper):
    county_name = "Wilson"
    base_url = COURT_SALES_URL

    def scrape(self) -> list[PropertyRecord]:
        records = []
        records += self._scrape_court_sales()
        records += self._scrape_preforeclosures()
        return records

    def _scrape_court_sales(self) -> list[PropertyRecord]:
        records = []
        today = str(date.today())
        log.info(f"[{self.county_name}] Scraping court sales page...")

        for url in [COURT_SALES_URL, COUNTY_SALES_URL]:
            resp = self.get(url)
            if not resp:
                continue
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
                        source_url=url,
                        scraped_date=today,
                        state="TN",
                        city="Lebanon",
                    )
                    for i, h in enumerate(headers):
                        val = cells[i] if i < len(cells) else ""
                        if any(k in h for k in ["owner", "name", "taxpayer"]):
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

            # Parse WordPress post/entry content for address listings
            content_blocks = (
                soup.find_all("div", class_=re.compile(r'entry-content|post-content|wp-content|page-content', re.I))
                or soup.find_all("article")
                or [soup.find("main")]
            )
            for container in content_blocks:
                if not container:
                    continue
                full_text = container.get_text(separator="\n", strip=True)
                # Split on paragraph breaks and look for address lines
                for para in re.split(r'\n{2,}', full_text):
                    para = para.strip()
                    address_m = re.search(r'\d+\s+\w[\w\s]+(?:Road|Pike|Drive|Street|Avenue|Lane|Way|Blvd|Court|Circle|Rd|Dr|St|Ave|Ln|Ct)\b[^\n]{0,80}', para, re.I)
                    if not address_m:
                        continue
                    amount_m = re.search(r'\$[\d,]+(?:\.\d{2})?', para)
                    date_m = re.search(r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}', para, re.I)
                    address = address_m.group(0).strip()
                    city_m = re.search(r',\s*([A-Za-z ]+),\s*TN', address, re.I)
                    city = city_m.group(1).strip().title() if city_m else "Lebanon"
                    records.append(PropertyRecord(
                        county=self.county_name,
                        record_type="Tax Delinquent",
                        property_address=address,
                        city=city,
                        amount_owed=amount_m.group(0) if amount_m else "",
                        sale_date=date_m.group(0) if date_m else "",
                        notes=para[:300],
                        source_url=url,
                        scraped_date=today,
                        state="TN",
                    ))

            # Grab PDF links
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href.lower():
                    full_url = href if href.startswith("http") else f"https://wilcoclerkandmaster.com{href}"
                    text = self.pdf_text(full_url)
                    if text:
                        pdf_records = self._parse_sale_pdf(full_url, text, today)
                        records.extend(pdf_records)

        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"Visit {COURT_SALES_URL} for Wilson County court auction sales.",
                source_url=COURT_SALES_URL,
                scraped_date=today,
                state="TN",
                city="Lebanon",
            ))

        log.info(f"[{self.county_name}] Court sales records: {len(records)}")
        return records

    def _parse_sale_pdf(self, pdf_url: str, text: str, today: str) -> list[PropertyRecord]:
        records = []
        sale_date = ""
        m = re.search(r'(\w+ \d+,\s*\d{4})', text)
        if m:
            sale_date = m.group(1)

        for line in text.split("\n"):
            stripped = line.strip()
            amount_m = re.search(r'\$[\d,]+\.\d{2}\s*$', stripped)
            if not amount_m:
                continue
            amount = amount_m.group(0).strip()
            rest = stripped[:amount_m.start()].strip()
            parcel_m = re.search(r'\b(\d{3}[A-Z0-9\-]+)\b', rest)
            owner = rest[:parcel_m.start()].strip() if parcel_m else rest
            parcel = parcel_m.group(1) if parcel_m else ""
            address = rest[parcel_m.end():].strip() if parcel_m else ""
            if not owner and not parcel:
                continue
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                owner_name=owner,
                property_address=address,
                parcel_id=parcel,
                amount_owed=amount,
                sale_date=sale_date,
                source_url=pdf_url,
                scraped_date=today,
                state="TN",
                city="Lebanon",
            ))
        return records

    def _scrape_preforeclosures(self) -> list[PropertyRecord]:
        today = str(date.today())
        return [PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            notes=f"Search Lis Pendens at {ROD_URL} — Wilson County Clerk and Master / Register of Deeds.",
            source_url=ROD_URL,
            scraped_date=today,
            state="TN",
            city="Lebanon",
        )]
