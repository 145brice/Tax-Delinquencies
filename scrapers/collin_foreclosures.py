"""Collin County TX statutory foreclosure notices."""
import re
from datetime import date, datetime

from .base_scraper import BaseScraper, PropertyRecord, log


LIST_URL = "https://apps2.collincountytx.gov/ForeclosureNotices"
_CITY_ZIP_RE = re.compile(r"^([A-Z .'-]+),\s*TX\s+(\d{5})$", re.I)


class CollinForeclosuresScraper(BaseScraper):
    county_name = "Collin"
    base_url = LIST_URL

    def scrape(self) -> list[PropertyRecord]:
        today = date.today().isoformat()
        resp = self.get(LIST_URL)
        if not resp:
            return [self._stub(today)]
        soup = self.soup(resp.text)
        records: list[PropertyRecord] = []

        for row in soup.select("tr.mud-table-row"):
            header = row.select_one("p.list-header")
            if not header:
                continue
            lines = [
                re.sub(r"\s+", " ", line).strip()
                for line in header.get_text("\n", strip=True).splitlines()
                if line.strip()
            ]
            raw_address = " ".join(lines)
            address = lines[0] if lines else raw_address
            city_zip = _CITY_ZIP_RE.match(lines[1]) if len(lines) > 1 else None
            city = city_zip.group(1).strip().title() if city_zip else ""
            zip_code = city_zip.group(2) if city_zip else ""

            fields = {}
            for paragraph in row.select("p"):
                label = paragraph.select_one(".list-subheader")
                if label:
                    key = label.get_text(" ", strip=True).rstrip(":")
                    value = paragraph.get_text(" ", strip=True).replace(label.get_text(" ", strip=True), "", 1).strip()
                    fields[key] = value

            sale_date = ""
            try:
                sale_date = datetime.strptime(fields.get("Sale Date", ""), "%m/%d/%Y").date().isoformat()
            except ValueError:
                pass
            file_date = fields.get("File Date", "")
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Pre-Foreclosure",
                property_address=address,
                city=city or fields.get("City", "McKinney"),
                state="TX",
                zip_code=zip_code,
                sale_date=sale_date,
                case_number=f"{address}|{file_date}",
                source_url=LIST_URL,
                scraped_date=today,
                notes=f"Filed: {file_date} | Property type: {fields.get('Property Type', '')}".strip(" |"),
            ))

        log.info(f"[{self.county_name}] Statutory foreclosure notices: {len(records)}")
        return records or [self._stub(today)]

    def _stub(self, today: str) -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            city="McKinney",
            state="TX",
            source_url=LIST_URL,
            scraped_date=today,
            notes="No Collin County statutory foreclosure notices found.",
        )
