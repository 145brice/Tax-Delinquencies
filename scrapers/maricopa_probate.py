"""
Maricopa County AZ probate notices from The Record Reporter PDFs.

Each Record Reporter weekday edition carries ~24 Notice to Creditors filings
(A.R.S. § 14-3803: an estate was opened; creditors have 4 months from first
publication). Each names the decedent — whose property the estate now
controls — plus the case number and usually the personal representative.
That makes them heir/estate seller leads, same as the MI and FL probate
sources.

The paper is set in irregular narrow columns, so a single column-count crop
garbles many blocks (neighboring-column bleed). This parser runs several
column-count passes plus the raw page text, keeps every block whose case
number and decedent survive extraction, and dedups on case number — the same
tolerant philosophy as the trustee-sale parser for this paper.
"""
import io
import re
from datetime import date

import pdfplumber

from .base_scraper import BaseScraper, PropertyRecord, log
from .maricopa_recordreporter import SOURCE_URL, _candidate_issue_dates, _clean, _pdf_url

_MARKER = re.compile(r"NOTICE\s+TO\s+CREDITORS", re.I)
# Maricopa Superior Court probate dockets: "CASE NO. PB 2026-004684" /
# "NO. PB2026-004587" — tolerate the OCR-ish spacing the PDFs produce.
_CASE_RE = re.compile(r"\bPB\s?-?\s?(\d{4})\s?-\s?(\d{4,7})", re.I)
_ESTATE_RE = re.compile(
    r"In\s+the\s+Matter\s+of\s+the\s+Estate\s+of[:\s]+([A-Z][A-Za-z .,'\-]{2,60}?),?\s+(?:Deceas|aka|a/k/a)", re.I)
# Trust variant: "In the Matter of the <Name> Trust" (settlor died; successor
# trustee now controls the trust's property).
_TRUST_RE = re.compile(
    r"In\s+the\s+Matter\s+of\s+the\s+([A-Z][A-Za-z .,'\-]{2,60}?\s+(?:Living\s+|Family\s+)?Trust)\b", re.I)
_PR_PATTERNS = [
    re.compile(r"([A-Z][A-Za-z.'\- ]{2,50}?)\s+has\s+been\s+appoin", re.I),
    re.compile(r"Representative\s+at:?\s+([A-Z][A-Za-z.'\- ]{2,50}?)(?=,|\s+c/o|\s+\d)", re.I),
    re.compile(r"([A-Z][A-Za-z.'\- ]{2,50}?),?\s+(?:as\s+)?Personal\s+Representative", re.I),
]
_PR_BOILERPLATE = re.compile(
    r"\b(?:undersigned|personal|representative|estate|notice|claims?|court|"
    r"appointed|the|all|persons|state|county|hereby|given|informal|formal|"
    r"appointment|application|publication)\b", re.I)
_BLOCK_CHARS = 1200

# Column bleed injects fragments of neighboring notices into the captured name
# span ("egal advice from a EILEEN MILLAR"). The decedent is set in caps in
# this paper, so keep the longest run of consecutive name-like tokens,
# preferring all-caps runs; commas bound a run (drops ", DAT"/", LLC" bleed).
_NAME_STOPWORDS = {
    "the", "and", "of", "from", "about", "advice", "court", "notice", "dated",
    "estate", "deceased", "matter", "case", "state", "county", "arizona",
    "superior", "publication", "llc", "dat", "not",
}


def _best_name_run(raw: str) -> str:
    def runs(tokens, want_caps):
        found, current = [], []
        for tok in tokens:
            word = tok.strip(".,'")
            ok = (re.fullmatch(r"[A-Z][A-Za-z.'\-]*,?", tok)
                  and word.lower() not in _NAME_STOPWORDS
                  and (not want_caps or word.upper() == word))
            if ok:
                current.append(tok.rstrip(","))
                if tok.endswith(","):        # comma closes the run
                    found.append(current)
                    current = []
            else:
                if current:
                    found.append(current)
                current = []
        if current:
            found.append(current)
        return found

    tokens = raw.split()
    for want_caps in (True, False):
        candidates = [r for r in runs(tokens, want_caps) if len(r) >= 2]
        if candidates:
            return " ".join(max(candidates, key=len))
    return ""


def _extract_pr(chunk: str) -> str:
    for pat in _PR_PATTERNS:
        for m in pat.finditer(chunk):
            name = _best_name_run(_clean(m.group(1)))
            words = name.split()
            if len(words) >= 2 and len(name) >= 6 and not _PR_BOILERPLATE.search(name):
                return name
    return ""


class MaricopaProbateScraper(BaseScraper):
    county_name = "Maricopa"
    base_url = SOURCE_URL

    def scrape(self) -> list[PropertyRecord]:
        today = date.today()
        today_str = str(today)
        records: list[PropertyRecord] = []
        seen_cases: set[str] = set()

        for issue_date in _candidate_issue_dates(today, getattr(self, "lookback_days", None)):
            for record in self._scrape_issue(_pdf_url(issue_date), issue_date, today_str):
                if record.case_number in seen_cases:
                    continue
                seen_cases.add(record.case_number)
                records.append(record)

        log.info(f"[{self.county_name}] Record Reporter probate notices: {len(records)}")
        if not records:
            records.append(PropertyRecord(
                county=self.county_name,
                record_type="Probate",
                city="Phoenix",
                state="AZ",
                source_url=SOURCE_URL,
                scraped_date=today_str,
                notes="No Maricopa probate notices found in recent Record Reporter PDFs.",
            ))
        return records

    def _scrape_issue(self, url: str, issue_date: date, today: str) -> list[PropertyRecord]:
        resp = self.get(url)
        if not resp:
            return []

        texts: list[str] = []
        try:
            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                for page in pdf.pages:
                    texts.append(page.extract_text(x_tolerance=1, y_tolerance=3) or "")
                    width, height = page.width, page.height
                    for ncols in (4, 5):
                        for col in range(ncols):
                            crop = page.crop((col * width / ncols, 0,
                                              (col + 1) * width / ncols, height))
                            texts.append(crop.extract_text(x_tolerance=1, y_tolerance=3) or "")
        except Exception as exc:
            log.warning(f"[{self.county_name}] Record Reporter PDF parse failed for {url}: {exc}")
            return []

        records: list[PropertyRecord] = []
        seen_cases: set[str] = set()
        for text in texts:
            text = re.sub(r"\s+", " ", re.sub(r"[’‘]", "'", text))
            for m in _MARKER.finditer(text):
                chunk = text[m.start():m.start() + _BLOCK_CHARS]
                record = self._parse_notice(chunk, url, issue_date, today)
                if record and record.case_number not in seen_cases:
                    seen_cases.add(record.case_number)
                    records.append(record)
        return records

    def _parse_notice(self, chunk: str, url: str, issue_date: date, today: str) -> PropertyRecord | None:
        case_m = _CASE_RE.search(chunk)
        if not case_m:
            return None    # bled-through block: unusable without a stable id
        case_number = f"PB{case_m.group(1)}-{case_m.group(2)}"

        estate_m = _ESTATE_RE.search(chunk)
        trust_m = None if estate_m else _TRUST_RE.search(chunk)
        if estate_m:
            decedent = _best_name_run(_clean(estate_m.group(1)))
            if not decedent:
                return None
            owner = f"Estate of {decedent.title()}"
            kind = "estate"
        elif trust_m:
            owner = _clean(trust_m.group(1))
            kind = "trust"
        else:
            return None    # decedent name lost to column bleed

        notes = [f"Record Reporter issue: {issue_date:%Y-%m-%d}",
                 "Type: Notice to Creditors (A.R.S. 14-3803)" if kind == "estate"
                 else "Type: Notice to Creditors (trust administration)"]
        pr = _extract_pr(chunk)
        if pr:
            notes.append(f"PR/Trustee: {pr}")

        return PropertyRecord(
            county=self.county_name,
            record_type="Probate",
            owner_name=owner,
            property_address="",
            city="Phoenix",
            state="AZ",
            sale_date=f"{issue_date:%Y-%m-%d}",   # publication date — stable id basis
            case_number=case_number,
            source_url=url,
            scraped_date=today,
            notes=" | ".join(notes),
        )
