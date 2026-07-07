"""
Michigan probate-notice scrapers — mipublicnotices.com.

Michigan probate courts require Notice to Creditors publication in a local
newspaper when a decedent's estate is opened (MCL 700.3801). The same
mipublicnotices.com JSON API used for foreclosure notices indexes these under
three notice types:

  Probate              12e24602-...  (highest volume; Detroit Legal News etc.)
  Notice to Creditors  f3756055-...  (structured NTC postings)
  Trust                60b71268-...  (trust administration notices)

Each notice names the decedent, dates of birth/death, the probate case number,
and — the lead-gen payoff — the personal representative's name, mailing
address, and often a phone number. The PR (usually an heir) controls the
estate's real property.

County subclasses are generated from the same county/alias table as the
foreclosure scrapers, so probate coverage automatically matches foreclosure
coverage.
"""
import re
from datetime import date, timedelta

from .base_scraper import BaseScraper, PropertyRecord, log
from .mi_legalnotices_base import MILegalNoticesScraper
from . import mi_legalnotices_counties as _mi_counties

SEARCH_URL = "https://www.mipublicnotices.com/api/v1/search/search"
PORTAL_URL = "https://www.mipublicnotices.com/"

PROBATE_NOTICE_TYPES = {
    "12e24602-2e1d-4649-a394-c9429dbd1f64": "Probate",
    "f3756055-da12-485e-b83d-124b5007a6fb": "Notice to Creditors",
    "60b71268-e3b7-484d-98a5-1acb59b0ff1d": "Trust",
}

LOOKBACK_DAYS = 90
PER_PAGE = 50
MAX_PAGES = 30   # Wayne runs ~1,250 probate notices per 90 days

_DATE = r"(?:[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})"
_ESTATE_RE = re.compile(r"Estate of\s+([A-Z][A-Za-z .,'&\-]{2,80}?)(?:,|\s+Date of|\s+Deceased|\s+a/k/a|\s+aka\b)", re.I)
_MATTER_RE = re.compile(r"In the matter of\s+([A-Z][A-Za-z .,'&\-]{2,80}?)(?:,|\s+Decedent|\s+Deceased)", re.I)
_DECEDENT_RE = re.compile(r"The decedent,\s+(.{3,80}?),\s*(?:Deceased,\s*)?died", re.I)
_DIED_RE = re.compile(r"\bdied[:,]?\s*(" + _DATE + r")", re.I)
_DOB_RE = re.compile(r"Date of [Bb]irth:?\*?\s*(" + _DATE + r")", re.I)
_CASE_RE = re.compile(r"\b(\d{4}[-\s][\d,]{5,9}[-\s]?[A-Z]{2})\b")
_PR_NAME_RE = re.compile(
    r"presented to\s+(.{3,120}?),?\s+(?:(?:as\s+)?(?:co-)?personal\s+representative|or to both)", re.I)
_PR_BLOCK_RE = re.compile(
    r"[Pp]ersonal\s+[Rr]epresentative[s]?\s+"
    r"(\d{1,6}[\w .,'#\-]{4,70}?(?:,\s*)?(?:MI|Michigan)\.?\s*\d{5})", re.I)
_PHONE_RE = re.compile(r"\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}")
_TRUSTEE_RE = re.compile(r"presented to\s+(.{3,120}?),?\s+(?:as\s+)?(?:successor\s+)?trustee", re.I)


class MIProbateNoticesScraper(BaseScraper):
    """Override in subclasses: county_name, area_alias, default_city."""
    county_name: str = ""
    area_alias: str = ""
    default_city: str = ""
    state: str = "MI"
    base_url = PORTAL_URL

    def scrape(self) -> list[PropertyRecord]:
        today_d = date.today()
        today = str(today_d)
        lookback = max(1, min(365, int(getattr(self, "lookback_days", None) or LOOKBACK_DAYS)))
        first = today_d - timedelta(days=lookback)

        records: list[PropertyRecord] = []
        seen: set[str] = set()

        for type_id, type_label in PROBATE_NOTICE_TYPES.items():
            for page in range(1, MAX_PAGES + 1):
                params = {
                    "type": type_id,
                    "area": self.area_alias,
                    "from": first.isoformat(),
                    "to": today_d.isoformat(),
                    "per": PER_PAGE,
                    "page": page,
                }
                resp = self.get(SEARCH_URL, params=params)
                if not resp:
                    break
                try:
                    hits = resp.json().get("hits", [])
                except ValueError:
                    log.warning(f"[{self.county_name}] Non-JSON response for {type_label}")
                    break
                if not hits:
                    break
                for hit in hits:
                    rec = self._parse_hit(hit, type_label, today)
                    if rec and rec.case_number not in seen:
                        seen.add(rec.case_number)
                        records.append(rec)
                if len(hits) < PER_PAGE:
                    break

        log.info(f"[{self.county_name}] Probate notices: {len(records)} kept "
                 f"({first} to {today_d})")

        if not records:
            records.append(self._stub(today, lookback))
        return records

    def _parse_hit(self, hit: dict, type_label: str, today: str) -> PropertyRecord | None:
        notice = hit.get("notice") or {}
        notice_id = notice.get("id", "")
        title = re.sub(r"\s+", " ", notice.get("title", "") or "").strip()
        content = re.sub(r"\s+", " ", notice.get("content", "") or "")
        publication = (notice.get("publication") or {}).get("name", "")
        posted = (notice.get("firstDatePublished") or "")[:10]

        # Decedent name: prefer explicit "Estate of X" / "The decedent, X, died".
        decedent = ""
        for pat in (_ESTATE_RE, _DECEDENT_RE, _MATTER_RE):
            m = pat.search(content) or pat.search(title)
            if m:
                decedent = m.group(1).strip(" ,.")
                break
        if not decedent and title and not title.upper().startswith("STATE OF"):
            decedent = title  # plain-name titles like "MARLENE E. CANNON"
        if not decedent:
            return None

        case_m = _CASE_RE.search(content) or _CASE_RE.search(title)
        case_number = case_m.group(1).replace(" ", "-") if case_m else notice_id

        note_parts = [f"Type: {type_label}"]
        died = _DIED_RE.search(content)
        if died:
            note_parts.append(f"Died: {died.group(1)}")
        dob = _DOB_RE.search(content)
        if dob:
            note_parts.append(f"DOB: {dob.group(1)}")

        pr = _PR_NAME_RE.search(content)
        if pr:
            pr_name = re.sub(r"\s+", " ", pr.group(1)).strip(" ,.")
            if 2 < len(pr_name) <= 120:
                note_parts.append(f"PR: {pr_name}")
        elif type_label == "Trust":
            tr = _TRUSTEE_RE.search(content)
            if tr:
                note_parts.append(f"Trustee: {tr.group(1).strip(' ,.')}")

        pr_addr = _PR_BLOCK_RE.search(content)
        if pr_addr:
            note_parts.append(f"PR address: {pr_addr.group(1).strip(' ,.')}")
            tail = content[pr_addr.end():pr_addr.end() + 40]
            phone = _PHONE_RE.search(tail)
            if phone:
                note_parts.append(f"PR phone: {phone.group(0)}")

        if publication:
            note_parts.append(f"Publication: {publication}")
        if posted:
            note_parts.append(f"Posted: {posted}")

        return PropertyRecord(
            county=self.county_name,
            record_type="Probate",
            owner_name=f"Estate of {decedent}",
            property_address="",
            city=self.default_city,
            state=self.state,
            zip_code="",
            sale_date=posted,   # publication date — stable across scrape runs
            case_number=case_number,
            source_url=PORTAL_URL,
            scraped_date=today,
            notes=" | ".join(note_parts),
        )

    def _stub(self, today: str, lookback: int) -> PropertyRecord:
        return PropertyRecord(
            county=self.county_name,
            record_type="Probate",
            state=self.state,
            city=self.default_city,
            notes=(f"No {self.county_name} County MI probate notices found in last "
                   f"{lookback} days. Browse at {PORTAL_URL}"),
            source_url=PORTAL_URL,
            scraped_date=today,
        )


def _county_specs() -> list[tuple[str, str, str]]:
    """(county_name, area_alias, default_city) from the foreclosure county classes."""
    specs = []
    for obj in vars(_mi_counties).values():
        if (isinstance(obj, type) and issubclass(obj, MILegalNoticesScraper)
                and obj is not MILegalNoticesScraper and obj.county_name):
            specs.append((obj.county_name, obj.area_alias, obj.default_city))
    specs.append(("Barry", "8", "Hastings"))   # Barry lives in barry_legalnotices.py
    return specs


def _make_class(county: str, alias: str, city: str) -> type:
    cls_name = f"{county.replace(' ', '').replace('.', '')}ProbateNoticesScraper"
    return type(cls_name, (MIProbateNoticesScraper,), {
        "county_name": county,
        "area_alias": alias,
        "default_city": city,
        "__doc__": f"{county} County MI probate notices — alias {alias}.",
    })


# source key ("wayne_probate") -> scraper class, for source_registry
MI_PROBATE_SCRAPERS: dict[str, type] = {
    f"{county.lower().replace(' ', '').replace('.', '')}_probate": _make_class(county, alias, city)
    for county, alias, city in _county_specs()
}
