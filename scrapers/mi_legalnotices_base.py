"""
Michigan legal-notice scraper base class — mipublicnotices.com.

Michigan foreclosure-by-advertisement notices (the pre-sheriff-sale step) are
published in local newspapers and indexed by the Michigan Press Association's
public-notice portal. Its React frontend is backed by a JSON search API we
query directly, filtered to a county's numeric area alias and the two
foreclosure notice-type GUIDs.

All 83 Michigan counties are available via the same API endpoint. Each county
subclass only needs to declare:
  county_name   — display label (e.g. "Wayne")
  area_alias    — numeric county alias in the mipublicnotices API (alphabetical
                  position of the county among Michigan's 83 counties, 1-indexed)
  default_city  — county seat or largest city, used when the notice gives no address

The two notice types scraped are Mortgage Foreclosure and Judicial Foreclosure;
they cover different property sets (mortgage vs. court-ordered sale) but both
belong in the Pre-Foreclosure bucket.
"""
import re
from datetime import date, datetime, timedelta

from .base_scraper import BaseScraper, PropertyRecord, log

SEARCH_URL = "https://www.mipublicnotices.com/api/v1/search/search"
PORTAL_URL = "https://www.mipublicnotices.com/"

NOTICE_TYPES = {
    "4170ee87-9201-46e1-9b93-d2ba9fb27e89": "Mortgage Foreclosure",
    "d19ea8be-71a1-47d0-b293-5585a20ac3bc": "Judicial Foreclosure",
}

LOOKBACK_DAYS = 90
PER_PAGE = 50
MAX_PAGES = 20   # 20 * 50 = 1,000 notices max per type; Wayne has ~970 in 90d

_AMOUNT_PATTERNS = [
    re.compile(r"claimed to be due[^$]{0,80}\$([\d,]+\.\d{2})", re.I),
    re.compile(r"(?:sum|amount|total)[^$]{0,40}\$([\d,]+\.\d{2})", re.I),
]
_KNOWN_RE = re.compile(
    r"commonly known as[:\s]+(.+?),\s*([A-Za-z .]+?),\s*MI\.?\s*(\d{5})", re.I)
_LOCALITY_PATTERNS = [
    re.compile(r"(?:Township|City|Village) of\s+([A-Z][A-Za-z ]+)", re.I),
    re.compile(r"\b([A-Z][a-z]+)\s+Township\b"),
]
_SALE_RE = re.compile(
    r"\bon\s+(?:[A-Z][a-z]+day,?\s*)?([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})")
_REDEEM_RE = re.compile(
    r"redemption period\s+(?:will be|shall be|is)?\s*(\d+\s*(?:month|year)s?)", re.I)
# Notice titles are "Mortgage Foreclosure Sale-<mortgagor name(s)>". Strip the
# boilerplate prefix so owner_name is the actual homeowner, not "M.F.S.".
_TITLE_PREFIX_RE = re.compile(
    r"^\s*(?:Notice of\s+)?(?:Mortgage|Judicial)?\s*Foreclosure\s+(?:Sale|Notice)\b\s*[-–—:]*\s*",
    re.I)
# Fallback: the mortgagor named in the notice body when the title lacks a name.
_MORTGAGOR_RE = re.compile(
    r"mortgage\s+(?:made|granted|executed)\s+by[:\s]+(.+?)"
    r"(?:,?\s+(?:to\b|a single|an unmarried|a married|whose address|as mortgagor|dated))",
    re.I)


class MILegalNoticesScraper(BaseScraper):
    """Override in subclasses: county_name, area_alias, default_city."""
    county_name: str = ""
    area_alias: str = ""
    default_city: str = ""
    state: str = "MI"
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
                    "area": self.area_alias,
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

        # Real homeowner is the part after the boilerplate title prefix; fall
        # back to the mortgagor named in the notice body, then to the raw title.
        owner = _TITLE_PREFIX_RE.sub("", title).strip()
        owner = re.sub(r"\s*(Foreclosure\s+)?Notice\s*$", "", owner, flags=re.I).strip()
        if not owner or len(owner) < 3:
            m = _MORTGAGOR_RE.search(content)
            owner = m.group(1).strip() if m else title.strip()

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
                    city = m.group(1).strip().title()
                    break

        if not city:
            city = self.default_city

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
            county=self.county_name,
            record_type="Pre-Foreclosure",
            owner_name=owner,
            property_address=address,
            city=city,
            state=self.state,
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
            county=self.county_name,
            record_type="Pre-Foreclosure",
            state=self.state,
            city=self.default_city,
            notes=(f"No {self.county_name} County MI foreclosure notices found in last "
                   f"{lookback} days. Browse at {PORTAL_URL}"),
            source_url=PORTAL_URL,
            scraped_date=today,
        )
