"""Bexar County TX mortgage and tax foreclosure map data."""
from calendar import monthcalendar, TUESDAY
from datetime import date

from .base_scraper import BaseScraper, PropertyRecord, log


MAP_URL = "https://maps.bexar.org/foreclosures/"
LAYER_URL = "https://maps.bexar.org/arcgis/rest/services/CC/ForeclosuresProd/MapServer/{layer}/query"


def _first_tuesday(year: int, month: int) -> str:
    weeks = monthcalendar(year, month)
    day = next(week[TUESDAY] for week in weeks if week[TUESDAY])
    return date(year, month, day).isoformat()


class BexarForeclosuresScraper(BaseScraper):
    county_name = "Bexar"
    base_url = MAP_URL

    def scrape(self) -> list[PropertyRecord]:
        today_d = date.today()
        today = today_d.isoformat()
        records: list[PropertyRecord] = []
        for layer, label in ((0, "Mortgage"), (1, "Tax")):
            resp = self.get(
                LAYER_URL.format(layer=layer),
                params={"where": "1=1", "outFields": "*", "returnGeometry": "false", "f": "json"},
            )
            if not resp:
                continue
            try:
                features = resp.json().get("features", [])
            except ValueError:
                continue
            for feature in features:
                item = feature.get("attributes", {})
                year = int(item.get("YEAR") or 0)
                month = int(item.get("MONTH") or 0)
                sale_date = _first_tuesday(year, month) if year and 1 <= month <= 12 else ""
                if not sale_date or date.fromisoformat(sale_date) < today_d:
                    continue
                records.append(PropertyRecord(
                    county=self.county_name,
                    record_type="Tax Delinquent" if label == "Tax" else "Pre-Foreclosure",
                    property_address=str(item.get("ADDRESS") or "").strip(),
                    city=str(item.get("CITY") or "San Antonio").strip().title(),
                    state="TX",
                    zip_code=str(item.get("ZIP") or "").strip(),
                    sale_date=sale_date,
                    case_number=str(item.get("DOC_NUMBER") or "").strip(),
                    source_url=MAP_URL,
                    scraped_date=today,
                    notes=f"{label} foreclosure | {item.get('SCHOOL_DIST') or ''}".strip(" |"),
                ))
        log.info(f"[{self.county_name}] Foreclosure map records: {len(records)}")
        return records or [PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            city="San Antonio",
            state="TX",
            source_url=MAP_URL,
            scraped_date=today,
            notes="No Bexar County foreclosure map records found.",
        )]
