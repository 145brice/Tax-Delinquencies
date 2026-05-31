"""Base scraper - human-like request pacing, rotating UA, PDF parsing, output schema."""
import io
import time
import random
import requests
import pdfplumber
from bs4 import BeautifulSoup
from dataclasses import dataclass, asdict
from typing import Optional
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Rotate through real browser UA strings so no single UA hammers a server
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

_BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "DNT": "1",
}

# Per-domain request tracking to avoid hammering the same host
_domain_last_request: dict[str, float] = {}
# Minimum seconds between requests to the same domain
_MIN_DOMAIN_GAP = 3.0
# Max extra random jitter on top of minimum
_MAX_JITTER = 4.0


def _domain(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc


def _polite_wait(url: str):
    """Enforce a minimum gap between requests to the same domain, with jitter.
    Sleeps in small chunks so a kill request can interrupt it."""
    dom = _domain(url)
    now = time.time()
    last = _domain_last_request.get(dom, 0)
    gap = now - last
    wait = _MIN_DOMAIN_GAP + random.uniform(0, _MAX_JITTER)
    if gap < wait:
        sleep_for = wait - gap
        log.debug(f"Rate-limiting {dom}: sleeping {sleep_for:.1f}s")
        # Interruptible sleep - wake every 0.2s to check kill flag
        end = time.time() + sleep_for
        while time.time() < end:
            if _kill_flag["set"]:
                raise ScraperKilled("Kill requested during rate-limit wait")
            time.sleep(min(0.2, end - time.time()))
    _domain_last_request[dom] = time.time()


@dataclass
class PropertyRecord:
    county: str = ""
    record_type: str = ""          # "Tax Delinquent" | "Pre-Foreclosure"
    owner_name: str = ""
    property_address: str = ""
    city: str = ""
    state: str = "TN"
    zip_code: str = ""
    parcel_id: str = ""
    tax_year: str = ""
    amount_owed: str = ""
    sale_date: str = ""
    case_number: str = ""
    source_url: str = ""
    scraped_date: str = ""
    notes: str = ""

    def to_dict(self):
        return asdict(self)


class ScraperKilled(Exception):
    """Raised when a scraper is killed mid-run via the global kill switch."""


# Global kill flag - set by Flask /api/scrape/stop-force=true.
# BaseScraper.get() checks this before each request and raises ScraperKilled.
_kill_flag = {"set": False}

def request_kill():
    _kill_flag["set"] = True

def clear_kill():
    _kill_flag["set"] = False

def is_kill_requested() -> bool:
    return _kill_flag["set"]


def _check_kill():
    if _kill_flag["set"]:
        raise ScraperKilled("Kill requested")


class BaseScraper:
    county_name: str = ""
    base_url: str = ""

    def __init__(self):
        self.session = requests.Session()
        self._rotate_ua()

    def _rotate_ua(self):
        ua = random.choice(_USER_AGENTS)
        self.session.headers.clear()
        self.session.headers.update({**_BASE_HEADERS, "User-Agent": ua})

    def get(self, url: str, **kwargs) -> Optional[requests.Response]:
        _check_kill()
        _polite_wait(url)
        _check_kill()
        self._rotate_ua()
        for attempt in range(3):
            _check_kill()
            try:
                resp = self.session.get(url, timeout=30, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                log.warning(f"[{self.county_name}] Attempt {attempt+1} failed for {url}: {e}")
                if attempt < 2:
                    backoff = (2 ** attempt) + random.uniform(1, 3)
                    time.sleep(backoff)
        log.error(f"[{self.county_name}] All attempts failed for {url}")
        return None

    def soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    def pdf_text(self, url: str) -> str:
        """Download a PDF and return extracted text. Returns '' for scanned/image PDFs."""
        resp = self.get(url)
        if not resp:
            return ""
        try:
            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                pages = []
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        pages.append(t)
                return "\n".join(pages)
        except Exception as e:
            log.warning(f"[{self.county_name}] PDF parse failed for {url}: {e}")
            return ""

    def pdf_text_ocr(self, url: str) -> str:
        """Download a scanned/image PDF and OCR it with Tesseract."""
        resp = self.get(url)
        if not resp:
            return ""
        try:
            import pytesseract
            from pdf2image import convert_from_bytes
            import os

            # Tesseract path (winget install)
            for c in [
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            ]:
                if os.path.exists(c):
                    pytesseract.pytesseract.tesseract_cmd = c
                    break

            # Poppler path (winget install)
            poppler_path = (
                r"C:\Users\user\AppData\Local\Microsoft\WinGet\Packages"
                r"\oschwartz10612.Poppler_Microsoft.Winget.Source_8wekyb3d8bbwe"
                r"\poppler-25.07.0\Library\bin"
            )
            if not os.path.exists(poppler_path):
                poppler_path = None  # let pdf2image search PATH

            images = convert_from_bytes(resp.content, dpi=200, poppler_path=poppler_path)
            pages = []
            for img in images:
                text = pytesseract.image_to_string(img, config="--psm 6")
                if text.strip():
                    pages.append(text)
            return "\n".join(pages)
        except Exception as e:
            log.warning(f"[{self.county_name}] OCR failed for {url}: {e}")
            return ""

    def pdf_text_auto(self, url: str) -> str:
        """Try normal text extraction first; fall back to OCR if empty."""
        text = self.pdf_text(url)
        if text.strip():
            return text
        log.info(f"[{self.county_name}] No text in PDF, trying OCR: {url.split('/')[-1]}")
        return self.pdf_text_ocr(url)

    def pdf_tables(self, url: str) -> list[list[list]]:
        resp = self.get(url)
        if not resp:
            return []
        try:
            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                all_tables = []
                for page in pdf.pages:
                    tables = page.extract_tables()
                    if tables:
                        all_tables.extend(tables)
                return all_tables
        except Exception as e:
            log.warning(f"[{self.county_name}] PDF table extract failed for {url}: {e}")
            return []

    def scrape(self) -> list[PropertyRecord]:
        raise NotImplementedError
