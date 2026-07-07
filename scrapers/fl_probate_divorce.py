"""
Florida probate + divorce notice scrapers — FloridaPublicNotices.com.

Reuses the FLPublicNoticesScraper fetch loop (county filter + date window +
keyword search) with different keywords and parsers:

  Probate — Florida Statutes 733.2121 requires a Notice to Creditors to be
  published when an estate is opened. Notices carry the decedent's name, date
  of death, probate file number, and the personal representative's name and
  mailing address (usually an heir who now controls the estate's property).

  Divorce — Notices of Action for Dissolution of Marriage (constructive
  service under FS 49.011) name petitioner and respondent, the case number,
  and usually the respondent's last-known street address and the petitioner's
  current address — both actionable for pre-listing outreach.

The keyword phrase must be wrapped in double quotes: the site's search treats
an unquoted multi-word query as an OR of the words.
"""
import re

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

_DATE = r"[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4}"

# ── Probate (Notice to Creditors) patterns ──────────────────────────────────
_NTC_MARKER = re.compile(r"NOTICE\s+TO\s+CREDITORS", re.I)
_ESTATE_PATTERNS = [
    re.compile(r"administration of the estate of\s+(.{3,90}?),?\s*(?:deceased|a/k/a|whose date)", re.I),
    re.compile(r"IN RE:?\s*(?:THE\s+)?ESTATE OF\s+(.{3,90}?)(?:,|\s+Deceased|\s+a/k/a)", re.I),
    re.compile(r"Estate of\s+(.{3,90}?),?\s+deceased", re.I),
]
_DOD_RE = re.compile(r"date of death (?:was|is)\s+(" + _DATE + r")", re.I)
_FILE_RE = re.compile(r"(?:File|Case)\s+(?:No\.?|Number)[.:]?\s*([\w./-]{4,30})", re.I)
# PR name appears either as "Personal Representative: /s/ NAME 123 Street" or
# "NAME, Personal Representative"; both must dodge the statutory boilerplate
# ("the names and addresses of the personal representative and the personal
# representative's attorney are set forth below").
_PR_AFTER_RE = re.compile(
    r"Personal Representative[:.]\s*(?:/s/\s*)?([A-Z][A-Za-z.,'\- ]{2,60}?)(?=\s+\d|\s*$)")
_PR_BEFORE_RE = re.compile(
    r"([A-Z][A-Za-z.,'\- ]{2,60}?),?\s+(?:as\s+)?(?:Co-)?Personal Representative")
_PR_BOILERPLATE = re.compile(
    r"\b(?:attorney|names?\s+and|address(?:es)?|set\s+forth|personal\s+representative|"
    r"copy\s+of|and\s+the|or\s+to|the\s+names)\b", re.I)
_PR_STOP = re.compile(r"\s+(?:Florida\s+Bar|Esq\b|P\.?\s?A\.?\b|Attorney|LLC|PLLC).*$", re.I)
_PR_ADDR_RE = re.compile(
    r"Personal Representative[:.]?\s+.{0,90}?(\d{1,6}[\w .,'#\-]{4,80}?(?:,\s*)?(?:FL|Florida)\.?\s*\d{5})", re.I)


def _extract_pr_name(text: str) -> str:
    for pat in (_PR_AFTER_RE, _PR_BEFORE_RE):
        for m in pat.finditer(text):
            name = _clean(_PR_STOP.sub("", m.group(1)))
            if 2 < len(name) <= 60 and not _PR_BOILERPLATE.search(name):
                return name
    return ""


def _clean_party(name: str) -> str:
    """Drop court-division tokens that bleed into party names (FM-G, Family 33)."""
    words = _clean(name).split()
    while words and (re.search(r"\d", words[0])
                     or re.fullmatch(r"(?i)(?:FM-?\w*|Family|Division|Case|No\.?|IN|RE:?)", words[0])):
        words.pop(0)
    return " ".join(words)

# ── Divorce (Notice of Action - Dissolution of Marriage) patterns ───────────
_DISSOLUTION_MARKER = re.compile(r"dissolution\s+of\s+marriage", re.I)
_CASE_NO_RE = re.compile(r"Case\s+No\.?:?\s*([\w-]{4,30})", re.I)
_PETITIONER_RE = re.compile(r"([A-Z][\w'. \-]{2,60}?)\s*,?\s*Petitioner", re.I)
_RESPONDENT_RE = re.compile(r"(?:And|and)\s+([A-Z][\w'. \-]{2,60}?)\s*,?\s*Respondent", re.I)
_LAST_KNOWN_RE = re.compile(r"Last\s+known\s+(?:mailing\s+)?address[:\s]*(.{5,110}?)\s*(?:you are|YOU ARE)", re.I)
_PET_ADDR_RE = re.compile(r"whose address is\s+(.{5,110}?)\s+(?:on or before|within)", re.I)
_DEADLINE_RE = re.compile(r"on or before\s+(" + _DATE + r")", re.I)
# City must follow a comma or " in " so street names don't leak into it.
_FL_CITY_ZIP_RE = re.compile(r"(?:,\s*|\bin\s+)([A-Za-z .'-]{2,40}?),?\s*(?:FL|Florida)\b\.?,?\s*(\d{5})", re.I)


class FLProbateNoticesScraper(FLPublicNoticesScraper):
    """Notice to Creditors (estate administration) via FloridaPublicNotices.com."""
    search_keyword = '"notice to creditors"'
    notice_label = "probate"

    def _parse_notice(self, notice: dict, today: str) -> PropertyRecord | None:
        text = _clean(notice.get("notice", ""))
        if not re.search(self.county_pattern, text, re.I):
            return None
        if not _NTC_MARKER.search(text):
            return None

        decedent = ""
        for pat in _ESTATE_PATTERNS:
            m = pat.search(text)
            if m:
                decedent = _clean(m.group(1))
                break
        if not decedent:
            return None

        notice_id = str(notice.get("id") or "")
        file_m = _FILE_RE.search(text)
        case_number = _clean(file_m.group(1)) if file_m else notice_id

        notes = ["Type: Notice to Creditors (probate)"]
        dod = _DOD_RE.search(text)
        if dod:
            notes.append(f"Died: {_clean(dod.group(1))}")
        pr_name = _extract_pr_name(text)
        if pr_name:
            notes.append(f"PR: {pr_name}")
        pr_addr = _PR_ADDR_RE.search(text)
        if pr_addr:
            notes.append(f"PR address: {_clean(pr_addr.group(1))}")
        if notice.get("paper"):
            notes.append(f"Publication: {notice['paper']}")
        if notice.get("date"):
            notes.append(f"Posted: {notice['date']}")

        source_path = (notice.get("_links", {}).get("self", {}) or {}).get("href", "")
        return PropertyRecord(
            county=self.county_name,
            record_type="Probate",
            owner_name=f"Estate of {decedent}",
            property_address="",
            city=self.default_city,
            state=self.state,
            sale_date=str(notice.get("date") or ""),  # publication date — stable id basis
            case_number=case_number,
            source_url=f"https://floridapublicnotices.com{source_path}" if source_path else SEARCH_URL,
            scraped_date=today,
            notes=" | ".join(notes),
        )

    def _stub(self, today: str) -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type="Probate",
            city=self.default_city,
            state=self.state,
            source_url=SEARCH_URL,
            scraped_date=today,
            notes=f"No matching {self.county_name} County probate notices found.",
        )


class FLDivorceNoticesScraper(FLPublicNoticesScraper):
    """Notices of Action for Dissolution of Marriage via FloridaPublicNotices.com."""
    search_keyword = '"dissolution of marriage"'
    notice_label = "divorce"

    def _parse_notice(self, notice: dict, today: str) -> PropertyRecord | None:
        text = _clean(notice.get("notice", ""))
        if not re.search(self.county_pattern, text, re.I):
            return None
        if not _DISSOLUTION_MARKER.search(text):
            return None

        notice_id = str(notice.get("id") or "")
        case_m = _CASE_NO_RE.search(text)
        case_number = _clean(case_m.group(1)) if case_m else notice_id

        pet_m = _PETITIONER_RE.search(text)
        resp_m = _RESPONDENT_RE.search(text)
        petitioner = _clean_party(pet_m.group(1)) if pet_m else ""
        respondent = _clean_party(resp_m.group(1)) if resp_m else ""
        owner = "; ".join(p for p in (respondent, petitioner) if p)
        if not owner:
            return None

        # Prefer the respondent's last-known street address (the marital home in
        # most publication cases); fall back to the petitioner's address.
        address = city = zip_code = ""
        for pat in (_LAST_KNOWN_RE, _PET_ADDR_RE):
            m = pat.search(text)
            if not m:
                continue
            candidate = _clean(m.group(1))
            if not re.search(r"\d+\s+[A-Za-z]", candidate):
                continue    # "Haiti", "Address unknown", etc.
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
        pet_addr = _PET_ADDR_RE.search(text)
        if pet_addr:
            notes.append(f"Petitioner address: {_clean(pet_addr.group(1))}")
        deadline = _DEADLINE_RE.search(text)
        if deadline:
            notes.append(f"Response due: {_clean(deadline.group(1))}")
        if notice.get("paper"):
            notes.append(f"Publication: {notice['paper']}")
        if notice.get("date"):
            notes.append(f"Posted: {notice['date']}")

        source_path = (notice.get("_links", {}).get("self", {}) or {}).get("href", "")
        return PropertyRecord(
            county=self.county_name,
            record_type="Divorce",
            owner_name=owner,
            property_address=address,
            city=city or self.default_city,
            state=self.state,
            zip_code=zip_code,
            sale_date=str(notice.get("date") or ""),  # publication date — stable id basis
            case_number=case_number,
            source_url=f"https://floridapublicnotices.com{source_path}" if source_path else SEARCH_URL,
            scraped_date=today,
            notes=" | ".join(notes),
        )

    def _stub(self, today: str) -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type="Divorce",
            city=self.default_city,
            state=self.state,
            source_url=SEARCH_URL,
            scraped_date=today,
            notes=f"No matching {self.county_name} County divorce notices found.",
        )


# ── County subclasses — county config inherited from the foreclosure classes ─

class BrowardProbateScraper(FLProbateNoticesScraper, BrowardPublicNoticesScraper):
    pass


class MiamiDadeProbateScraper(FLProbateNoticesScraper, MiamiDadePublicNoticesScraper):
    pass


class PalmBeachProbateScraper(FLProbateNoticesScraper, PalmBeachPublicNoticesScraper):
    pass


class OrangeFLProbateScraper(FLProbateNoticesScraper, OrangeFLPublicNoticesScraper):
    pass


class HillsboroughProbateScraper(FLProbateNoticesScraper, HillsboroughPublicNoticesScraper):
    pass


class BrowardDivorceScraper(FLDivorceNoticesScraper, BrowardPublicNoticesScraper):
    pass


class MiamiDadeDivorceScraper(FLDivorceNoticesScraper, MiamiDadePublicNoticesScraper):
    pass


class PalmBeachDivorceScraper(FLDivorceNoticesScraper, PalmBeachPublicNoticesScraper):
    pass


class OrangeFLDivorceScraper(FLDivorceNoticesScraper, OrangeFLPublicNoticesScraper):
    pass


class HillsboroughDivorceScraper(FLDivorceNoticesScraper, HillsboroughPublicNoticesScraper):
    pass
