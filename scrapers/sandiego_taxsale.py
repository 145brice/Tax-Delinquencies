"""
San Diego County (CA) tax-defaulted property sales.

Primary source:
  Official 2026 Notice of Public Internet Auction PDF from the San Diego
  Treasurer-Tax Collector. It includes item number, APN, last assessee, and
  minimum bid for the March 13-18, 2026 tax-defaulted property auction.

Secondary source:
  MyTaxSale prior-sale results page. This is useful for comps/history, but the
  public current auction item pages hide APNs unless logged in.
"""
import re
from datetime import date

import requests

from .base_scraper import BaseScraper, PropertyRecord, log


CURRENT_AUCTION_NOTICE_URL = (
    "https://www.sdttc.com/content/dam/ttc/docs/taxcollection/"
    "Notice-of-Public-Online-Auction-7102.pdf"
)
PRIOR_SALES_URL = "https://sdttc.mytaxsale.com/reports/total_sales"
AUCTION_PORTAL_URL = "https://sdttc.mytaxsale.com/"
ADDRAPN_QUERY_URL = (
    "https://gis-public.sandiegocounty.gov/arcgis/rest/services/"
    "sdep_warehouse/ADDRAPN/FeatureServer/0/query"
)

CURRENT_AUCTION_SALE_DATE = "March 13 - March 18, 2026"
CURRENT_AUCTION_APPROVAL_DATE = "December 9, 2025"
CURRENT_AUCTION_SOURCE_ID = "7102"

_PDF_ROW_RE = re.compile(
    r"(?<!\d)(\d{4})\s+"
    r"(\d{3}-\d{3}-\d{2}-\d{2})\s+"
    r"([^$\n]{2,120}?)\s+"
    r"\$([\d,]+)",
    re.S,
)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip(" ,.;")


def _money_or_blank(value: str) -> str:
    value = _clean(value)
    return value if re.search(r"\d", value) else ""


def _apn_key(apn: str) -> str:
    return re.sub(r"\D", "", apn or "")


def _format_number(value) -> str:
    if value in (None, "", 0, 0.0):
        return ""
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return str(value).strip()
    return str(value) if value else ""


def _address_from_attrs(attrs: dict) -> tuple[str, str, str]:
    number = _format_number(attrs.get("ADDRNMBR"))
    pieces = [
        number,
        _clean(attrs.get("ADDRFRAC")),
        _clean(attrs.get("ADDRPDIR")),
        _clean(attrs.get("ADDRNAME")),
        _clean(attrs.get("ADDRSFX")),
        _clean(attrs.get("ADDRPOSTD")),
    ]
    street = " ".join(p for p in pieces if p)
    unit = _clean(attrs.get("ADDRUNIT"))
    if unit:
        street = f"{street} #{unit}" if street else f"Unit {unit}"

    city = _clean(attrs.get("COMMUNITY")).title()
    zip_code = _clean(attrs.get("ADDRZIP"))
    return street, city, zip_code


class SanDiegoTaxSaleScraper(BaseScraper):
    county_name = "San Diego"
    base_url = CURRENT_AUCTION_NOTICE_URL

    def scrape(self) -> list[PropertyRecord]:
        today = str(date.today())
        records: list[PropertyRecord] = []

        records.extend(self._scrape_current_auction_notice(today))
        records.extend(self._scrape_prior_sales(today))
        self._enrich_situs_addresses(records)

        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                notes=(
                    "No San Diego tax-sale records found. Public auction portal "
                    "may require a registered bidder account for current downloads."
                ),
                source_url=AUCTION_PORTAL_URL,
                scraped_date=today,
                city="San Diego",
                state="CA",
            ))

        log.info(f"[{self.county_name}] Tax sale records: {len(records)}")
        return records

    def _enrich_situs_addresses(self, records: list[PropertyRecord]) -> None:
        apns = sorted({_apn_key(r.parcel_id) for r in records if _apn_key(r.parcel_id)})
        if not apns:
            return

        address_by_apn: dict[str, tuple[str, str, str]] = {}
        for i in range(0, len(apns), 75):
            chunk = apns[i:i + 75]
            where = "APN IN (" + ",".join(f"'{apn}'" for apn in chunk) + ")"
            try:
                resp = requests.post(
                    ADDRAPN_QUERY_URL,
                    data={
                        "f": "json",
                        "where": where,
                        "outFields": (
                            "APN,ADDRNMBR,ADDRFRAC,ADDRPDIR,ADDRNAME,ADDRPOSTD,"
                            "ADDRSFX,ADDRUNIT,ADDRZIP,STATE,COMMUNITY"
                        ),
                        "returnGeometry": "false",
                    },
                    timeout=45,
                )
                resp.raise_for_status()
                payload = resp.json()
            except (requests.RequestException, ValueError) as exc:
                log.warning(f"[{self.county_name}] SanGIS address lookup failed: {exc}")
                continue

            for feature in payload.get("features", []):
                attrs = feature.get("attributes") or {}
                apn = _apn_key(attrs.get("APN"))
                if not apn:
                    continue
                candidate = _address_from_attrs(attrs)
                current = address_by_apn.get(apn)
                # Prefer full numbered situs addresses, then named streets, then city-only.
                if not current or (candidate[0] and not current[0]) or (
                    candidate[0] and candidate[0][0].isdigit() and not current[0][:1].isdigit()
                ):
                    address_by_apn[apn] = candidate

        enriched = 0
        for record in records:
            street, city, zip_code = address_by_apn.get(_apn_key(record.parcel_id), ("", "", ""))
            if street:
                record.property_address = street
                enriched += 1
            if city:
                record.city = city
            if zip_code:
                record.zip_code = zip_code
            if street or city or zip_code:
                record.notes = f"{record.notes} | Situs address enriched from SanGIS ADDRAPN."

        log.info(
            f"[{self.county_name}] SanGIS address enrichment: "
            f"{enriched} street addresses for {len(records)} records"
        )

    def _scrape_current_auction_notice(self, today: str) -> list[PropertyRecord]:
        log.info(f"[{self.county_name}] Parsing current auction notice PDF...")
        text = self.pdf_text(CURRENT_AUCTION_NOTICE_URL)
        if not text:
            return []

        # Flatten the PDF text because the notice uses multi-column layout.
        flat_text = re.sub(r"\s+", " ", text)
        seen: set[tuple[str, str]] = set()
        records: list[PropertyRecord] = []

        for match in _PDF_ROW_RE.finditer(flat_text):
            item_no, apn, assessee, minimum_bid = match.groups()
            assessee = _clean(assessee)
            if not assessee:
                continue

            key = (item_no, apn)
            if key in seen:
                continue
            seen.add(key)

            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                owner_name=assessee,
                city="San Diego",
                state="CA",
                parcel_id=apn,
                amount_owed=f"${minimum_bid}",
                sale_date=CURRENT_AUCTION_SALE_DATE,
                case_number=f"Tax Sale {CURRENT_AUCTION_SOURCE_ID} Item {item_no}",
                source_url=CURRENT_AUCTION_NOTICE_URL,
                scraped_date=today,
                notes=(
                    "San Diego tax-defaulted property auction notice. "
                    f"Minimum bid: ${minimum_bid}. "
                    f"Board approval date: {CURRENT_AUCTION_APPROVAL_DATE}. "
                    "Public auction notice PDF; full auction-list download requires "
                    "a MyTaxSale user account."
                ),
            ))

        log.info(f"[{self.county_name}] Current auction notice records: {len(records)}")
        return records

    def _scrape_prior_sales(self, today: str) -> list[PropertyRecord]:
        log.info(f"[{self.county_name}] Fetching prior sales table...")
        resp = self.get(PRIOR_SALES_URL)
        if not resp:
            return []

        soup = self.soup(resp.text)
        table = soup.find("table")
        if not table:
            log.warning(f"[{self.county_name}] No prior-sales table found.")
            return []

        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else []
        records: list[PropertyRecord] = []

        for tr in rows:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 5:
                continue
            deed_id, apn, sale_date, opening_bid, winning_bid = cells[:5]
            note = cells[5] if len(cells) > 5 else ""
            if not apn:
                continue

            note_parts = [
                "Prior sale result, not a current lead.",
                f"Deed#: {deed_id}",
                f"Opening bid: {opening_bid}",
            ]
            winning_bid = _money_or_blank(winning_bid)
            opening_bid = _money_or_blank(opening_bid)
            if winning_bid:
                note_parts.append(f"Winning bid: {winning_bid}")
            if note:
                note_parts.append(note)

            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Tax Delinquent",
                parcel_id=apn,
                amount_owed=winning_bid or opening_bid,
                sale_date=sale_date,
                notes=" | ".join(note_parts),
                source_url=PRIOR_SALES_URL,
                scraped_date=today,
                city="San Diego",
                state="CA",
            ))

        log.info(f"[{self.county_name}] Prior sale records: {len(records)}")
        return records
