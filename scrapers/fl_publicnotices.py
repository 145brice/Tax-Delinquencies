"""Major-market Florida foreclosure notices via FloridaPublicNotices.com."""
import html
import os
import re
from datetime import date, datetime, timedelta

from .base_scraper import BaseScraper, PropertyRecord, log


SEARCH_URL = "https://floridapublicnotices.com/"
MAX_RESULTS = int(os.getenv("FL_NOTICE_MAX_RESULTS", "250"))
PAGE_SIZE = 50

_CASE_RE = re.compile(r"\bCASE\s+(?:NO\.?|NUMBER)?\s*[:#.]?\s*([A-Z0-9\-]+(?:XX[A-Z0-9]+)?)", re.I)
_SALE_PATTERNS = [
    re.compile(r"\bwill\s+sell\b.{0,500}?\bon\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+([A-Z][a-z]+),?\s+(\d{4})", re.I),
    re.compile(r"\bwill\s+sell\b.{0,500}?\bon\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})", re.I),
    re.compile(r"\bsale\b.{0,250}?\bon\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})", re.I),
]
_OWNER_RE = re.compile(r"\b(?:vs?\.?|versus)\s+(.{3,250}?),?\s+Defendant(?:s|\(s\))?", re.I | re.S)
_ADDRESS_PATTERNS = [
    re.compile(r"\bProperty\s+Address(?:es)?\s*[:#.]?\s*([^\n;]{8,160})", re.I),
    re.compile(r"\bStreet\s+Address(?:es)?\s*[:#.]?\s*([^\n;]{8,160})", re.I),
    re.compile(r"\bcommonly\s+known\s+as\s*[:#.]?\s*([^\n;]{8,160})", re.I),
]
_CITY_ZIP_RE = re.compile(r",\s*([A-Za-z .'-]+),?\s+FL(?:ORIDA)?\s+(\d{5})", re.I)
_NON_OWNER = re.compile(
    r"\b(?:unknown|tenant|spouse|association|bank|mortgage|secretary|department|"
    r"state of florida|county of|city of|clerk|trustee)\b",
    re.I,
)
_TIMESHARE = re.compile(r"\b(?:timeshare|interval ownership|vacation ownership|unit/week)\b", re.I)


def _clean(value: str) -> str:
    value = html.unescape(value or "")
    return re.sub(r"\s+", " ", value).strip(" ,.;")


class FLPublicNoticesScraper(BaseScraper):
    county_name = ""
    county_id = ""
    default_city = ""
    county_pattern = ""
    state = "FL"
    base_url = SEARCH_URL

    def scrape(self) -> list[PropertyRecord]:
        today_d = date.today()
        today = today_d.isoformat()
        lookback = max(1, min(365, int(getattr(self, "lookback_days", None) or 45)))
        first = today_d - timedelta(days=lookback)
        records: list[PropertyRecord] = []
        seen: set[str] = set()

        for offset in range(0, MAX_RESULTS, PAGE_SIZE):
            payload = {
                "counties": [self.county_id],
                "date-range--start-date": first.isoformat(),
                "date-range--end-date": today_d.isoformat(),
                "keywords": "foreclosure",
                "offset": offset or None,
                "paper": "-1",
                "sort-by": None,
                "limit": PAGE_SIZE,
            }
            resp = self.post(SEARCH_URL, json=payload)
            if not resp:
                break
            try:
                data = resp.json()
            except ValueError:
                break
            notices = data.get("_embedded", {}).get("notices", [])
            if not notices:
                break
            for notice in notices:
                record = self._parse_notice(notice, today)
                if record and record.case_number not in seen:
                    seen.add(record.case_number)
                    records.append(record)
            if len(notices) < PAGE_SIZE:
                break

        log.info(f"[{self.county_name}] Florida foreclosure notices: {len(records)}")
        return records or [self._stub(today)]

    def _parse_notice(self, notice: dict, today: str) -> PropertyRecord | None:
        text = _clean(notice.get("notice", ""))
        if not re.search(self.county_pattern, text, re.I):
            return None
        if _TIMESHARE.search(text):
            return None

        case_match = _CASE_RE.search(text)
        notice_id = str(notice.get("id") or "")
        case_number = _clean(case_match.group(1)) if case_match else notice_id

        owner = ""
        owner_match = _OWNER_RE.search(text)
        if owner_match:
            candidates = [
                _clean(part)
                for part in re.split(r"\s*;\s*|\s+AND\s+|,\s+(?=[A-Z][A-Z ])", owner_match.group(1))
            ]
            owners = [part for part in candidates if 2 < len(part) <= 100 and not _NON_OWNER.search(part)]
            owner = "; ".join(owners[:2])

        address = city = zip_code = ""
        for pattern in _ADDRESS_PATTERNS:
            match = pattern.search(text)
            if match:
                address = _clean(match.group(1))
                address = re.split(
                    r"\b(?:Any person|If any|Together with|The sale will|Dated this|"
                    r"Americans with|Property owner as of|Lis pendens)\b",
                    address,
                    1,
                    flags=re.I,
                )[0].strip(" ,.;")
                break
        if address:
            city_zip = _CITY_ZIP_RE.search(address)
            if city_zip:
                city = _clean(city_zip.group(1)).title()
                zip_code = city_zip.group(2)

        sale_date = ""
        for pattern in _SALE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            raw = (
                f"{match.group(2)} {match.group(1)}, {match.group(3)}"
                if len(match.groups()) == 3
                else match.group(1)
            )
            raw = re.sub(r"\s+", " ", raw).strip()
            for fmt in ("%B %d, %Y", "%B %d %Y"):
                try:
                    sale_date = datetime.strptime(raw, fmt).date().isoformat()
                    break
                except ValueError:
                    pass
            if sale_date:
                break

        source_path = (notice.get("_links", {}).get("self", {}) or {}).get("href", "")
        notes = [
            f"Publication: {notice.get('paper', '')}",
            f"Posted: {notice.get('date', '')}",
        ]
        return PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            owner_name=owner,
            property_address=address,
            city=city or self.default_city,
            state=self.state,
            zip_code=zip_code,
            sale_date=sale_date,
            case_number=case_number,
            source_url=f"https://floridapublicnotices.com{source_path}" if source_path else SEARCH_URL,
            scraped_date=today,
            notes=" | ".join(part for part in notes if not part.endswith(": ")),
        )

    def _stub(self, today: str) -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            city=self.default_city,
            state=self.state,
            source_url=SEARCH_URL,
            scraped_date=today,
            notes=f"No matching {self.county_name} County foreclosure notices found.",
        )


class BrowardPublicNoticesScraper(FLPublicNoticesScraper):
    county_name = "Broward"
    county_id = "6"
    default_city = "Fort Lauderdale"
    county_pattern = r"\bBROWARD\s+COUNTY\b"


class HillsboroughPublicNoticesScraper(FLPublicNoticesScraper):
    county_name = "Hillsborough"
    county_id = "28"
    default_city = "Tampa"
    county_pattern = r"\bHILLSBOROUGH(?:\s+COUNTY)?\b"


class MiamiDadePublicNoticesScraper(FLPublicNoticesScraper):
    county_name = "Miami-Dade"
    county_id = "43"
    default_city = "Miami"
    county_pattern = r"\bMIAMI\s*[- ]?\s*DADE\s+COUNTY\b"


class OrangeFLPublicNoticesScraper(FLPublicNoticesScraper):
    county_name = "Orange"
    county_id = "48"
    default_city = "Orlando"
    county_pattern = r"\bORANGE\s+COUNTY\b"


class PalmBeachPublicNoticesScraper(FLPublicNoticesScraper):
    county_name = "Palm Beach"
    county_id = "50"
    default_city = "West Palm Beach"
    county_pattern = r"\bPALM\s+BEACH\s+COUNTY\b"
