"""
Maricopa County AZ trustee-sale notices from The Record Reporter PDFs.

Record Reporter publishes Arizona public notices as PDF newspaper editions.
The layout is multi-column and sometimes OCR-like, so this parser is deliberately
tolerant: it keeps real Maricopa trustee-sale blocks even when one field is not
available in the extracted text.
"""
import io
import os
import re
from datetime import date, timedelta

import pdfplumber

from .base_scraper import BaseScraper, PropertyRecord, log

BASE_URL = "https://recordreporter.com/LegalNotices"
SOURCE_URL = f"{BASE_URL}/"
MAX_PDFS = int(os.getenv("MARICOPA_RR_MAX_PDFS", "8"))

_TS_RE = re.compile(r"\bTS\s*(?:No\.?|#|#:)\s*[:#]?\s*([A-Z0-9_.-]+)", re.I)
_APN_RE = re.compile(
    r"(?:Tax\s+Parcel\s+Number|County\s+Assessor'?s\s+Tax\s+Parcel\s+Number|A\.?P\.?N\.?)"
    r"\s*[:#]?\s*([0-9A-Z-]{5,})",
    re.I,
)
_BALANCE_RE = re.compile(r"Original\s+Principal\s+Balance\s*:?\s*\$?([\d,]+(?:\.\d{2})?)", re.I)
_SALE_DATE_RE = re.compile(r"Sale\s+Date(?:\s+and\s+Time)?\s*:?\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})", re.I)
_ADDRESS_PATTERNS = [
    re.compile(
        r"(?:Purported\s+Street\s+Address(?:\s+Or\s+Identifiable\s+Location)?|Street\s+Address\s+or\s+Identifiable\s+Location(?:\s+of\s+Property)?|address\s+of\s+real\s+property)"
        r"\s*:?\s*(.+?AZ\s+\d{5})",
        re.I | re.S,
    ),
    re.compile(r"\b(\d{2,6}\s+[A-Z0-9][A-Z0-9 .#'-]{5,90},?\s+(?:Phoenix|Mesa|Scottsdale|Chandler|Glendale|Peoria|Tempe|Goodyear|Avondale|Surprise|Gilbert|Queen Creek|Buckeye|Tolleson|Laveen|Maricopa)\s*,?\s+AZ\s+\d{5})", re.I),
]
_TRUSTOR_RE = re.compile(
    r"(?:Name(?:\(s\))?\s+and\s+Address(?:\(s\))?\s+of\s+Original\s+Trustor(?:\(s\))?|Name\s+and\s+address\s+or\s+original\s+trustor)"
    r"\s*:?\s*(.+?)(?:\s+Name\s+(?:and\s+Address\s+)?of\s+(?:Current\s+)?Beneficiary|\s+Name\s+(?:and\s+Address\s+)?of\s+Trustee|\s+The\s+Successor\s+Trustee|\s+Sale\s+Information|\s+Dated:|\s+RR-\d+)",
    re.I | re.S,
)
_CITY_ZIP_RE = re.compile(
    r"\b(Phoenix|Mesa|Scottsdale|Chandler|Glendale|Peoria|Tempe|Goodyear|Avondale|Surprise|Gilbert|Queen Creek|Buckeye|Tolleson|Laveen|Maricopa)\s*,?\s+AZ\s+(\d{5})",
    re.I,
)


def _clean(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "")
    value = re.sub(r"\s+([,.;:])", r"\1", value)
    return value.strip(" ,.;:")


def _money(value: str) -> str:
    value = _clean(value)
    return f"${value}" if value and not value.startswith("$") else value


def _pdf_url(day: date) -> str:
    return f"{BASE_URL}/RR-{day:%Y-%m-%d}.pdf"


def _candidate_issue_dates(today: date, lookback_days: int | None) -> list[date]:
    lookback = max(1, int(lookback_days or 30))
    dates = []
    for offset in range(lookback + 1):
        d = today - timedelta(days=offset)
        # Record Reporter commonly publishes legal notices in weekday editions.
        if d.weekday() < 5:
            dates.append(d)
    return dates[:MAX_PDFS]


class MaricopaRecordReporterScraper(BaseScraper):
    county_name = "Maricopa"
    base_url = SOURCE_URL

    def scrape(self) -> list[PropertyRecord]:
        today = date.today()
        today_str = str(today)
        records: list[PropertyRecord] = []
        seen: set[tuple[str, str, str]] = set()

        for issue_date in _candidate_issue_dates(today, getattr(self, "lookback_days", None)):
            url = _pdf_url(issue_date)
            pdf_records = self._scrape_issue(url, issue_date, today_str)
            for record in pdf_records:
                key = (
                    record.case_number.lower(),
                    record.parcel_id.lower(),
                    record.property_address.lower(),
                )
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)

        log.info(f"[{self.county_name}] Record Reporter trustee notices: {len(records)}")
        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Pre-Foreclosure",
                city="Phoenix",
                state="AZ",
                source_url=SOURCE_URL,
                scraped_date=today_str,
                notes="No Maricopa trustee-sale notices found in recent Record Reporter PDFs.",
            ))
        return records

    def _scrape_issue(self, url: str, issue_date: date, today: str) -> list[PropertyRecord]:
        resp = self.get(url)
        if not resp:
            return []

        try:
            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                page_texts = []
                for page in pdf.pages:
                    full_text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
                    cols = []
                    width, height = page.width, page.height
                    for col in range(3):
                        crop = page.crop((col * width / 3, 0, (col + 1) * width / 3, height))
                        cols.append(crop.extract_text(x_tolerance=1, y_tolerance=3) or "")
                    page_texts.append("\n".join([full_text, *cols]))
        except Exception as exc:
            log.warning(f"[{self.county_name}] Record Reporter PDF parse failed for {url}: {exc}")
            return []

        text = "\n".join(page_texts)
        return self._parse_text(text, url, issue_date, today)

    def _parse_text(self, text: str, url: str, issue_date: date, today: str) -> list[PropertyRecord]:
        normalized = re.sub(r"[’‘]", "'", text or "")
        normalized = re.sub(r"\r", "\n", normalized)
        starts = [m.start() for m in re.finditer(r"(?i)(?:\bTS\s*(?:No\.?|#|#:)|Notice\s+Of\s+Trustee'?s\s+Sale)", normalized)]
        records: list[PropertyRecord] = []
        seen_chunks: set[str] = set()

        for idx, start in enumerate(starts):
            end = starts[idx + 1] if idx + 1 < len(starts) else start + 3500
            chunk = normalized[start:min(end, start + 5000)]
            if "MARICOPA" not in chunk.upper():
                continue
            compact_key = re.sub(r"\W+", "", chunk[:500].lower())
            if compact_key in seen_chunks:
                continue
            seen_chunks.add(compact_key)

            record = self._parse_notice(chunk, url, issue_date, today)
            if record:
                records.append(record)
        return records

    def _parse_notice(self, chunk: str, url: str, issue_date: date, today: str) -> PropertyRecord | None:
        ts = _clean((_TS_RE.search(chunk) or ["", ""])[1])
        apn = _clean((_APN_RE.search(chunk) or ["", ""])[1])
        balance = _money((_BALANCE_RE.search(chunk) or ["", ""])[1])
        sale_date = _clean((_SALE_DATE_RE.search(chunk) or ["", ""])[1])

        address = ""
        for pattern in _ADDRESS_PATTERNS:
            match = pattern.search(chunk)
            if match:
                address = _clean(match.group(1))
                break
        address = re.sub(r"\s+Name\s+.*$", "", address, flags=re.I)

        trustor = ""
        trustor_match = _TRUSTOR_RE.search(chunk)
        if trustor_match:
            trustor = _clean(trustor_match.group(1))
            if address and address in trustor:
                trustor = _clean(trustor.replace(address, ""))
            trustor = re.sub(r"\s+\d{2,6}\s+.+$", "", trustor)

        city = "Phoenix"
        zip_code = ""
        city_match = _CITY_ZIP_RE.search(address)
        if city_match:
            city = city_match.group(1).title()
            zip_code = city_match.group(2)

        # Keep genuine Maricopa notices even if a noisy PDF page loses the APN
        # or trustor. Require at least one useful lead identifier beyond county.
        if not (ts or apn or address):
            return None

        notes = [
            f"Record Reporter issue: {issue_date:%Y-%m-%d}",
            "Maricopa trustee-sale public notice",
        ]
        if not trustor:
            notes.append("Trustor not parsed from PDF text")
        if not balance:
            notes.append("Original principal/opening amount not parsed from PDF text")
        if not sale_date:
            notes.append("Sale date not parsed from PDF text")

        return PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            owner_name=trustor,
            property_address=address,
            city=city,
            state="AZ",
            zip_code=zip_code,
            parcel_id=apn,
            amount_owed=balance,
            sale_date=sale_date,
            case_number=ts,
            source_url=url,
            scraped_date=today,
            notes=" | ".join(notes),
        )
