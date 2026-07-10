"""
Florida tax-deed application notices — FloridaPublicNotices.com.

Reuses the FLPublicNoticesScraper fetch loop (county filter + date window +
keyword search) with the '"tax deed"' keyword and a parser for the statutory
Notice of Application for Tax Deed (Florida Statutes 197.512). A certificate
holder has applied for a tax deed: unless the owner redeems the delinquent
taxes, the property is sold at a scheduled tax-deed sale. Every notice carries
the certificate number/year, the parcel ("Description"), the owner ("Name In
Which Assessed"), and the sale date — no street address, so these leads rely
on Parcel ID + County skip tracing.

Gotchas found while sampling (2026-07):
  * Newspapers circulate across county lines, so a search scoped to one county
    returns notices for neighbors (a Palm Beach search surfaced Hendry County
    parcels). The inherited county_pattern gate handles this.
  * The keyword also matches surplus-funds interpleader suits that merely
    mention a past "tax deed sale" — the statutory heading marker filters
    those out.
The keyword phrase must be wrapped in double quotes: the site's search treats
an unquoted multi-word query as an OR of the words.
"""
import re
from datetime import datetime

from .base_scraper import PropertyRecord
from .fl_publicnotices import (
    SEARCH_URL,
    BrowardPublicNoticesScraper,
    FLPublicNoticesScraper,
    HillsboroughPublicNoticesScraper,
    MiamiDadePublicNoticesScraper,
    OrangeFLPublicNoticesScraper,
    PalmBeachPublicNoticesScraper,
    _clean,
)

_TAXDEED_MARKER = re.compile(r"NOTICE\s+OF\s+APPLICATION\s+FOR\s+TAX\s+DEED", re.I)
_FILE_RE = re.compile(r"FILE\s*(?:NO\.?|NUMBER|#)[:.\s]*([\w./-]{2,30})", re.I)
# Value must start with a digit or the regex grabs "and" out of the statutory
# prose "The Certificate number and year of issuance ... are as follows".
_CERT_RE = re.compile(r"Certificate\s+(?:No\.?|Number)[:.\s]*(\d[\w/-]{0,19})", re.I)
_CERT_YEAR_RE = re.compile(r"Year(?:\s+of\s+Issuance)?[:.\s]*(\d{4})", re.I)
# Parcel wording varies by clerk: Orange prints "PARCEL ID # 18-22-28-...",
# smaller counties put the bare parcel right after "Description:".
_PARCEL_PATTERNS = [
    re.compile(r"Parcel\s*(?:ID|No\.?|Number)?\s*#?[:.\s]*(\d[\d\-A-Z./]{5,40})", re.I),
    re.compile(r"Folio\s*(?:No\.?|Number)?\s*#?[:.\s]*(\d[\d\-A-Z./]{5,40})", re.I),
    re.compile(r"Description(?:\s+of\s+(?:the\s+)?Property)?[:.\s]*(\d[\d\-A-Z./ ]{5,40}?)(?=\s+[A-Z][a-z]|\s+Name|\s*$)", re.I),
]
# The statutory prose repeats the phrase ("...the names in which it was
# assessed are as follows: ...") before the labeled field appears, so the
# parser takes the LAST match and rejects boilerplate captures.
_ASSESSED_RE = re.compile(
    r"Name(?:s|\(s\))?\s+In\s+Which\s+(?:It\s+Was\s+)?Assessed[:.\s]*(.{3,120})", re.I)
_ASSESSED_JUNK = re.compile(
    r"^(?:are|is)\b|certificate|folio|number|as follows", re.I)
# Legal-description boilerplate that immediately follows the assessed name.
_ASSESSED_STOP = re.compile(
    r"\s+(?:Lot\b|Lots\b|Unit\b|Block\b|Parcel\b|Tract\b|Section\b|Township\b|"
    r"A\s+Subdivision|According\b|All\s+(?:that|of)\b|Condominium\b|Plat\b|PB\b|Beg\w*\b|"
    r"Com\w*\b|Unless\b|Said\b|Legal\b|The\s+[NSEW]\b|[NSEW]\s*\d|\d+\s*/\s*\d).*$",
    re.I,
)
# Prefer the explicit "which is the 13th day of August, 2026" the statute
# requires; fall back to "sold ... on the <N> day of <Month>, <Year>" and to
# Orange's abbreviated "scheduled to begin at 10:00 a.m. ET, Aug 20, 2026".
# A bare "day of" pattern would wrongly match the "Dated this ..." clerk line.
_SALE_DATE_PATTERNS = [
    re.compile(r"which\s+is\s+the\s+(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+([A-Z][a-z]+),?\s+(\d{4})", re.I),
    re.compile(r"will\s+be\s+sold\b.{0,200}?\bon\s+the\s+(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+([A-Z][a-z]+),?\s+(\d{4})", re.I | re.S),
    re.compile(r"will\s+be\s+sold\b.{0,250}?\b([A-Z][a-z]{2,8})\.?\s+(\d{1,2}),?\s+(\d{4})", re.I | re.S),
]
_SALE_SITE_RE = re.compile(r"(https?://[\w.-]*realtaxdeed\.com\S*)", re.I)


class FLTaxDeedNoticesScraper(FLPublicNoticesScraper):
    """Notice of Application for Tax Deed (FS 197.512) via FloridaPublicNotices.com."""
    search_keyword = '"tax deed"'
    notice_label = "tax deed"

    def _parse_notice(self, notice: dict, today: str) -> PropertyRecord | None:
        text = _clean(notice.get("notice", ""))
        if not _TAXDEED_MARKER.search(text):
            return None    # surplus-funds suits etc. that merely mention a tax deed
        if not re.search(self.county_pattern, text, re.I):
            return None    # neighboring-county notice carried by the same paper

        notice_id = str(notice.get("id") or "")
        file_m = _FILE_RE.search(text)
        cert_m = _CERT_RE.search(text)
        cert_no = _clean(cert_m.group(1)) if cert_m else ""
        # File # restarts yearly per county; add the certificate for a stable key.
        case_number = _clean(file_m.group(1)) if file_m else notice_id
        if cert_no:
            case_number = f"{case_number}/cert{cert_no}"

        owner = ""
        for assessed_m in _ASSESSED_RE.finditer(text):
            candidate = _clean(_ASSESSED_STOP.sub("", assessed_m.group(1)))
            if candidate and not _ASSESSED_JUNK.search(candidate):
                owner = candidate    # keep the last non-boilerplate match
        if not owner:
            return None

        parcel = ""
        for pattern in _PARCEL_PATTERNS:
            parcel_m = pattern.search(text)
            if parcel_m:
                parcel = _clean(parcel_m.group(1))
                break

        sale_date = ""
        for pattern in _SALE_DATE_PATTERNS:
            m = pattern.search(text)
            if not m:
                continue
            # "day of" patterns capture (day, Month, year); the abbreviated
            # fallback captures (Month, day, year) — a digit first group tells
            # them apart.
            if m.group(1).isdigit():
                raw = f"{m.group(2)} {m.group(1)}, {m.group(3)}"
            else:
                raw = f"{m.group(1)} {m.group(2)}, {m.group(3)}"
            raw = re.sub(r"\s+", " ", raw).strip()
            for fmt in ("%B %d, %Y", "%b %d, %Y"):
                try:
                    sale_date = datetime.strptime(raw, fmt).date().isoformat()
                    break
                except ValueError:
                    pass
            if sale_date:
                break

        notes = ["Type: Tax deed application (FS 197.512) — owner can redeem until sale"]
        if cert_no:
            year_m = _CERT_YEAR_RE.search(text)
            notes.append(f"Certificate: {cert_no}" + (f" ({year_m.group(1)})" if year_m else ""))
        site_m = _SALE_SITE_RE.search(text)
        if site_m:
            notes.append(f"Sale site: {site_m.group(1).rstrip('.,')}")
        if notice.get("paper"):
            notes.append(f"Publication: {notice['paper']}")
        if notice.get("date"):
            notes.append(f"Posted: {notice['date']}")

        source_path = (notice.get("_links", {}).get("self", {}) or {}).get("href", "")
        return PropertyRecord(
            county=self.county_name,
            record_type="Tax Sale",
            owner_name=owner,
            property_address="",
            city=self.default_city,
            state=self.state,
            parcel_id=parcel,
            sale_date=sale_date or str(notice.get("date") or ""),
            case_number=case_number,
            source_url=f"https://floridapublicnotices.com{source_path}" if source_path else SEARCH_URL,
            scraped_date=today,
            notes=" | ".join(notes),
        )

    def _stub(self, today: str) -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type="Tax Sale",
            city=self.default_city,
            state=self.state,
            source_url=SEARCH_URL,
            scraped_date=today,
            notes=f"No matching {self.county_name} County tax deed notices found.",
        )


# ── County subclasses — county config inherited from the foreclosure classes ─

class BrowardTaxDeedScraper(FLTaxDeedNoticesScraper, BrowardPublicNoticesScraper):
    pass


class MiamiDadeTaxDeedScraper(FLTaxDeedNoticesScraper, MiamiDadePublicNoticesScraper):
    pass


class PalmBeachTaxDeedScraper(FLTaxDeedNoticesScraper, PalmBeachPublicNoticesScraper):
    pass


class OrangeFLTaxDeedScraper(FLTaxDeedNoticesScraper, OrangeFLPublicNoticesScraper):
    pass


class HillsboroughTaxDeedScraper(FLTaxDeedNoticesScraper, HillsboroughPublicNoticesScraper):
    pass
