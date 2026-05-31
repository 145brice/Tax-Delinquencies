import re
from datetime import date

import requests

from .base_scraper import BaseScraper, PropertyRecord, log


DUVAL_RE_TAX_URL = "https://legals.jaxdailyrecord.com/re_tax/retax.php"
DUVAL_RE_TAX_SEARCH_URL = "https://legals.jaxdailyrecord.com/re_tax/retax_search.php"
MAX_RECORDS = 1000

_TOTAL_RE = re.compile(r"Found\s+(\d+)\s+Records", re.I)
_NOTICE_TAX_YEAR_RE = re.compile(r"pay the amount due for\s+(\d{4})\s+delinquent", re.I)
_SALE_DATE_RE = re.compile(r"on the\s+(\d+(?:st|nd|rd|th)\s+day of\s+[A-Za-z]+,\s+\d{4})", re.I)
_ROW_RE = re.compile(
    r"R\.E\.\#\s+([0-9A-Z-]+)\s+"
    r"(\d{4})\s+"
    r"(.+?)\s+"
    r"(\$[\d,]+\.\d{2})"
    r"(?=\s+R\.E\.\#|\s+Displaying\s+|\s+Next\s+Records|$)",
    re.I | re.S,
)
_LEGAL_START_RE = re.compile(r"\b\d{1,2}-\d[A-Z]-\d{2}E\b")


def _clean(v: str) -> str:
    return re.sub(r"\s+", " ", (v or "").replace("\xa0", " ")).strip(" ,.;")


def _split_owner_description(raw: str) -> tuple[str, str]:
    raw = _clean(raw)
    match = _LEGAL_START_RE.search(raw)
    if not match:
        return raw, ""
    return _clean(raw[:match.start()]), _clean(raw[match.start():])


class DuvalJaxDailyRecordRealEstateTaxScraper(BaseScraper):
    county_name = "Duval"
    base_url = DUVAL_RE_TAX_SEARCH_URL

    def scrape(self) -> list[PropertyRecord]:
        today = str(date.today())
        params = {
            "mode": "search",
            "REnumber": "",
            "PropDesc": "",
            "Name": "",
            "incrementby": str(MAX_RECORDS),
            "sort": "1",
            "submit": "Search RE",
        }

        try:
            resp = requests.get(DUVAL_RE_TAX_URL, params=params, timeout=45)
            resp.raise_for_status()
        except requests.RequestException:
            return [self._stub(today, "source unavailable")]

        try:
            notice_resp = requests.get(DUVAL_RE_TAX_SEARCH_URL, timeout=30)
            notice_resp.raise_for_status()
            notice_text = self.soup(notice_resp.text).get_text(" ", strip=True)
        except requests.RequestException:
            notice_text = ""

        text = self.soup(resp.text).get_text(" ", strip=True)
        total_match = _TOTAL_RE.search(text)
        total_count = total_match.group(1) if total_match else ""
        notice_tax_year_match = _NOTICE_TAX_YEAR_RE.search(notice_text)
        notice_tax_year = notice_tax_year_match.group(1) if notice_tax_year_match else ""
        sale_date_match = _SALE_DATE_RE.search(notice_text)
        sale_date = sale_date_match.group(1) if sale_date_match else ""

        records: list[PropertyRecord] = []
        for match in _ROW_RE.finditer(text):
            parcel_id, tax_year, owner_and_description, amount = match.groups()
            owner, legal_description = _split_owner_description(owner_and_description)
            if not parcel_id or not owner:
                continue
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                owner_name=owner,
                property_address="",
                city="Jacksonville",
                state="FL",
                parcel_id=parcel_id,
                tax_year=tax_year,
                amount_owed=amount,
                sale_date=sale_date,
                source_url=resp.url,
                scraped_date=today,
                notes=(
                    "Duval real-estate tax certificate sale list. "
                    f"Notice delinquent tax year: {notice_tax_year or 'unknown'}. "
                    f"Row year: {tax_year}. "
                    f"Legal description: {legal_description}. "
                    f"Total source records: {total_count}"
                ),
            ))

        log.info(f"[Duval] Jax Daily Record delinquent RE tax records: {len(records)}")
        if not records:
            return [self._stub(today, "no matching tax records found")]
        return records

    def _stub(self, today: str, reason: str) -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type="Tax Delinquent",
            city="Jacksonville",
            state="FL",
            source_url=DUVAL_RE_TAX_SEARCH_URL,
            scraped_date=today,
            notes=f"Jax Daily Record delinquent real-estate tax scraper: {reason}.",
        )
