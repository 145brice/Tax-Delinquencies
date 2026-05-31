import re
from datetime import date

import requests

from .base_scraper import BaseScraper, PropertyRecord, log


NOTICE_URL = (
    "https://legals.jaxdailyrecord.com/public_notices/publicnotices.php"
    "?Category=Notice+of+Sale+-+Foreclosure&mode=daily"
)

COUNTY_MARKERS = {
    "Duval": ("DUVAL COUNTY", "DUVAL COUNTY, FLORIDA"),
    "Clay": ("CLAY COUNTY", "CLAY COUNTY, FLORIDA"),
    "Nassau": ("NASSAU COUNTY", "NASSAU COUNTY, FLORIDA"),
    "St. Johns": ("ST. JOHNS COUNTY", "SAINT JOHNS COUNTY"),
}

COUNTY_DEFAULT_CITY = {
    "Duval": "Jacksonville",
    "Clay": "Green Cove Springs",
    "Nassau": "Fernandina Beach",
    "St. Johns": "St. Augustine",
}

COUNTY_AUCTION_URL = {
    "Duval": "https://www.duval.realforeclose.com",
    "Clay": "https://www.clay.realforeclose.com",
    "Nassau": "https://www.nassauclerk.realforeclose.com",
    "St. Johns": "https://www.saintjohns.realforeclose.com",
}


_CASE_RE = re.compile(r"\bCASE\s*(?:NO\.?|#|NUMBER)?\s*[:.]?\s*([A-Z0-9\-]+(?:[ \t]+[A-Z0-9\-]+)?)", re.I)
_SALE_DATE_PATTERNS = [
    re.compile(r"\bon\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", re.I),
    re.compile(r"\bon\s+the\s+\d{1,2}(?:st|nd|rd|th)?\s+day\s+of\s+([A-Z][a-z]+,\s+\d{4})", re.I),
    re.compile(r"\bat\s+\d{1,2}:\d{2}\s*(?:A\.?M\.?|P\.?M\.?)?,?\s+on\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", re.I),
]
_ADDRESS_PATTERNS = [
    re.compile(r"(?:Property Address|Also known as)\s*[:.]?\s*([^\n]{8,160})", re.I),
]
_CITY_STATE_ZIP_RE = re.compile(r"(.+?),\s*([A-Za-z .'-]+),?\s+FL\s+(\d{5})", re.I)


def _clean(v: str) -> str:
    return re.sub(r"\s+", " ", (v or "").replace("\xa0", " ")).strip(" ,.;")


def _extract_case(text: str) -> str:
    m = _CASE_RE.search(text.replace("\r", "\n"))
    return _clean(m.group(1)) if m else ""


def _extract_sale_date(text: str) -> str:
    for pat in _SALE_DATE_PATTERNS:
        m = pat.search(text)
        if m:
            return _clean(m.group(1))
    return ""


def _extract_address(text: str) -> tuple[str, str, str]:
    for pat in _ADDRESS_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        raw = _clean(m.group(1))
        raw = re.split(r"\b(?:Any person|together with|Dated this|Americans With)", raw, 1, flags=re.I)[0]
        raw = _clean(raw)
        city = zip_code = ""
        cs = _CITY_STATE_ZIP_RE.search(raw)
        if cs:
            city = _clean(cs.group(2)).title()
            zip_code = cs.group(3)
        return raw, city, zip_code
    return "", "", ""


class JaxDailyRecordLegalNoticesScraper(BaseScraper):
    county_name = ""
    base_url = NOTICE_URL

    def scrape(self) -> list[PropertyRecord]:
        today = str(date.today())
        try:
            resp = requests.get(NOTICE_URL, timeout=30)
            resp.raise_for_status()
        except requests.RequestException:
            return [self._stub(today, "source unavailable")]

        soup = self.soup(resp.text)
        records: list[PropertyRecord] = []
        markers = COUNTY_MARKERS[self.county_name]

        for heading in soup.find_all("h3"):
            title = heading.get_text(" ", strip=True)
            if "Notice of Sale - Foreclosure" not in title:
                continue
            cell = heading.find_parent("td")
            if not cell:
                continue
            text = cell.get_text("\n", strip=True)
            upper = text.upper()
            if not any(marker in upper for marker in markers):
                continue

            case_number = _extract_case(text)
            address, city, zip_code = _extract_address(text)
            sale_date = _extract_sale_date(text)
            if not address:
                address = f"Case {case_number}, {self.county_name} County, FL" if case_number else ""
            if not address:
                continue

            ref = title.replace("Notice of Sale - Foreclosure", "").strip()
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Pre-Foreclosure",
                property_address=address,
                city=city or COUNTY_DEFAULT_CITY[self.county_name],
                state="FL",
                zip_code=zip_code,
                case_number=case_number or ref,
                sale_date=sale_date,
                source_url=NOTICE_URL,
                scraped_date=today,
                notes=f"{ref} | Auction: {COUNTY_AUCTION_URL[self.county_name]}",
            ))

        log.info(f"[{self.county_name}] Jax Daily Record foreclosure notices: {len(records)}")
        if not records:
            return [self._stub(today, "no matching notices found")]
        return records

    def _stub(self, today: str, reason: str) -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            city=COUNTY_DEFAULT_CITY.get(self.county_name, ""),
            state="FL",
            source_url=NOTICE_URL,
            scraped_date=today,
            notes=f"Jax Daily Record foreclosure notice scraper: {reason}.",
        )


class DuvalJaxDailyRecordScraper(JaxDailyRecordLegalNoticesScraper):
    county_name = "Duval"


class ClayJaxDailyRecordScraper(JaxDailyRecordLegalNoticesScraper):
    county_name = "Clay"


class NassauJaxDailyRecordScraper(JaxDailyRecordLegalNoticesScraper):
    county_name = "Nassau"


class StJohnsJaxDailyRecordScraper(JaxDailyRecordLegalNoticesScraper):
    county_name = "St. Johns"
