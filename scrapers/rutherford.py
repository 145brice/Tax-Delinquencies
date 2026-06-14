"""
Rutherford County scraper.

Tax Delinquent:
  Sales handled via GovEase.com online auctions.
  Chancery Court search (all delinquent properties in court): https://rcchancery.com/tax-bill-receipts
  Search with "**" to pull all properties.

Pre-Foreclosure: rodweb.rutherfordcountytn.gov
"""
import re
from datetime import date
import requests
from .base_scraper import BaseScraper, PropertyRecord, log

SEARCH_URL = "https://rcchancery.com/tax-bill-receipts"
GOVEASE_TN = "https://liveauctions.govease.com/tennessee/rutherford-county"
ROD_URL = "https://rodweb.rutherfordcountytn.gov/"

# The rcchancery.com search endpoint (POST form)
SEARCH_POST_URL = "https://rcchancery.com/tax-bill-receipts"


class RutherfordScraper(BaseScraper):
    county_name = "Rutherford"
    base_url = SEARCH_URL

    def scrape(self) -> list[PropertyRecord]:
        records = []
        records += self._scrape_tax_delinquent()
        records += self._scrape_govease()
        records += self._scrape_preforeclosures()
        return records

    def _scrape_tax_delinquent(self) -> list[PropertyRecord]:
        records = []
        today = str(date.today())
        log.info(f"[{self.county_name}] Scraping chancery court tax search...")

        # Attempt the wildcard search - the site uses a GET param
        search_url = f"{SEARCH_URL}?search=**"
        resp = self.get(search_url)
        if not resp:
            resp = self.get(SEARCH_URL)
        if not resp:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"Visit {SEARCH_URL} and search '**' to see all delinquent properties.",
                source_url=SEARCH_URL,
                scraped_date=today,
                state="TN",
                city="Murfreesboro",
            ))
            return records

        soup = self.soup(resp.text)

        # Parse result tables
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
            if not headers:
                continue
            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if not cells or all(c == "" for c in cells):
                    continue
                rec = PropertyRecord(
                    county=self.county_name,
                    record_type="Tax Delinquent",
                    source_url=search_url,
                    scraped_date=today,
                    state="TN",
                    city="Murfreesboro",
                )
                for i, h in enumerate(headers):
                    val = cells[i] if i < len(cells) else ""
                    if any(k in h for k in ["owner", "name", "taxpayer"]):
                        rec.owner_name = val
                    elif "address" in h:
                        rec.property_address = val
                    elif any(k in h for k in ["parcel", "map", "account", "bill"]):
                        rec.parcel_id = val
                    elif any(k in h for k in ["amount", "balance", "tax", "total", "owed"]):
                        rec.amount_owed = val
                    elif "year" in h:
                        rec.tax_year = val
                    elif "city" in h:
                        rec.city = val
                    elif "zip" in h:
                        rec.zip_code = val
                if rec.owner_name or rec.parcel_id or rec.property_address:
                    records.append(rec)

        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"Visit {SEARCH_URL}?search=** to see all delinquent properties in Chancery Court.",
                source_url=SEARCH_URL,
                scraped_date=today,
                state="TN",
                city="Murfreesboro",
            ))

        log.info(f"[{self.county_name}] Tax delinquent records: {len(records)}")
        return records

    def _scrape_govease(self) -> list[PropertyRecord]:
        """Attempt to pull current auction listings from GovEase.
        If unavailable, return one informational fallback record."""
        records = []
        today = str(date.today())
        log.info(f"[{self.county_name}] Checking GovEase auction listings...")

        try:
            self._rotate_ua()
            resp = self.session.get(GOVEASE_TN, timeout=30)
        except requests.RequestException as e:
            log.warning(f"[{self.county_name}] GovEase request failed: {e}")
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"GovEase county page unavailable. Check {GOVEASE_TN} manually.",
                source_url=GOVEASE_TN,
                scraped_date=today,
                state="TN",
                city="Murfreesboro",
            ))
            return records

        if resp.status_code == 404 or "PageNotFound" in resp.url:
            log.info(f"[{self.county_name}] GovEase page unavailable (404/PageNotFound).")
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"GovEase county page currently unavailable (404). Check {GOVEASE_TN} later.",
                source_url=GOVEASE_TN,
                scraped_date=today,
                state="TN",
                city="Murfreesboro",
            ))
            return records

        if resp.status_code >= 400:
            log.warning(f"[{self.county_name}] GovEase returned HTTP {resp.status_code}.")
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"GovEase returned HTTP {resp.status_code}. Check {GOVEASE_TN} manually.",
                source_url=GOVEASE_TN,
                scraped_date=today,
                state="TN",
                city="Murfreesboro",
            ))
            return records

        soup = self.soup(resp.text)
        for card in soup.find_all(class_=re.compile(r"property|listing|auction|parcel", re.I)):
            text = card.get_text(separator=" ", strip=True)
            if not text or len(text) < 10:
                continue
            amount_m = re.search(r"\$[\d,]+", text)
            rec = PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=text[:200],
                amount_owed=amount_m.group(0) if amount_m else "",
                source_url=GOVEASE_TN,
                scraped_date=today,
                state="TN",
                city="Murfreesboro",
            )
            records.append(rec)

        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"No parseable GovEase listings found. Check {GOVEASE_TN} manually.",
                source_url=GOVEASE_TN,
                scraped_date=today,
                state="TN",
                city="Murfreesboro",
            ))

        log.info(f"[{self.county_name}] GovEase records: {len(records)}")
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
