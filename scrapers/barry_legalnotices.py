"""Barry County (MI) — mortgage/judicial foreclosure notices via mipublicnotices.com.

Michigan foreclosure-by-advertisement notices (the pre-sheriff-sale step) are
published in The Hastings Banner and indexed by the Michigan Press Association's
public-notice site. Its React frontend is backed by a JSON search API we query
directly, filtered to the Barry County area (numeric alias) and the foreclosure
notice types.

These are mortgage foreclosures — a different property set from the county's
tax-foreclosure auction, so the two Barry sources do not overlap.
"""
import os
import re
from datetime import date, datetime, timedelta

from .base_scraper import BaseScraper, PropertyRecord, log

SEARCH_URL = "https://www.mipublicnotices.com/api/v1/search/search"
PORTAL_URL = "https://www.mipublicnotices.com/"

# Barry County's numeric area alias on mipublicnotices (counties are aliased
# alphabetically within Michigan; Barry == 8).
BARRY_AREA_ALIAS = os.getenv("MI_BARRY_AREA_ALIAS", "8")

# Foreclosure notice-type ids from /api/v1/noticetypes.
NOTICE_TYPES = {
    "4170ee87-9201-46e1-9b93-d2ba9fb27e89": "Mortgage Foreclosure",
    "d19ea8be-71a1-47d0-b293-5585a20ac3bc": "Judicial Foreclosure",
}

LOOKBACK_DAYS = int(os.getenv("MI_NOTICE_LOOKBACK_DAYS", "90"))
PER_PAGE = 50
MAX_PAGES = 6

_AMOUNT_PATTERNS = [
    re.compile(r"claimed to be due[^$]{0,80}\$([\d,]+\.\d{2})", re.I),
    re.compile(r"(?:sum|amount|total)[^$]{0,40}\$([\d,]+\.\d{2})", re.I),
]
# "commonly known as 711 Bauer Rd, Hastings, MI 49058"
_KNOWN_RE = re.compile(
    r"commonly known as[:\s]+(.+?),\s*([A-Za-z .]+?),\s*MI\.?\s*(\d{5})", re.I)
# Township / city fallbacks: "Township of Baltimore", "Irving Township".
_LOCALITY_PATTERNS = [
    re.compile(r"(?:Township|City|Village) of\s+([A-Z][A-Za-z]+)", re.I),
    re.compile(r"\b([A-Z][a-z]+)\s+Township\b"),
]
_SALE_RE = re.compile(
    r"\bon\s+(?:[A-Z][a-z]+day,?\s*)?([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})")
_REDEEM_RE = re.compile(
    r"redemption period\s+(?:will be|shall be|is)?\s*(\d+\s*(?:month|year)s?)", re.I)


class BarryLegalNoticesScraper(BaseScraper):
    county_name = "Barry"
    base_url = PORTAL_URL

    def scrape(self) -> list[PropertyRecord]:
        today_d = date.today()
        today = str(today_d)
        lookback = max(1, min(365, int(getattr(self, "lookback_days", None) or LOOKBACK_DAYS)))
        first = today_d - timedelta(days=lookback)

        records: list[PropertyRecord] = []
        seen: set[str] = set()

        for type_id, type_label in NOTICE_TYPES.items():
            for page in range(1, MAX_PAGES + 1):
                params = {
                    "type": type_id,
                    "area": BARRY_AREA_ALIAS,
                    "from": first.isoformat(),
                    "to": today_d.isoformat(),
                    "per": PER_PAGE,
                    "page": page,
                }
                resp = self.get(SEARCH_URL, params=params)
                if not resp:
                    break
                try:
                    hits = resp.json().get("hits", [])
                except ValueError:
                    log.warning(f"[{self.county_name}] Non-JSON response for {type_label}")
                    break
                if not hits:
                    break
                for hit in hits:
                    rec = self._parse_hit(hit, type_label, today)
                    if rec and rec.case_number not in seen:
                        seen.add(rec.case_number)
                        records.append(rec)
                if len(hits) < PER_PAGE:
                    break

        log.info(f"[{self.county_name}] Foreclosure notices: {len(records)} kept "
                 f"({first} to {today_d})")

        if not records:
            records.append(self._stub(today, lookback))
        return records

    def _parse_hit(self, hit: dict, type_label: str, today: str) -> PropertyRecord | None:
        notice = hit.get("notice") or {}
        notice_id = notice.get("id", "")
        title = notice.get("title", "")
        content = re.sub(r"\s+", " ", notice.get("content", "") or "")
        publication = (notice.get("publication") or {}).get("name", "")
        posted = (notice.get("firstDatePublished") or "")[:10]

        owner = re.sub(r"\s*(Foreclosure\s+)?Notice\s*$", "", title, flags=re.I).strip()

        address = city = zip_code = ""
        known = _KNOWN_RE.search(content)
        if known:
            address = known.group(1).strip()
            city = known.group(2).strip().title()
            zip_code = known.group(3)
        else:
            for pat in _LOCALITY_PATTERNS:
                m = pat.search(content)
                if m:
                    city = f"{m.group(1).title()} Township"
                    break

        amount = self._extract_amount(content)
        sale_date = self._extract_sale_date(content)

        note_parts = [f"Type: {type_label}"]
        if publication:
            note_parts.append(f"Publication: {publication}")
        if posted:
            note_parts.append(f"Posted: {posted}")
        redeem = _REDEEM_RE.search(content)
        if redeem:
            note_parts.append(f"Redemption: {redeem.group(1)}")

        return PropertyRecord(
            county="Barry",
            record_type="Pre-Foreclosure",
            owner_name=owner,
            property_address=address,
            city=city,
            state="MI",
            zip_code=zip_code,
            amount_owed=amount,
            sale_date=sale_date,
            case_number=notice_id,
            source_url=PORTAL_URL,
            scraped_date=today,
            notes=" | ".join(note_parts),
        )

    @staticmethod
    def _extract_amount(content: str) -> str:
        for pat in _AMOUNT_PATTERNS:
            m = pat.search(content)
            if m:
                return f"${m.group(1)}"
        amounts = [float(a.replace(",", "")) for a in re.findall(r"\$([\d,]+\.\d{2})", content)]
        amounts = [a for a in amounts if a > 1000]
        return f"${max(amounts):,.2f}" if amounts else ""

    @staticmethod
    def _extract_sale_date(content: str) -> str:
        m = _SALE_RE.search(content)
        if not m:
            return ""
        raw = m.group(1).replace(",", "")
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                pass
        return ""

    def _stub(self, today: str, lookback: int) -> PropertyRecord:
        return PropertyRecord(
            county="Barry",
            record_type="Pre-Foreclosure",
            state="MI",
            notes=f"No Barry County MI foreclosure notices found in last {lookback} days. "
                  f"Browse at {PORTAL_URL}",
            source_url=PORTAL_URL,
            scraped_date=today,
        )
