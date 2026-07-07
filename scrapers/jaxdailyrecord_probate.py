"""
Jax Daily Record probate + dissolution-of-marriage notices (Northeast FL).

Same legals.jaxdailyrecord.com portal as the foreclosure notice scraper, with
different Category filters:

  Category=Probate — Notice to Creditors postings (decedent, date of death,
  file number, personal representative name + address).

  Category=Notice+of+Action+-+Dissolution+of+Marriage — divorce constructive-
  service notices (petitioner, respondent + last-known address).

Notices for Duval, Clay, Nassau, and St. Johns counties appear in the same
feed; the county markers from the foreclosure scraper filter them.
"""
import re
from datetime import date

import requests

from .base_scraper import BaseScraper, PropertyRecord, log
from .fl_probate_divorce import _clean_party, _extract_pr_name
from .jaxdailyrecord_legalnotices import COUNTY_DEFAULT_CITY, COUNTY_MARKERS, _clean

PROBATE_URL = (
    "https://legals.jaxdailyrecord.com/public_notices/publicnotices.php"
    "?Category=Probate&mode=daily"
)
DISSOLUTION_URL = (
    "https://legals.jaxdailyrecord.com/public_notices/publicnotices.php"
    "?Category=Notice+of+Action+-+Dissolution+of+Marriage&mode=daily"
)

_DATE = r"[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4}"
_ESTATE_PATTERNS = [
    re.compile(r"administration of the estate of\s+(.{3,90}?),?\s*(?:deceased|a/k/a|whose date)", re.I),
    re.compile(r"IN RE:?\s*(?:THE\s+)?ESTATE OF\s+(.{3,90}?)(?:,|\s+Deceased|\s+a/k/a)", re.I),
    re.compile(r"Estate of\s+(.{3,90}?),?\s+deceased", re.I),
]
_DOD_RE = re.compile(r"date of death was\s+(" + _DATE + r")", re.I)
_FILE_RE = re.compile(r"(?:File|Case)\s+(?:No\.?|Number)[.:]?\s*([\w./-]{4,30})", re.I)
_CASE_NO_RE = re.compile(r"Case\s+No\.?:?\s*([\w-]{4,30})", re.I)
_PETITIONER_RE = re.compile(r"([A-Z][\w'. \-]{2,60}?)\s*,?\s*Petitioner", re.I)
_RESPONDENT_RE = re.compile(r"(?:And|and)\s+([A-Z][\w'. \-]{2,60}?)\s*,?\s*Respondent", re.I)
_TO_ADDR_RE = re.compile(r"\bTO:?\s+[A-Z][\w'. \-]{2,60}?\s+(\d{1,6}[\w .,'#\-]{4,90}?(?:FL|Florida)\.?,?\s*\d{5})", re.I)
_LAST_KNOWN_RE = re.compile(r"Last\s+known\s+(?:mailing\s+)?address[:\s]*(.{5,110}?)\s*(?:you are|YOU ARE|<)", re.I)
_DEADLINE_RE = re.compile(r"on or before\s+(" + _DATE + r")", re.I)
# City must follow a comma or " in " so street names don't leak into it.
_FL_CITY_ZIP_RE = re.compile(r"(?:,\s*|\bin\s+)([A-Za-z .'-]{2,40}?),?\s*(?:FL|Florida)\b\.?,?\s*(\d{5})", re.I)


class JaxDailyRecordCategoryScraper(BaseScraper):
    """Shared page-walk for a Jax Daily Record legal-notice category."""
    county_name = ""
    category_url = ""
    category_marker = ""    # substring the notice h3 title must contain
    record_type = ""

    def scrape(self) -> list[PropertyRecord]:
        today = str(date.today())
        try:
            resp = requests.get(self.category_url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException:
            return [self._stub(today, "source unavailable")]

        soup = self.soup(resp.text)
        records: list[PropertyRecord] = []
        seen: set[str] = set()
        markers = COUNTY_MARKERS[self.county_name]

        for heading in soup.find_all("h3"):
            title = heading.get_text(" ", strip=True)
            if self.category_marker not in title:
                continue
            cell = heading.find_parent("td")
            if not cell:
                continue
            text = cell.get_text("\n", strip=True)
            upper = text.upper()
            if not any(marker in upper for marker in markers):
                continue
            rec = self._parse_notice(title, text, today)
            if rec and rec.case_number not in seen:
                seen.add(rec.case_number)
                records.append(rec)

        log.info(f"[{self.county_name}] Jax Daily Record {self.record_type} notices: {len(records)}")
        if not records:
            return [self._stub(today, "no matching notices found")]
        return records

    def _parse_notice(self, title: str, text: str, today: str) -> PropertyRecord | None:
        raise NotImplementedError

    def _stub(self, today: str, reason: str) -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type=self.record_type,
            city=COUNTY_DEFAULT_CITY.get(self.county_name, ""),
            state="FL",
            source_url=self.category_url,
            scraped_date=today,
            notes=f"Jax Daily Record {self.record_type.lower()} notice scraper: {reason}.",
        )


class JaxDailyRecordProbateScraper(JaxDailyRecordCategoryScraper):
    category_url = PROBATE_URL
    base_url = PROBATE_URL
    category_marker = "Probate"
    record_type = "Probate"

    def _parse_notice(self, title: str, text: str, today: str) -> PropertyRecord | None:
        flat = _clean(text.replace("\n", " "))
        decedent = ""
        for pat in _ESTATE_PATTERNS:
            m = pat.search(flat)
            if m:
                decedent = _clean(m.group(1))
                break
        if not decedent:
            return None

        ref = _clean(title.replace("Probate", ""))
        file_m = _FILE_RE.search(flat)
        case_number = _clean(file_m.group(1)) if file_m else ref

        notes = ["Type: Notice to Creditors (probate)"]
        dod = _DOD_RE.search(flat)
        if dod:
            notes.append(f"Died: {_clean(dod.group(1))}")
        pr_name = _extract_pr_name(flat)
        if pr_name:
            notes.append(f"PR: {pr_name}")
        if ref:
            notes.append(ref)

        return PropertyRecord(
            county=self.county_name,
            record_type="Probate",
            owner_name=f"Estate of {decedent}",
            city=COUNTY_DEFAULT_CITY[self.county_name],
            state="FL",
            case_number=case_number,
            source_url=PROBATE_URL,
            scraped_date=today,
            notes=" | ".join(notes),
        )


class JaxDailyRecordDissolutionScraper(JaxDailyRecordCategoryScraper):
    category_url = DISSOLUTION_URL
    base_url = DISSOLUTION_URL
    category_marker = "Dissolution"
    record_type = "Divorce"

    def _parse_notice(self, title: str, text: str, today: str) -> PropertyRecord | None:
        flat = _clean(text.replace("\n", " "))
        pet_m = _PETITIONER_RE.search(flat)
        resp_m = _RESPONDENT_RE.search(flat)
        petitioner = _clean_party(pet_m.group(1)) if pet_m else ""
        respondent = _clean_party(resp_m.group(1)) if resp_m else ""
        owner = "; ".join(p for p in (respondent, petitioner) if p)
        if not owner:
            return None

        ref = _clean(re.sub(r"Notice of Action - Dissolution of Marriage", "", title, flags=re.I))
        case_m = _CASE_NO_RE.search(flat)
        case_number = _clean(case_m.group(1)) if case_m else ref

        address = city = zip_code = ""
        for pat in (_LAST_KNOWN_RE, _TO_ADDR_RE):
            m = pat.search(flat)
            if not m:
                continue
            candidate = _clean(m.group(1))
            if not re.search(r"\d+\s+[A-Za-z]", candidate):
                continue
            address = candidate
            cz = _FL_CITY_ZIP_RE.search(candidate)
            if cz:
                city = _clean(cz.group(1)).title()
                zip_code = cz.group(2)
            break

        notes = ["Type: Dissolution of Marriage (notice of action)"]
        if petitioner:
            notes.append(f"Petitioner: {petitioner}")
        if respondent:
            notes.append(f"Respondent: {respondent}")
        if ref:
            notes.append(ref)
        # Response deadline doubles as a stable record date for the listing id.
        deadline = _DEADLINE_RE.search(flat)
        sale_date = _clean(deadline.group(1)) if deadline else ""

        return PropertyRecord(
            county=self.county_name,
            record_type="Divorce",
            owner_name=owner,
            property_address=address,
            city=city or COUNTY_DEFAULT_CITY[self.county_name],
            state="FL",
            zip_code=zip_code,
            sale_date=sale_date,
            case_number=case_number,
            source_url=DISSOLUTION_URL,
            scraped_date=today,
            notes=" | ".join(notes),
        )


class DuvalJaxProbateScraper(JaxDailyRecordProbateScraper):
    county_name = "Duval"


class StJohnsJaxProbateScraper(JaxDailyRecordProbateScraper):
    county_name = "St. Johns"


class ClayJaxProbateScraper(JaxDailyRecordProbateScraper):
    county_name = "Clay"


class NassauJaxProbateScraper(JaxDailyRecordProbateScraper):
    county_name = "Nassau"


class DuvalJaxDissolutionScraper(JaxDailyRecordDissolutionScraper):
    county_name = "Duval"
