"""
Maricopa County AZ trustee-sale notices from Arizona Capitol Times.

The public notice search currently exposes hundreds of Maricopa trustee-sale
records and detail pages with APN, address, trustor, principal balance, and
sale date. This source fills the volume gap left by single-trustee firm lists.
"""
import os
import re
from datetime import date
from urllib.parse import urljoin

from .base_scraper import BaseScraper, PropertyRecord, log

SEARCH_URL = "https://azcapitoltimes.com/public-notice/search-results/"
MAX_DETAILS = int(os.getenv("MARICOPA_AZCT_MAX_DETAILS", "150"))
MAX_PAGES = int(os.getenv("MARICOPA_AZCT_MAX_PAGES", "12"))

_TS_RE = re.compile(r"\bTS\s*(?:No\.?|#|#:)\s*[:#]?\s*([A-Z0-9_.-]+)", re.I)
_APN_RE = re.compile(r"\b(?:APN|A\.P\.N\.)\s*:?\s*([0-9A-Z-]+(?:\s+\d+)?(?:\s+FKA\s+[0-9A-Z-]+)?)", re.I)
_BALANCE_RE = re.compile(r"Original\s+Principal\s+Balance\s*:?\s*\$?([\d,]+(?:\.\d{2})?)", re.I)
_SALE_DATE_PATTERNS = [
    re.compile(r"\bon\s+([A-Za-z]+ \d{1,2}, \d{4})\s+at\s+\d{1,2}:\d{2}\s*(?:AM|PM)", re.I),
    re.compile(r"Sale\s+Date(?:\s+and\s+Time)?\s*:?\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})", re.I),
]
_ADDRESS_RE = re.compile(
    r"(?:purported\s+to\s+be|purported\s+street\s+address(?:\s+or\s+identifiable\s+location)?|street\s+address\s+and\s+other\s+common\s+designation(?:,\s*if\s+any,\s*of\s+the\s+real\s+property\s+described\s+above\s+is\s+purported\s+to\s+be)?|identifiable\s+location\s+of\s+trust\s+property)"
    r"\s*:?\s*(.+?AZ\s+\d{5})",
    re.I | re.S,
)
_TRUSTOR_RE = re.compile(
    r"Name\s+and\s+Address\s+of\s+(?:the\s+)?original\s+Trustor\s*:?\s*(.+?)"
    r"(?:\s+Name\s+and\s+Address\s+of\s+(?:the\s+)?Beneficiary|\s+Name\s+and\s+Address\s+of\s+Trustee|\s+Name\s+and\s+address\s+of\s+Successor\s+Trustee|\s+Said\s+sale\s+will|\s+The\s+Successor\s+Trustee)",
    re.I | re.S,
)
_CITY_ZIP_RE = re.compile(
    r"\b(Phoenix|Mesa|Scottsdale|Chandler|Glendale|Peoria|Tempe|Goodyear|Avondale|Surprise|Gilbert|Queen Creek|Buckeye|Tolleson|Laveen|Maricopa|Fountain Hills|Paradise Valley|Sun City|El Mirage)\s*,?\s+AZ\s+(\d{5})",
    re.I,
)


def _clean(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "")
    value = re.sub(r"\s+([,.;:])", r"\1", value)
    return value.strip(" ,.;:")


def _money(value: str) -> str:
    value = _clean(value)
    return f"${value}" if value and not value.startswith("$") else value


def _extract_sale_date(text: str) -> str:
    for pattern in _SALE_DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            return _clean(match.group(1))
    return ""


def _extract_address(text: str) -> str:
    match = _ADDRESS_RE.search(text)
    if not match:
        return ""
    address = _clean(match.group(1))
    address = re.sub(r"^.*purported\s+to\s+be\s*:?\s*", "", address, flags=re.I)
    address = re.sub(r"^.*identifiable\s+location\s+of\s+trust\s+property\s*:?\s*", "", address, flags=re.I)
    address = re.split(r"\s+(?:LOT|APN|A\.P\.N\.|Original\s+Principal\s+Balance)\b", address, flags=re.I)[0]
    return _clean(address)


def _extract_trustor(text: str, address: str) -> str:
    match = _TRUSTOR_RE.search(text)
    if not match:
        return ""
    trustor = _clean(match.group(1))
    trustor = re.sub(r"^\(as shown on the Deed of Trust\)\s*", "", trustor, flags=re.I)
    if address:
        trustor = _clean(trustor.replace(address, ""))
    trustor = re.sub(r"\s+\d{2,6}\s+.+$", "", trustor)
    return _clean(trustor)


class MaricopaAzCapitolTimesScraper(BaseScraper):
    county_name = "Maricopa"
    base_url = SEARCH_URL

    def __init__(self):
        super().__init__()
        self._force_plain_encoding()

    def _rotate_ua(self):
        super()._rotate_ua()
        self._force_plain_encoding()

    def _force_plain_encoding(self):
        # This site may return Brotli-compressed content without a decoder in
        # the local Python environment. Stick to encodings requests can read.
        self.session.headers["Accept-Encoding"] = "gzip, deflate"

    def scrape(self) -> list[PropertyRecord]:
        today = str(date.today())
        detail_urls = self._collect_detail_urls()
        records: list[PropertyRecord] = []
        seen: set[tuple[str, str, str]] = set()

        for detail_url in detail_urls[:MAX_DETAILS]:
            record = self._scrape_detail(detail_url, today)
            if not record:
                continue
            key = (
                record.case_number.lower(),
                record.parcel_id.lower(),
                record.property_address.lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            records.append(record)

        log.info(f"[{self.county_name}] AZ Capitol Times trustee notices: {len(records)}")
        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Pre-Foreclosure",
                city="Phoenix",
                state="AZ",
                source_url=SEARCH_URL,
                scraped_date=today,
                notes="No Maricopa trustee-sale notices found from Arizona Capitol Times search.",
            ))
        return records

    def _collect_detail_urls(self) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        page = 1
        while len(urls) < MAX_DETAILS and page <= MAX_PAGES:
            resp = self.get(SEARCH_URL, params={"indexgroup": "all", "pageindex": page})
            if not resp:
                break
            soup = self.soup(resp.text)
            page_links = []
            for a in soup.find_all("a", href=lambda h: h and "search-detail" in h):
                text = a.get_text(" ", strip=True)
                if "Trustee Sales" not in text or "County: Maricopa" not in text:
                    continue
                href = urljoin(SEARCH_URL, a.get("href"))
                if href in seen:
                    continue
                seen.add(href)
                page_links.append(href)
            if not page_links:
                break
            urls.extend(page_links)
            page += 1
        return urls

    def _scrape_detail(self, url: str, today: str) -> PropertyRecord | None:
        resp = self.get(url)
        if not resp:
            return None
        text = self.soup(resp.text).get_text("\n", strip=True)
        if "County: Maricopa" not in text and "Maricopa County" not in text:
            return None
        if "TRUSTEE" not in text.upper():
            return None

        # The detail page repeats a short preview before "Ad Text"; prefer the
        # full notice after that marker when present.
        ad_text = text.split("Ad Text", 1)[-1] if "Ad Text" in text else text

        ts = _clean((_TS_RE.search(ad_text) or ["", ""])[1])
        apn = _clean((_APN_RE.search(ad_text) or ["", ""])[1])
        amount = _money((_BALANCE_RE.search(ad_text) or ["", ""])[1])
        sale_date = _extract_sale_date(ad_text)
        address = _extract_address(ad_text)
        trustor = _extract_trustor(ad_text, address)

        city = "Phoenix"
        zip_code = ""
        city_match = _CITY_ZIP_RE.search(address)
        if city_match:
            city = city_match.group(1).title()
            zip_code = city_match.group(2)

        if not (ts or apn or address):
            return None

        notes = ["Arizona Capitol Times public trustee-sale notice"]
        if not trustor:
            notes.append("Trustor not listed/parsed")
        if not amount:
            notes.append("Original principal balance not listed/parsed")
        if not sale_date:
            notes.append("Sale date not listed/parsed")

        return PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            owner_name=trustor,
            property_address=address,
            city=city,
            state="AZ",
            zip_code=zip_code,
            parcel_id=apn,
            amount_owed=amount,
            sale_date=sale_date,
            case_number=ts,
            source_url=url,
            scraped_date=today,
            notes=" | ".join(notes),
        )
