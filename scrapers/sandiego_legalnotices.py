"""
San Diego County (CA) — Notice of Trustee Sale via the CNPA public-notice
aggregator (capublicnotice.com).

Endpoint: /search/query with form params (GET works):
  categories=14         (Notice of Trustee Sale)
  county=San Diego
  firstDate, lastDate   (mm/dd/yyyy)
  size=100

Each result is a <div class="list"> block with publication, description
(contains TS#, APN, deed-of-trust date), post date, and refcode.
"""
import re
from datetime import date, timedelta
from .base_scraper import BaseScraper, PropertyRecord, log

SEARCH_URL = "https://www.capublicnotice.com/search/query"
PORTAL_URL = "https://www.capublicnotice.com/category/legals/notice-of-trustee-sale"
LOOKBACK_DAYS = 120

_TS_RE = re.compile(r"T\.?S\.?\s*No\.?\s*[:#]?\s*([A-Z0-9\-]+)", re.I)
_APN_RE = re.compile(r"APN[:#\s]+([0-9\-A-Z]+)", re.I)


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

        for block in blocks:
            advert_id_input = block.find("input", id=re.compile(r"^advertId_"))
            advert_id = advert_id_input["value"] if advert_id_input else ""
            advert_url = (f"https://www.capublicnotice.com/advert/-{advert_id}"
                          if advert_id else PORTAL_URL)

            h4 = block.find("h4")
            publication = h4.get_text(strip=True) if h4 else ""

            desc_el = block.find("span", class_="description")
            desc = desc_el.get_text(" ", strip=True) if desc_el else ""

            time_el = block.find("time")
            post_date = time_el.get("datetime", "")[:10] if time_el else ""

            ref_el = block.find("span", class_="refcode")
            refcode = ref_el.get_text(strip=True) if ref_el else ""

            ts_no = ""
            ts_m = _TS_RE.search(desc)
            if ts_m:
                ts_no = ts_m.group(1)

            apn = ""
            apn_m = _APN_RE.search(desc)
            if apn_m:
                apn = apn_m.group(1)

            note_parts = []
            if publication:
                note_parts.append(f"Publication: {publication}")
            if post_date:
                note_parts.append(f"Posted: {post_date}")
            if refcode:
                note_parts.append(refcode)
            if desc:
                note_parts.append(desc[:300])

            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Pre-Foreclosure",
                parcel_id=apn,
                case_number=ts_no,
                source_url=advert_url,
                scraped_date=today,
                city="San Diego",
                state="CA",
                notes=" | ".join(note_parts),
            ))

        if not records:
            records.append(self._stub(today))

        log.info(f"[{self.county_name}] Trustee-sale notices: {len(records)}")
        return records

    def _stub(self, today: str) -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            notes=f"No trustee-sale notices found in last {LOOKBACK_DAYS} days. "
                  f"Browse at {PORTAL_URL}",
            source_url=PORTAL_URL,
            scraped_date=today,
            city="San Diego",
            state="CA",
        )
