"""
Maricopa County AZ - Trustee Sale Notices via Tiffany & Bosco Law Firm.

Source: fs.tblaw.com/sales/PendingSalesAz.aspx
Tiffany & Bosco is one of the largest trustee firms in Arizona. The page
lists pending trustee sales statewide; we filter for Maricopa County.

Each record spans 3 table rows:
  Row 1 (7 cells): sale_date, file_no, loan_date, blank, org_loan_amt, status, opening_bid
  Row 2 (5 cells): sale_time, address, blank, trustor, max_bid
  Row 3 (3 cells): place_of_sale, county, blank

Note: Coverage is limited to one law firm. Maricopa typically has 3-8
active listings at a time from this source.
"""
import re
from datetime import date, timedelta

from .base_scraper import BaseScraper, PropertyRecord, log

PENDING_URL = "https://fs.tblaw.com/sales/PendingSalesAz.aspx"
PORTAL_URL  = "https://fs.tblaw.com/sales/PendingSalesAz.aspx"

_MONEY_RE  = re.compile(r"\$([\d,]+(?:\.\d{2})?)")
_CITY_ZIP  = re.compile(r"([A-Za-z ]+?),\s*AZ\s+(\d{5})", re.I)
_DATE_RE   = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")


def _clean(v: str) -> str:
    return re.sub(r"\s+", " ", (v or "")).strip(" ,.;()")


def _money(v: str) -> str:
    m = _MONEY_RE.search(v or "")
    return f"${m.group(1)}" if m else ""


def _first_date(v: str) -> str:
    """Return the first date in a string like '6/2/2026 (6/9/2026)'."""
    m = _DATE_RE.search(v or "")
    if not m:
        return ""
    try:
        from datetime import datetime
        return datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return m.group(1)


class MaricopaTrusteeSaleScraper(BaseScraper):
    county_name = "Maricopa"
    base_url = PORTAL_URL

    def scrape(self) -> list[PropertyRecord]:
        today = str(date.today())
        records: list[PropertyRecord] = []

        # First GET to capture ASP.NET state fields
        resp = self.get(PENDING_URL)
        if not resp:
            return [self._stub(today)]

        soup = self.soup(resp.text)
        vs  = (soup.find("input", {"name": "__VIEWSTATE"}) or {}).get("value", "")
        vsg = (soup.find("input", {"name": "__VIEWSTATEGENERATOR"}) or {}).get("value", "")
        evv = (soup.find("input", {"name": "__EVENTVALIDATION"}) or {}).get("value", "")

        # Build date strings without platform-specific strftime flags
        today_d = date.today()
        end_d   = today_d + timedelta(days=180)
        start_str = f"{today_d.month}/{today_d.day}/{today_d.year}"
        end_str   = f"{end_d.month}/{end_d.day}/{end_d.year}"

        post_data = {
            "__VIEWSTATE": vs,
            "__VIEWSTATEGENERATOR": vsg,
            "__EVENTVALIDATION": evv,
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "tbSaleStart": start_str,
            "tbSaleEnd": end_str,
            "tbCountyFilter": "",
            "tbAddrFilter": "",
            "tbCityFilter": "",
            "tbZipFilter": "",
            "tbFileFilter": "",
            "vceSaleStart_ClientState": "",
            "vceSaleEnd_ClientState": "",
            "vceDateCompare_ClientState": "",
            "vceCounty_ClientState": "",
            "vceAddr_ClientState": "",
            "vceCity_ClientState": "",
            "vceZip_ClientState": "",
            "vceFileFilter_ClientState": "",
            "cpeMoreOptions_ClientState": "",
            "btnSearch": "Search",
        }

        post_resp = self.session.post(PENDING_URL, data=post_data, timeout=30)
        if not post_resp or post_resp.status_code != 200:
            log.warning(f"[{self.county_name}] POST failed, falling back to GET response")
            post_resp = resp

        data_soup = self.soup(post_resp.text)

        # Find the main data table (the largest one by row count)
        tables = data_soup.find_all("table")
        data_table = None
        for tbl in tables:
            if len(tbl.find_all("tr")) > 10:
                data_table = tbl
                break

        if not data_table:
            log.warning(f"[{self.county_name}] No data table found on tblaw page")
            return [self._stub(today)]

        all_rows = data_table.find_all("tr")

        # Skip header rows (legend + 3 column-header rows = first ~6 rows)
        # Then data rows come in groups of 3 (plus ~3 blank/spacing rows = 6 per record)
        # Identify data rows by cell count: 7-cell = row1, 5-cell = row2, 3-cell = row3
        parsed: list[dict] = []
        i = 0
        while i < len(all_rows):
            row1_cells = [c.get_text(" ", strip=True) for c in all_rows[i].find_all(["td", "th"])]
            # A valid record row1 has 7 cells where cell[0] looks like a date and cell[5] is status
            if (len(row1_cells) == 7
                    and _DATE_RE.search(row1_cells[0])
                    and row1_cells[5] in ("Trustee Sale", "Postponed", "Cancelled", "Sale")):
                # Try to find row2 and row3 in the next few rows
                row2_cells = row3_cells = []
                for offset in range(1, 6):
                    if i + offset >= len(all_rows):
                        break
                    cels = [c.get_text(" ", strip=True) for c in all_rows[i + offset].find_all(["td", "th"])]
                    if len(cels) == 5 and not row2_cells:
                        row2_cells = cels
                    elif len(cels) == 3 and row2_cells and not row3_cells:
                        row3_cells = cels
                        break

                if row2_cells and row3_cells:
                    county = _clean(row3_cells[1]) if len(row3_cells) > 1 else ""
                    if county.lower() == "maricopa":
                        parsed.append({
                            "sale_date_raw": _clean(row1_cells[0]),
                            "file_no":       _clean(row1_cells[1]),
                            "org_loan_amt":  _clean(row1_cells[4]),
                            "status":        _clean(row1_cells[5]),
                            "opening_bid":   _clean(row1_cells[6]),
                            "address":       _clean(row2_cells[1]),
                            "trustor":       _clean(row2_cells[3]),
                            "max_bid":       _clean(row2_cells[4]),
                            "place_of_sale": _clean(row3_cells[0]),
                        })
            i += 1

        log.info(f"[{self.county_name}] Found {len(parsed)} Maricopa trustee sale records")

        for prop in parsed:
            address = prop["address"]
            city = zip_code = ""
            cz_m = _CITY_ZIP.search(address)
            if cz_m:
                city     = cz_m.group(1).strip().title()
                zip_code = cz_m.group(2)

            sale_date = _first_date(prop["sale_date_raw"])
            opening_bid = _money(prop["opening_bid"]) or _money(prop["org_loan_amt"])

            # Skip cancelled sales
            if "cancel" in prop["status"].lower():
                continue

            note_parts = [
                f"File#: {prop['file_no']}",
                f"Status: {prop['status']}",
            ]
            if prop["org_loan_amt"]:
                note_parts.append(f"Original loan: {prop['org_loan_amt']}")
            if prop["max_bid"]:
                note_parts.append(f"Max bid: {prop['max_bid']}")
            if prop["sale_date_raw"] and "(" in prop["sale_date_raw"]:
                note_parts.append(f"Sale dates: {prop['sale_date_raw']}")

            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Pre-Foreclosure",
                owner_name=prop["trustor"],
                property_address=address,
                city=city or "Phoenix",
                state="AZ",
                zip_code=zip_code,
                case_number=prop["file_no"],
                amount_owed=opening_bid,
                sale_date=sale_date,
                source_url=PORTAL_URL,
                scraped_date=today,
                notes=" | ".join(note_parts),
            ))

        if not records:
            records.append(self._stub(today))

        return records

    def _stub(self, today: str) -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            notes=(
                "No Maricopa County trustee sale notices found from Tiffany & Bosco. "
                f"Browse pending AZ sales at {PORTAL_URL}"
            ),
            source_url=PORTAL_URL,
            scraped_date=today,
            city="Phoenix",
            state="AZ",
        )

