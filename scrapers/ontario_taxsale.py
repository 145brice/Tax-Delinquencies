"""
Ontario (Canada) municipal tax sales — OntarioTaxSales.ca aggregator.

Ontario municipalities sell tax-arrears properties by public tender/auction
under the Municipal Act. OntarioTaxSales.ca lists the active/upcoming sales.

IMPORTANT data limitation: Ontario tax-sale advertisements legally do NOT
publish the registered owner's name (only the roll number, legal description,
minimum tender amount, and — on this aggregator — a street address and map
pin). So these are buyer-facing *opportunity* records (address + minimum bid +
tender date), not owner skip-trace leads. owner_name is intentionally blank.

Toronto / Mississauga / Brampton etc. essentially never appear: the large GTA
cities recover arrears through their own process rather than a tax sale, so the
results here are smaller / rural / GTA-adjacent municipalities.
"""
import re
from datetime import date

from .base_scraper import BaseScraper, PropertyRecord, log


LISTINGS_URL = "https://www.ontariotaxsales.ca/tax-sale-properties"
_SALE_URL_RE = re.compile(
    r"https://www\.ontariotaxsales\.ca/tax-sales/[a-z0-9-]+/"
)
_REDIRECT_SALE_RE = re.compile(
    r"redirect_to=(https://www\.ontariotaxsales\.ca/tax-sales/[a-z0-9-]+/)"
)
_SLUG_RE = re.compile(r"^(.+)-(\d{4}-\d{2}-\d{2})$")
_MONEY_RE = re.compile(r"^\$[\d,]+(?:\.\d{2})?")
_COORD_RE = re.compile(r"^-?\d{2}\.\d+,\s*-?\d{2,3}\.\d+")


# Force gzip/deflate: the shared base headers advertise brotli, but the
# `brotli` package isn't installed, so a br-encoded body decodes to garbage.
_NO_BROTLI = {"Accept-Encoding": "gzip, deflate"}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip(" ,.;")


class OntarioTaxSaleScraper(BaseScraper):
    county_name = "Ontario (ON)"
    base_url = LISTINGS_URL

    def scrape(self) -> list[PropertyRecord]:
        today = str(date.today())
        sale_urls = self._enumerate_sales()
        log.info(f"[{self.county_name}] Found {len(sale_urls)} tax-sale pages")

        records: list[PropertyRecord] = []
        for url in sale_urls:
            records.extend(self._scrape_sale(url, today))

        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Sale",
                state="ON",
                source_url=LISTINGS_URL,
                scraped_date=today,
                notes="No active Ontario tax sales found at scrape time.",
            ))

        log.info(f"[{self.county_name}] Ontario tax-sale records: {len(records)}")
        return records

    def _enumerate_sales(self) -> list[str]:
        resp = self.get(LISTINGS_URL, headers=_NO_BROTLI)
        if not resp:
            return []
        urls = set(_SALE_URL_RE.findall(resp.text))
        urls.update(_REDIRECT_SALE_RE.findall(resp.text))
        # Drop the index page itself if matched.
        urls.discard(LISTINGS_URL + "/")
        # Keep only sales whose tender date is today or later.
        upcoming = []
        for u in urls:
            slug = u.rstrip("/").split("/")[-1]
            m = _SLUG_RE.match(slug)
            if m and m.group(2) >= date.today().isoformat():
                upcoming.append(u)
            elif not m:
                upcoming.append(u)
        return sorted(set(upcoming))

    def _scrape_sale(self, url: str, today: str) -> list[PropertyRecord]:
        resp = self.get(url, headers=_NO_BROTLI)
        if not resp:
            return []

        slug = url.rstrip("/").split("/")[-1]
        m = _SLUG_RE.match(slug)
        municipality = (m.group(1).replace("-", " ").title() if m else slug)
        sale_date = m.group(2) if m else ""

        soup = self.soup(resp.text)
        lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]

        records: list[PropertyRecord] = []
        seen: set[str] = set()
        for i, line in enumerate(lines):
            if not line.startswith("Minimum Tender Amount"):
                continue

            addr_line = _clean(lines[i - 1]) if i > 0 else ""
            amount = file_no = coords = ""
            sold = False
            for j in range(i + 1, min(i + 8, len(lines))):
                lj = lines[j]
                if not amount and _MONEY_RE.match(lj):
                    amount = _clean(lj)
                if lj.startswith("File Number") and j + 1 < len(lines):
                    file_no = _clean(lines[j + 1])
                if not coords and _COORD_RE.match(lj):
                    coords = _clean(lj)
                if "sold for" in lj.lower() or "cancelled" in lj.lower():
                    sold = True
            # Skip completed/cancelled sales — opportunity must still be open.
            if sold:
                continue
            if not addr_line or addr_line.startswith("Minimum Tender"):
                continue

            # Split a trailing ", City" off the address line when present.
            street, city = addr_line, municipality
            if "," in addr_line:
                head, tail = addr_line.rsplit(",", 1)
                if head.strip():
                    street, city = _clean(head), _clean(tail)

            dedupe_key = file_no or f"{municipality}|{addr_line}|{amount}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            note_parts = [
                "Ontario municipal tax sale (public tender).",
                "Owner name not published in Ontario tax-sale ads.",
            ]
            if coords:
                note_parts.append(f"Map: {coords}")

            records.append(PropertyRecord(
                county=municipality,
                record_type="Tax Sale",
                owner_name="",
                property_address=street,
                city=city,
                state="ON",
                amount_owed=amount,
                sale_date=sale_date,
                case_number=file_no,
                source_url=url,
                scraped_date=today,
                notes=" | ".join(note_parts),
            ))

        log.info(f"[{self.county_name}] {municipality} {sale_date}: {len(records)} records")
        return records
