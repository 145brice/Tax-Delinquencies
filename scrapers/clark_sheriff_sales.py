"""Clark County NV sheriff sales from the official annual sales page."""
import re
from datetime import date, datetime

import requests

from .base_scraper import BaseScraper, PropertyRecord, log


SALES_URL = (
    "https://www.clarkcountynv.gov/government/departments/"
    "sheriff_civil/sheriff_s_sales/{year}-sales"
)
_APN_RE = re.compile(r"APN:\s*([A-Z0-9\-]+)", re.I)
_CITY_ZIP_RE = re.compile(r"^(.+?),\s*NV\s+(\d{5})$", re.I)


class ClarkSheriffSalesScraper(BaseScraper):
    county_name = "Clark"
    base_url = SALES_URL.format(year=date.today().year)

    def scrape(self) -> list[PropertyRecord]:
        today_d = date.today()
        today = today_d.isoformat()
        url = SALES_URL.format(year=today_d.year)
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException:
            return [self._stub(today, url)]
        soup = self.soup(resp.text)
        table = soup.select_one("div.table.bordered")
        records: list[PropertyRecord] = []

        for row in table.find_all("div", recursive=False)[1:] if table else []:
            cells = row.find_all("div", recursive=False)
            if len(cells) < 4:
                continue
            date_text = cells[0].get_text(" ", strip=True)
            details = [p.get_text(" ", strip=True) for p in cells[1].find_all("p")]
            status = cells[3].get_text(" ", strip=True)
            if len(details) < 3 or "CANCEL" in status.upper():
                continue
            try:
                sale_date = datetime.strptime(date_text, "%m/%d/%Y").date().isoformat()
            except ValueError:
                continue
            if date.fromisoformat(sale_date) < today_d:
                continue
            case_number = details[0]
            address = details[1]
            city_zip = _CITY_ZIP_RE.match(details[2])
            city = city_zip.group(1).strip().title() if city_zip else "Las Vegas"
            zip_code = city_zip.group(2) if city_zip else ""
            joined = " ".join(details)
            apn_match = _APN_RE.search(joined)
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Pre-Foreclosure",
                property_address=address,
                city=city,
                state="NV",
                zip_code=zip_code,
                parcel_id=apn_match.group(1) if apn_match else "",
                sale_date=sale_date,
                case_number=case_number,
                source_url=url,
                scraped_date=today,
                notes=f"Sheriff sale status: {status}",
            ))

        log.info(f"[{self.county_name}] Sheriff sale records: {len(records)}")
        return records or [self._stub(today, url)]

    def _stub(self, today: str, url: str) -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            city="Las Vegas",
            state="NV",
            source_url=url,
            scraped_date=today,
            notes="No active Clark County sheriff sales found.",
        )
