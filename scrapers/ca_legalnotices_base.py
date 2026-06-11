"""
Generic California legal-notice scraper base class.

Scrapes Notice of Trustee Sale notices from capublicnotice.com filtered by
a configurable county. Subclasses just supply the county name, a default
city, and zip-code allow/deny ranges to filter cross-county notices.

Each notice listing on capublicnotice.com is published in a single newspaper,
but that paper may circulate to multiple counties — so we fetch each advert's
full page and confirm the property is in the target county via zip+city.
"""
import os
import re
from datetime import date, timedelta
from .base_scraper import BaseScraper, PropertyRecord, log

SEARCH_URL = "https://www.capublicnotice.com/search/query"
PORTAL_URL = "https://www.capublicnotice.com/category/legals/notice-of-trustee-sale"
LOOKBACK_DAYS = int(os.getenv("CA_NOTICE_LOOKBACK_DAYS", "60"))
MAX_NOTICE_DETAILS = int(os.getenv("CA_NOTICE_MAX_DETAILS", "150"))

_TS_RE = re.compile(r"T\.?S\.?\s*No\.?\s*[:#]?\s*([A-Z0-9\-]+)", re.I)
_APN_RE = re.compile(r"A\.?P\.?N\.?[:#\s]+([0-9][\d\-A-Z]+)", re.I)
_ADDR_RE = re.compile(r"Property Address[:\s]+([^\n]{10,120})", re.I)
_CITY_STATE_RE = re.compile(r"([A-Za-z ]+),\s*CA\s+(\d{5})", re.I)
_OWNER_PATTERNS = [
    re.compile(r"\bTrustor\(s\)?\s*[:\-]\s*([^\n]{3,120})", re.I),
    re.compile(r"\bTrustor\s*[:\-]\s*([^\n]{3,120})", re.I),
    re.compile(r"\bexecuted by\s+(.{3,120}?),\s+as\s+Trustor", re.I | re.S),
]

# Trustee-sale notices use varied phrasing for the unpaid balance.
# Order matters — most specific first.
_AMOUNT_PATTERNS = [
    re.compile(r"(?:estimated\s+(?:total\s+)?(?:amount\s+of\s+)?(?:the\s+)?unpaid\s+balance(?:\s+and\s+other\s+(?:charges|reasonable\s+estimated\s+costs))?[^$]{0,80})\$([\d,]+\.\d{2})", re.I),
    re.compile(r"(?:total\s+amount\s+(?:of\s+)?(?:the\s+)?(?:unpaid\s+)?obligation[^$]{0,80})\$([\d,]+\.\d{2})", re.I),
    re.compile(r"(?:opening\s+bid[^$]{0,40})\$([\d,]+\.\d{2})", re.I),
    re.compile(r"(?:initial\s+(?:opening\s+)?bid[^$]{0,40})\$([\d,]+\.\d{2})", re.I),
    re.compile(r"(?:amount\s+(?:of\s+)?(?:the\s+)?(?:unpaid\s+)?balance[^$]{0,40})\$([\d,]+\.\d{2})", re.I),
    # Fallback: the largest dollar amount in the notice (usually the loan balance)
]


def _extract_amount(text: str) -> str:
    """Return the most likely unpaid-balance amount from a trustee sale notice."""
    if not text:
        return ""
    for pat in _AMOUNT_PATTERNS:
        m = pat.search(text)
        if m:
            return f"${m.group(1)}"
    # Fallback: pick the largest $ amount in the notice — usually the loan balance
    amounts = re.findall(r"\$([\d,]+\.\d{2})", text)
    if amounts:
        nums = [(float(a.replace(",", "")), a) for a in amounts]
        nums.sort(reverse=True)
        # Largest amount must be > $1000 to plausibly be a loan balance
        if nums[0][0] > 1000:
            return f"${nums[0][1]}"
    return ""


# Boilerplate that follows the name in a trustee-sale notice — cut everything from here on.
_OWNER_STOP = re.compile(
    r"\b(?:Duly\s+Appointed|as\s+Trustee|Trustee|Beneficiary|Property\s+Address|"
    r"A\.?P\.?N|WHOSE\s+ADDRESS|Date\s+of\s+Sale|will\s+(?:be\s+)?s(?:old|ell)|"
    r"under\s+(?:a|the)\s+Deed|recorded\s+on|dated|NOTICE)\b", re.I)
# Vesting / marital descriptors that trail the name — strip from the descriptor onward.
_VESTING = re.compile(
    r",?\s*(?:"
    r"an?\s*married\s+\w+"          # "a married woman", "amarried woman" (notice typos)
    r"|an?\s+(?:un)?married\s+\w+"
    r"|a\s+widow(?:er)?"
    r"|an?\s+single\s+\w+"
    r"|as\s+(?:his|her|their)\s+sole"
    r"|as\s+joint\s+tenants"
    r"|as\s+(?:sole|community|separate)"
    r"|trustee[s]?\s+of"
    r"|husband\s+and\s+wife"
    r").*$", re.I)


def _extract_owner(text: str) -> str:
    if not text:
        return ""
    for pat in _OWNER_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        raw = re.sub(r"\s+", " ", m.group(1)).strip(" ,.;")
        raw = _OWNER_STOP.split(raw, 1)[0]      # drop trailing trustee boilerplate
        raw = _VESTING.sub("", raw)             # drop "a widower / married / sole & separate"
        raw = raw.strip(" ,.;")
        if 2 < len(raw) <= 100:
            return raw
    return ""


class CALegalNoticesScraper(BaseScraper):
    """Override these in subclasses:"""
    county_name: str = ""           # e.g. "San Diego"
    default_city: str = ""          # e.g. "San Diego" — fallback when no city extracted
    allowed_zip_patterns: list = [] # regex patterns matching county zips
    rejected_zip_patterns: list = []# regex patterns matching nearby OOC zips
    allowed_cities: set = set()     # lowercase city names known to be in this county
    rejected_cities: set = set()    # lowercase city names known to be outside

    base_url = PORTAL_URL

    def _detail_limit(self, lookback_days: int) -> int:
        # Larger search windows need more candidate notices, but still keep an
        # upper bound so Vercel does not spend forever opening detail pages.
        return min(MAX_NOTICE_DETAILS, max(60, lookback_days * 2))

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Pre-compile zip regexes
        cls._allow_re = [re.compile(p) for p in cls.allowed_zip_patterns]
        cls._reject_re = [re.compile(p) for p in cls.rejected_zip_patterns]

    def _is_in_county(self, text: str) -> bool:
        """
        Priority:
        1. If any allowed zip is in the 'Property Address' line — keep (strong signal).
        2. Otherwise, allowed_cities/zips outside Property Address only weakly indicate.
        3. Rejected cities/zips only reject if there's no allowed signal first.
        """
        lower = text.lower()

        # Strong signal: check the Property Address line specifically
        import re as _re
        addr_match = _re.search(r"Property Address[:\s]+([^\n]{10,160})", text, _re.I)
        if addr_match:
            addr = addr_match.group(1)
            addr_lower = addr.lower()
            # Allowed zip in property address → definitely in county
            for r in self._allow_re:
                if r.search(addr):
                    return True
            # Allowed city in property address → in county
            for city in self.allowed_cities:
                if city in addr_lower:
                    return True
            # Rejected city in property address → definitely out
            for city in self.rejected_cities:
                if city in addr_lower:
                    return False
            # Rejected zip in property address → out
            for r in self._reject_re:
                if r.search(addr):
                    return False

        # Fallback: scan whole text but be less strict
        # First check for allowed signals
        has_allow_zip = any(r.search(text) for r in self._allow_re)
        has_allow_city = any(city in lower for city in self.allowed_cities)

        if has_allow_zip:
            return True
        if has_allow_city:
            # City match but no zip — accept unless rejected city is also present
            for city in self.rejected_cities:
                if city in lower:
                    return False
            return True

        return False

    def scrape(self) -> list[PropertyRecord]:
        today_d = date.today()
        today = str(today_d)
        lookback_days = int(getattr(self, "lookback_days", None) or LOOKBACK_DAYS)
        lookback_days = max(1, min(365, lookback_days))
        detail_limit = self._detail_limit(lookback_days)
        first = today_d - timedelta(days=lookback_days)

        params = {
            "categories": "14",
            "county": self.county_name,
            "size": str(detail_limit),
            "firstDate": first.strftime("%m/%d/%Y"),
            "lastDate": today_d.strftime("%m/%d/%Y"),
        }

        log.info(f"[{self.county_name}] Searching trustee-sale notices "
                 f"{first} to {today_d}...")
        resp = self.get(SEARCH_URL, params=params)
        if not resp:
            return [self._stub(today)]

        soup = self.soup(resp.text)
        blocks = soup.find_all("div", class_="list")[:detail_limit]
        records: list[PropertyRecord] = []
        skipped = 0

        for block in blocks:
            advert_id_input = block.find("input", id=re.compile(r"^advertId_"))
            advert_id = advert_id_input["value"] if advert_id_input else ""
            advert_url = (f"https://www.capublicnotice.com/advert/-{advert_id}"
                          if advert_id else PORTAL_URL)

            h4 = block.find("h4")
            publication = h4.get_text(strip=True) if h4 else ""

            time_el = block.find("time")
            post_date = time_el.get("datetime", "")[:10] if time_el else ""

            ref_el = block.find("span", class_="refcode")
            refcode = ref_el.get_text(strip=True) if ref_el else ""

            full_text = ""
            if advert_id:
                full_resp = self.get(advert_url)
                if full_resp:
                    full_soup = self.soup(full_resp.text)
                    full_text = full_soup.get_text(" ", strip=True)

            if full_text and not self._is_in_county(full_text):
                skipped += 1
                continue

            ts_m = _TS_RE.search(full_text or "")
            ts_no = ts_m.group(1) if ts_m else ""

            apn_m = _APN_RE.search(full_text or "")
            apn = apn_m.group(1).strip(" -") if apn_m else ""

            address = city_name = zip_code = ""
            addr_m = _ADDR_RE.search(full_text or "")
            if addr_m:
                address = addr_m.group(1).strip()
                cs_m = _CITY_STATE_RE.search(address)
                if cs_m:
                    city_name = cs_m.group(1).strip().title()
                    zip_code = cs_m.group(2)
            else:
                cs_m = _CITY_STATE_RE.search(full_text or "")
                if cs_m:
                    city_name = cs_m.group(1).strip().title()
                    zip_code = cs_m.group(2)

            amount = _extract_amount(full_text or "")
            owner = _extract_owner(full_text or "")

            note_parts = []
            if publication:
                note_parts.append(f"Publication: {publication}")
            if post_date:
                note_parts.append(f"Posted: {post_date}")
            if refcode:
                note_parts.append(refcode)
            if address:
                note_parts.append(f"Address: {address}")

            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Pre-Foreclosure",
                owner_name=owner,
                property_address=address,
                city=city_name or self.default_city,
                state="CA",
                zip_code=zip_code,
                parcel_id=apn,
                case_number=ts_no,
                amount_owed=amount,
                source_url=advert_url,
                scraped_date=today,
                notes=" | ".join(note_parts),
            ))

        log.info(
            f"[{self.county_name}] Trustee-sale notices: {len(records)} kept, "
            f"{skipped} skipped (outside county)"
        )

        if not records:
            records.append(self._stub(today))

        return records

    def _stub(self, today: str) -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            notes=f"No {self.county_name} County trustee-sale notices found in last {int(getattr(self, 'lookback_days', None) or LOOKBACK_DAYS)} days. "
                  f"Browse at {PORTAL_URL}",
            source_url=PORTAL_URL,
            scraped_date=today,
            city=self.default_city,
            state="CA",
        )
