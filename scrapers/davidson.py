"""
Davidson County (Nashville) scraper.

Tax Delinquent sale lists are posted as PDFs under wp-content/uploads on:
  https://chanceryclerkandmaster.nashville.gov/

PDF format (fixed-width columnar):
  Property Owner | Map/Parcel Number | Instrument Number | Property Address | Acres | Amount

Owners sometimes wrap onto a continuation line that has NO parcel/amount.
We join continuation lines to the previous record.
"""
import re
from datetime import date
from .base_scraper import BaseScraper, PropertyRecord, log

SCHEDULE_URL = "https://chanceryclerkandmaster.nashville.gov/fees/property-tax-schedule/"
SALE_INFO_URL = "https://chanceryclerkandmaster.nashville.gov/fees/delinquent-tax-sales/"
WP_BASE = "https://chanceryclerkandmaster.nashville.gov/wp-content/uploads/"

# Sale list PDFs are uploaded per sale date with predictable names but are not
# reliably linked from any page. Pattern observed across years:
#   <Month><D>th_Tax-Sale<taxyear>_<seq>.pdf   (also TaxSale, and abbreviated month)
# where <taxyear> lags the sale year by 2-3 (e.g. June 17 2026 sale = tax year
# 2023) and <seq> counts sequential list revisions. We discover them by parsing
# sale dates off the schedule page and probing candidate URLs.

_ORDINAL = {1: "st", 2: "nd", 3: "rd", 21: "st", 22: "nd", 23: "rd", 31: "st"}


def _day_suffix(day: int) -> str:
    return _ORDINAL.get(day, "th")

# Continuation line: no parcel-like digits at start, no dollar amount
_CONT_RE = re.compile(r'^\s{0,5}[A-Z][A-Z\s,&\.]+$')
_SKIP_WORDS = {
    "PROPERTY TAX SALE", "PURSUANT TO TENNESSEE", "TERMS OF THE SALE",
    "POTENTIAL PURCHASER", "Metropolitan Government", "Book & Page",
    "Instrument Number", "SAMUEL D.", "For further information",
    "ASSEMBLY", "CONTINUING", "OF SAID SALE", "SHALL BE", "GOVERNMENT",
    "PROPERTIES WILL", "no representations",
}


class DavidsonScraper(BaseScraper):
    county_name = "Davidson"
    base_url = SCHEDULE_URL

    def scrape(self) -> list[PropertyRecord]:
        records = []
        records += self._scrape_tax_delinquent()
        records += self._scrape_preforeclosures()
        return records

    def _discover_sale_pdfs(self, schedule_html: str) -> list[str]:
        """Find current sale-list PDFs by probing candidate URLs derived from
        the sale dates posted on the schedule page."""
        import time as _time
        from datetime import datetime, timedelta

        sale_dates = []
        for m in re.finditer(r'([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})', schedule_html):
            try:
                d = datetime.strptime(f"{m.group(1)} {m.group(2)}, {m.group(3)}", "%B %d, %Y").date()
            except ValueError:
                continue
            if d not in sale_dates:
                sale_dates.append(d)

        today = date.today()
        # Lists post ~1 month before a sale and stay up after; older sales'
        # records get dropped by the lookback filter anyway.
        window = [d for d in sale_dates
                  if today - timedelta(days=60) <= d <= today + timedelta(days=60)]

        found: list[str] = []
        for d in window:
            month_full = d.strftime("%B")
            month_abbr = d.strftime("%b")
            day = f"{d.day}{_day_suffix(d.day)}"
            name_stems = []
            for mon in (month_full, month_abbr):
                for sep in ("Tax-Sale", "TaxSale"):
                    for tax_year in (d.year - 3, d.year - 2):
                        name_stems.append(f"{mon}{day}_{sep}{tax_year}")
            for stem in name_stems:
                probe = f"{WP_BASE}{stem}_1.pdf"
                try:
                    r = self.session.head(probe, timeout=10)
                except Exception:
                    continue
                _time.sleep(0.3)
                if r.status_code != 200:
                    continue
                # Pattern hit: walk sequence numbers until a miss.
                found.append(probe)
                seq = 2
                while seq <= 8:
                    nxt = f"{WP_BASE}{stem}_{seq}.pdf"
                    try:
                        r = self.session.head(nxt, timeout=10)
                    except Exception:
                        break
                    _time.sleep(0.3)
                    if r.status_code != 200:
                        break
                    found.append(nxt)
                    seq += 1
                break   # naming is consistent per sale; stop trying other stems
        return found

    def _scrape_tax_delinquent(self) -> list[PropertyRecord]:
        records = []
        today = str(date.today())
        log.info(f"[{self.county_name}] Collecting PDF links...")

        pdf_urls: list[str] = []

        # Scrape both pages for linked PDFs, and probe for unlinked sale lists
        # using the sale-date schedule.
        for page_url in [SCHEDULE_URL, SALE_INFO_URL]:
            resp = self.get(page_url)
            if resp:
                soup = self.soup(resp.text)
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "wp-content" in href and ".pdf" in href.lower():
                        full_url = href if href.startswith("http") else f"https://chanceryclerkandmaster.nashville.gov{href}"
                        if full_url not in pdf_urls:
                            pdf_urls.append(full_url)
                if page_url == SCHEDULE_URL:
                    for url in self._discover_sale_pdfs(resp.text):
                        if url not in pdf_urls:
                            pdf_urls.append(url)

        log.info(f"[{self.county_name}] Parsing {len(pdf_urls)} PDFs...")
        seen_urls = set()
        for pdf_url in pdf_urls:
            if pdf_url in seen_urls:
                continue
            seen_urls.add(pdf_url)
            recs = self._parse_davidson_pdf(pdf_url, today)
            if recs:
                records.extend(recs)
                log.info(f"[{self.county_name}]   {pdf_url.split('/')[-1]} -> {len(recs)} records")

        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=f"No parseable sale list PDFs found. Visit {SCHEDULE_URL}",
                source_url=SCHEDULE_URL,
                scraped_date=today,
                state="TN",
                city="Nashville",
            ))

        log.info(f"[{self.county_name}] Total tax delinquent records: {len(records)}")
        return records

    def _parse_davidson_pdf(self, pdf_url: str, today: str) -> list[PropertyRecord]:
        """
        Two PDF layouts exist:
        Format A (older): all fields on one line, owner may wrap to next line
          333, LLC  09211020400  20161212 0129961  333 22ND AVE N  .20  $21,305.81
        Format B (newer): owner+parcel+address may be split across lines, amount at end
          CITY VIEW DEVELOPMENT  .13  $2,106.83   (owner only, no parcel on this line)
          CRC GROUP LLC  091134K00100CO  20220930 0107954  409 A EASTBORO DR  0  $1,628.39
        We try Format A first; if yield < 5 records, try Format B.
        """
        records = []
        text = self.pdf_text(pdf_url)
        if not text:
            return records

        # Extract sale date
        sale_date = ""
        m = re.search(r'ON \w+,\s+(\w+ \d+,\s*\d{4})', text)
        if m:
            sale_date = m.group(1).strip()

        lines = text.split("\n")
        in_data = False
        pending: PropertyRecord | None = None

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Start of data section
            if "Property Owner" in stripped and "Map/Parcel" in stripped:
                in_data = True
                continue
            if not in_data:
                continue

            # Skip known boilerplate / page footer lines
            if any(skip in stripped for skip in _SKIP_WORDS):
                continue
            # Skip page number lines like "C:\Users\...doc  7/9/2025"
            if re.match(r'^C:\\', stripped) or re.match(r'^\d+$', stripped):
                continue

            # Does the line have a dollar amount? -> real record or continuation with amount
            amount_m = re.search(r'\$[\d,]+\.\d{2}\s*$', stripped)

            if amount_m:
                # Save any pending record first
                if pending:
                    records.append(pending)
                    pending = None

                amount = amount_m.group(0).strip()
                rest = stripped[:amount_m.start()].strip()

                # Acres: decimal just before amount
                acres = ""
                acres_m = re.search(r'(\d+\.\d+)\s*$', rest)
                if acres_m:
                    acres = acres_m.group(1)
                    rest = rest[:acres_m.start()].strip()

                # Parcel ID: 5+ digit block possibly with letters/dashes
                parcel_m = re.search(r'\b(\d{5,}[A-Z0-9]*(?:[A-Z]{2}\d+[A-Z]{0,2})?)\b', rest)
                owner = address = parcel_id = instrument = ""

                if parcel_m:
                    owner = rest[:parcel_m.start()].strip().rstrip(',')
                    after = rest[parcel_m.end():].strip()
                    parcel_id = parcel_m.group(1)
                    # Instrument: two groups of 8+7 digits
                    instr_m = re.match(r'^(\d{7,8}\s+\d{7})', after)
                    if instr_m:
                        instrument = instr_m.group(1).strip()
                        address = after[instr_m.end():].strip()
                    else:
                        address = after
                else:
                    owner = rest

                if not owner and not parcel_id:
                    continue

                pending = PropertyRecord(
                    county=self.county_name,
                    record_type="Tax Delinquent",
                    owner_name=owner,
                    property_address=address,
                    city="Nashville",
                    state="TN",
                    parcel_id=parcel_id,
                    amount_owed=amount,
                    sale_date=sale_date,
                    notes=f"Acres: {acres}" if acres else "",
                    source_url=pdf_url,
                    scraped_date=today,
                )

            else:
                # Continuation line (owner name overflow, e.g. "BRENDA ET AL")
                if pending and re.match(r'^[A-Z&\s,\.\-\']+$', stripped) and len(stripped) < 60:
                    pending.owner_name = (pending.owner_name + " " + stripped).strip()
                # Otherwise skip

        # Don't forget the last record
        if pending:
            records.append(pending)

        # Format B fallback: if we got < 5 records, try grouping by amount-only lines
        if len(records) < 5:
            records = self._parse_davidson_pdf_format_b(text, pdf_url, sale_date, today)

        return records

    def _parse_davidson_pdf_format_b(self, text: str, pdf_url: str, sale_date: str, today: str) -> list[PropertyRecord]:
        """
        Format B: lines with just 'OWNER  acres  $amount' interleaved with
        lines like 'OWNER  PARCEL  INSTRUMENT  ADDRESS  acres  $amount' (complete).
        Group lines between dollar-amount anchors.
        """
        records = []
        lines = text.split("\n")
        in_data = False
        buffer = []

        def flush(buf):
            if not buf:
                return None
            joined = " ".join(buf)
            amount_m = re.search(r'\$[\d,]+\.\d{2}', joined)
            if not amount_m:
                return None
            amount = amount_m.group(0)
            rest = joined[:amount_m.start()].strip()
            # Remove acres
            rest = re.sub(r'\s+\d+\.\d+\s*$', '', rest).strip()
            rest = re.sub(r'\s+0\s*$', '', rest).strip()
            parcel_m = re.search(r'\b(\d{5,}[A-Z0-9]*)\b', rest)
            owner = address = parcel_id = ""
            if parcel_m:
                owner = rest[:parcel_m.start()].strip().rstrip(',')
                after = rest[parcel_m.end():].strip()
                parcel_id = parcel_m.group(1)
                instr_m = re.match(r'^(\d{7,8}\s+\d{7})', after)
                address = after[instr_m.end():].strip() if instr_m else after
            else:
                owner = rest
            if not owner and not parcel_id:
                return None
            return PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                owner_name=owner,
                property_address=address,
                city="Nashville",
                state="TN",
                parcel_id=parcel_id,
                amount_owed=amount,
                sale_date=sale_date,
                source_url=pdf_url,
                scraped_date=today,
            )

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if "Property Owner" in stripped and ("Number" in stripped or "Parcel" in stripped):
                in_data = True
                continue
            if not in_data:
                continue
            if any(skip in stripped for skip in _SKIP_WORDS):
                continue
            if re.match(r'^C:\\', stripped):
                continue

            if re.search(r'\$[\d,]+\.\d{2}\s*$', stripped):
                buffer.append(stripped)
                rec = flush(buffer)
                if rec:
                    records.append(rec)
                buffer = []
            else:
                buffer.append(stripped)

        if buffer:
            rec = flush(buffer)
            if rec:
                records.append(rec)

        return records

    def _scrape_preforeclosures(self) -> list[PropertyRecord]:
        today = str(date.today())
        return [PropertyRecord(
            county=self.county_name,
            record_type="Pre-Foreclosure",
            notes="Search Lis Pendens at https://register.padctn.org/ — doc type 'LIS PENDENS'. Requires authenticated portal access.",
            source_url="https://register.padctn.org/",
            scraped_date=today,
            city="Nashville",
            state="TN",
        )]
