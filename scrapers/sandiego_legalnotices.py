"""
San Diego County (CA) — Notice of Trustee Sale via the CNPA public-notice
aggregator (capublicnotice.com).

Search endpoint returns notices published in SD-area newspapers, but those
papers serve a wider geographic market (Riverside, etc.).  We fetch each
advert's full page to extract the property address and filter to SD County
only based on city name or zip code.

SD County zip codes start: 919xx, 920xx, 921xx (92003–92199 roughly)
Nearby non-SD zips to exclude: Temecula/Murrieta 92562-92596, Riverside 925xx
"""
import re
from datetime import date, timedelta
from .base_scraper import BaseScraper, PropertyRecord, log

SEARCH_URL = "https://www.capublicnotice.com/search/query"
PORTAL_URL = "https://www.capublicnotice.com/category/legals/notice-of-trustee-sale"
LOOKBACK_DAYS = 120

# Cities that are unambiguously in San Diego County
_SD_CITIES = {
    "san diego", "chula vista", "el cajon", "escondido", "oceanside",
    "carlsbad", "vista", "san marcos", "santee", "la mesa", "el cajon",
    "spring valley", "poway", "national city", "lemon grove", "encinitas",
    "solana beach", "del mar", "la jolla", "rancho bernardo", "miramar",
    "clairemont", "mission valley", "linda vista", "bay park", "pacific beach",
    "ocean beach", "point loma", "coronado", "imperial beach", "bonita",
    "chula vista", "otay ranch", "lakeside", "santee", "alpine", "ramona",
    "valley center", "fallbrook", "bonsall", "rainbow", "pauma valley",
    "warner springs", "julian", "borrego springs", "campo", "tecate",
    "potrero", "boulevard", "jacumba", "pine valley", "mount laguna",
    "la jolla", "cardiff", "leucadia", "olivenhain", "rancho santa fe",
    "4s ranch", "scripps ranch",
}

# SD County zips: 919xx and most 920xx (92003–92199)
# We allow 919xx and 9200x-9219x, reject 9220x+ (Riverside/SB area)
_SD_ZIP_RE = re.compile(r"\b(919\d{2}|920\d{2}|921\d{2})\b")
_NON_SD_ZIP_RE = re.compile(r"\b(925\d{2}|926\d{2}|927\d{2}|928\d{2}|922\d{2}|923\d{2}|924\d{2})\b")

_TS_RE = re.compile(r"T\.?S\.?\s*No\.?\s*[:#]?\s*([A-Z0-9\-]+)", re.I)
_APN_RE = re.compile(r"A\.?P\.?N\.?[:#\s]+([0-9][\d\-A-Z]+)", re.I)
_ADDR_RE = re.compile(r"Property Address[:\s]+([^\n]{10,120})", re.I)
_CITY_STATE_RE = re.compile(r"([A-Za-z ]+),\s*CA\s+(\d{5})", re.I)


def _is_san_diego(text: str) -> bool:
    """Return True if the notice text appears to be for a San Diego County property."""
    # Check explicit non-SD cities first
    non_sd = {"temecula", "murrieta", "menifee", "hemet", "perris",
              "riverside", "moreno valley", "corona", "lake elsinore",
              "san bernardino", "fontana", "ontario", "rancho cucamonga",
              "victorville", "palm springs", "palm desert", "indio"}
    lower = text.lower()
    for city in non_sd:
        if city in lower:
            return False

    # Non-SD zip codes are a hard reject
    if _NON_SD_ZIP_RE.search(text):
        return False

    # Any SD zip → keep
    if _SD_ZIP_RE.search(text):
        return True

    # Any SD city name → keep
    for city in _SD_CITIES:
        if city in lower:
            return True

    return False


class SanDiegoLegalNoticesScraper(BaseScraper):
    county_name = "San Diego"
    base_url = PORTAL_URL

    def scrape(self) -> list[PropertyRecord]:
        today_d = date.today()
        today = str(today_d)
        first = today_d - timedelta(days=LOOKBACK_DAYS)

        params = {
            "categories": "14",
            "county": "San Diego",
            "size": "100",
            "firstDate": first.strftime("%m/%d/%Y"),
            "lastDate": today_d.strftime("%m/%d/%Y"),
        }

        log.info(f"[{self.county_name}] Searching trustee-sale notices "
                 f"{first} to {today_d}...")
        resp = self.get(SEARCH_URL, params=params)
        if not resp:
            return [self._stub(today)]

        soup = self.soup(resp.text)
        blocks = soup.find_all("div", class_="list")
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

            # Fetch full notice to get address + confirm SD County
            full_text = ""
            if advert_id:
                full_resp = self.get(advert_url)
                if full_resp:
                    full_soup = self.soup(full_resp.text)
                    full_text = full_soup.get_text(" ", strip=True)

            if full_text and not _is_san_diego(full_text):
                skipped += 1
                continue

            ts_no = ""
            ts_m = _TS_RE.search(full_text or "")
            if ts_m:
                ts_no = ts_m.group(1)

            apn = ""
            apn_m = _APN_RE.search(full_text or "")
            if apn_m:
                apn = apn_m.group(1).strip(" -")

            # Extract property address
            address = city_name = zip_code = ""
            addr_m = _ADDR_RE.search(full_text or "")
            if addr_m:
                address = addr_m.group(1).strip()
                cs_m = _CITY_STATE_RE.search(address)
                if cs_m:
                    city_name = cs_m.group(1).strip().title()
                    zip_code = cs_m.group(2)
            else:
                # Fall back: find first "CITY, CA ZIP" pattern
                cs_m = _CITY_STATE_RE.search(full_text or "")
                if cs_m:
                    city_name = cs_m.group(1).strip().title()
                    zip_code = cs_m.group(2)

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
                property_address=address,
                city=city_name or "San Diego",
                state="CA",
                zip_code=zip_code,
                parcel_id=apn,
                case_number=ts_no,
                source_url=advert_url,
                scraped_date=today,
                notes=" | ".join(note_parts),
            ))

        log.info(
            f"[{self.county_name}] Trustee-sale notices: {len(records)} kept, "
            f"{skipped} skipped (outside SD County)"
        )

        if not records:
            records.append(self._stub(today))

        return records

    def _stub(self, today: str) -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            notes=f"No SD County trustee-sale notices found in last {LOOKBACK_DAYS} days. "
                  f"Browse at {PORTAL_URL}",
            source_url=PORTAL_URL,
            scraped_date=today,
            city="San Diego",
            state="CA",
        )
