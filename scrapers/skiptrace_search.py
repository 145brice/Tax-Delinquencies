"""
Free, no-API skip trace via DuckDuckGo.

Harvests phone numbers out of DuckDuckGo result snippets over plain HTTP -- no
browser, no JavaScript, no CAPTCHA. People-search sites (TruePeopleSearch,
FastPeopleSearch, Whitepages, Radaris, Spokeo, ClustrMaps, NationalPublicData,
UnMask, ...) leak full numbers into the snippets; we keep a number only when the
owner's name sits right beside it (or it's on the owner's people-search URL).

DuckDuckGo tolerates far more automated traffic than Google and never shows a
never-ending CAPTCHA, but it DOES rate-limit on heavy bursts (an "are you a human /
select the ducks" page, HTTP 202). When that happens the session stops with a clear
message -- wait ~30-60 min or switch networks (phone hotspot), then resume.

Public entry points:
    lookup(owner, street, city, state) -> dict
    DDGSession() / get_session()        -> reuse one HTTP session for many lookups
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import unquote, parse_qs, urlparse, urlencode

import requests
from bs4 import BeautifulSoup

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_PATH = DATA_DIR / "skiptrace_cache.json"
DDG_COOKIE_PATH = DATA_DIR / ".skiptrace_ddg_cookies.json"
# DuckDuckGo pacing (seconds). DDG has no CAPTCHA and tolerates more, but stay polite.
DDG_MIN_GAP = float(os.getenv("SKIPTRACE_DDG_MIN_GAP", "4"))
DDG_MAX_GAP = float(os.getenv("SKIPTRACE_DDG_MAX_GAP", "9"))


def _load_ddg_cookies(sess) -> None:
    try:
        data = json.loads(DDG_COOKIE_PATH.read_text(encoding="utf-8"))
        sess.cookies.update(requests.utils.cookiejar_from_dict(data))
    except Exception:
        pass


def _save_ddg_cookies(sess) -> None:
    try:
        DDG_COOKIE_PATH.write_text(
            json.dumps(requests.utils.dict_from_cookiejar(sess.cookies)), encoding="utf-8")
    except Exception:
        pass

_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]
_ACCEPT_LANG = ["en-US,en;q=0.9", "en-US,en;q=0.8", "en-GB,en-US;q=0.9,en;q=0.8"]
# Query phrasings -- {n}=name {c}=city {s}=state. Plain, human-style queries (no
# "truepeoplesearch"/"fastpeoplesearch" in the text -- those scraper-style queries
# raise the bot score). People-search sites rank for these anyway and _detect_source
# still tags them.
_QUERY_TEMPLATES = [
    '"{n}" {c} {s} phone number',
    '"{n}" {c} {s} phone',
    '{n} {c} {s} phone number',
    '"{n}" {c}, {s} cell phone',
]
_EMAIL_QUERY_TEMPLATES = [
    '"{n}" {c} {s} email address',
    '"{n}" {c}, {s} email',
    '{n} {c} {s} email contact',
]
# Reverse-address axis -- search the PROPERTY address, not the owner name. People-search
# sites keep address-indexed "current resident" pages that list the occupant + phone, so
# this catches owners whose name is garbled in the legal notice. {addr}=cleaned street.
# The name-beside-number gate in _extract_blocks still applies, so a *different* current
# resident's number is dropped -- we only keep numbers sitting next to the owner's name.
_ADDR_QUERY_TEMPLATES = [
    '"{addr}" {c} {s} phone',
    'who lives at {addr} {c} {s}',
    '"{addr}" {c} {s} current resident',
]
# site:-targeted fallback -- ONLY for leads that missed every other pass. Highest-yield
# query type (a people-search domain in the text), but the scraper-style query raises
# DDG's bot score, so it's reserved for misses and fires one domain per lead.
_SITE_DOMAINS = ["fastpeoplesearch.com", "truepeoplesearch.com",
                 "thatsthem.com", "cyberbackgroundchecks.com"]
_SITE_QUERY_TEMPLATES = [
    '"{n}" {c} {s} site:{d}',
    '{n} {c} site:{d}',
]
# Google (via Serper) prefers natural, UNQUOTED queries -- exact-match "quotes" return
# almost nothing on Google. These lean on the people-search sites Google indexes well.
_SERPER_PHONE_TEMPLATES = [
    '{n} {c} {s} phone number',
    '{n} {c} {s} address phone',
    '{n} {c} {s} truepeoplesearch',
]
_SERPER_EMAIL_TEMPLATES = [
    '{n} {c} {s} email address',
    '{n} {c} {s} contact email',
]

_PHONE_RE = re.compile(r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# --- input cleaning (mirrors app.py _skiptrace_search_* helpers) --------------

def clean_owner(owner: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", str(owner or ""))
    # First owner only: split on & / AND / ; / slash (legal notices list co-owners).
    text = re.split(r"\s*(?:&|\band\b|;|/)\s*", text, maxsplit=1, flags=re.I)[0]
    # Drop entity, vesting, relationship and suffix words that aren't part of the name.
    text = re.sub(
        r"\b(ETUX|ET UX|ET AL|AKA|ESTATE|HEIRS|OF|LLC|INC|TRUST|REVOCABLE|LIVING|"
        r"WIFE|HUSBAND|MARRIED|UNMARRIED|SINGLE|WIDOW|WIDOWER|SPOUSE|"
        r"TRUSTEE|TRUSTOR|JR|SR|II|III|IV)\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" ,.")
    if "," in text:
        last, rest = text.split(",", 1)
        text = f"{rest.strip()} {last.strip()}"
    text = re.sub(r"[^A-Za-z\s'-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_addr(street: str) -> str:
    t = re.sub(r"\{[^}]*\}", " ", str(street or ""))
    t = re.sub(r"\s+\.\d+\b", " ", t)          # drop ".20" acreage trailer
    return re.sub(r"\s+", " ", t).strip(" ,")


def _norm_phone(p: str) -> str:
    d = re.sub(r"\D", "", p)
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return f"({d[0:3]}) {d[3:6]}-{d[6:10]}" if len(d) == 10 else p.strip()


# --- cache --------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _cache_key(name: str, citystate: str) -> str:
    return f"{name}|{citystate}".lower()


class Blocked(Exception):
    """DuckDuckGo rate-limited us. Stop and back off rather than hammer."""


# --- phone extraction + ranking ----------------------------------------------

# Known people-search sources we expect to leak full numbers into snippets.
_SOURCES = {
    "truepeoplesearch": "TruePeopleSearch", "fastpeoplesearch": "FastPeopleSearch",
    "nationalpublicdata": "NationalPublicData", "unmask": "UnMask",
    "cyberbackgroundchecks": "CyberBackgroundChecks", "thatsthem": "ThatsThem",
    "spokeo": "Spokeo", "beenverified": "BeenVerified", "whitepages": "Whitepages",
    "radaris": "Radaris", "nuwber": "Nuwber", "clustrmaps": "ClustrMaps",
    "usphonebook": "USPhonebook", "peoplefinders": "PeopleFinders",
}
# Context that means the number belongs to a BUSINESS / Maps card, not the owner.
_BIZ_NEG = (" md", "m.d.", "dr.", "nephrolog", "clinic", "hospital", "reviews", "directions",
            "website", "rating", "hours", "llc", "inc.", "realty", "attorney", "law firm")
_OWNER_BOOST = ("owns the phone", "phone number", "cell phone", "lives in", "lives at", "associated")


def _detect_source(ctx: str) -> str:
    low = ctx.lower()
    for key, label in _SOURCES.items():
        if key in low:
            return label
    return ""


def _name_tokens(name: str) -> tuple[str, str]:
    toks = [t for t in re.sub(r"[^a-z ]", " ", name.lower()).split() if len(t) > 1]
    return (toks[0] if toks else ""), (toks[-1] if toks else "")


def _query_name(name: str) -> str:
    """First + last only for the search query. People-search sites index by first/last,
    so a long 'LESLY MARITZA ZELAYA BONILLA' searched whole returns little -- 'LESLY
    BONILLA' returns the right pages. The name-adjacency gate still guards precision."""
    parts = name.split()
    return f"{parts[0]} {parts[-1]}" if len(parts) > 2 else name


def _extract_blocks(blocks: list[dict], name: str, street: str, state: str) -> tuple[list[dict], list[str]]:
    """STRICT: only harvest numbers from result blocks where the owner's name
    actually appears (in the snippet text or, even better, the result URL).
    A number with no owner name beside it is dropped -- this kills 'Dr. Kevin Cox, MD'
    style junk that just happens to share a surname, and same-surname relatives."""
    first, last = _name_tokens(name)
    phones: dict[str, dict] = {}
    emails: set[str] = set()
    for blk in blocks:
        text = blk.get("text") or ""
        url = (blk.get("url") or "").lower()
        low = text.lower()
        name_in_text = bool(first and last and first in low and last in low)
        name_in_url = bool(first and last and first in url and last in url)
        if not (name_in_text or name_in_url):
            continue                                   # not clearly this person -> skip block
        if any(b in low for b in _BIZ_NEG):
            continue                                   # business / Maps card -> skip whole block
        source = _detect_source(url) or _detect_source(low)
        for m in _PHONE_RE.finditer(text):
            norm = _norm_phone(m.group(0))
            if not re.match(r"\(\d{3}\) \d{3}-\d{4}", norm):
                continue
            ctx = text[max(0, m.start() - 140): m.end() + 140].strip()
            clow = ctx.lower()
            # CORE RULE: the owner's name must be RIGHT BESIDE this number (first AND
            # last in its local context), OR the result URL is this person's page (with
            # at least the first name beside the number, to dodge same-surname relatives).
            name_beside = bool(first and last and first in clow and last in clow)
            if not (name_beside or (name_in_url and first and first in clow)):
                continue
            score = 0
            if name_in_url:
                score += 4
            if name_beside:
                score += 4
            if source:
                score += 2
            if street:
                snum = street.split()[0]
                if snum.isdigit() and snum in ctx:
                    score += 2
            if any(b in clow for b in _OWNER_BOOST):
                score += 1
            cur = phones.get(norm)
            if cur is None or score > cur["score"]:
                phones[norm] = {"phone": norm, "score": score, "source": source, "context": ctx}
        for e in _EMAIL_RE.findall(text):
            el = e.lower()
            if not el.endswith((".png", ".jpg", ".svg", ".webp", ".gif")) and \
               not any(b in el for b in ("google", "gstatic", "schema.org", "example")):
                emails.add(e)
    ranked = sorted(phones.values(), key=lambda d: (-d["score"], d["phone"]))
    return ranked, sorted(emails)


def _hp(a: float, b: float) -> None:
    time.sleep(random.uniform(a, b))


# --- DuckDuckGo backend -------------------------------------------------------

_DDG_ENDPOINTS = ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/")


def _ddg_blocks(html: str) -> list[dict]:
    """Parse DuckDuckGo HTML results into [{url, text}] blocks for _extract_blocks."""
    soup = BeautifulSoup(html, "lxml")
    blocks: list[dict] = []
    results = soup.select("div.result")
    if results:                                            # html.duckduckgo.com layout
        for res in results:
            a = res.select_one("a.result__a")
            href = a.get("href", "") if a else ""
            real = ""
            if "uddg=" in href:                            # decode DDG's redirect to the real URL
                try:
                    real = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
                except Exception:
                    real = ""
            text = res.get_text(" ", strip=True)
            if text:
                blocks.append({"url": real or href, "text": text})
    else:                                                  # lite.duckduckgo.com fallback
        for a in soup.select("a.result-link"):
            row = a.find_parent("tr")
            snip = ""
            if row is not None and row.find_next_sibling("tr"):
                snip = row.find_next_sibling("tr").get_text(" ", strip=True)
            text = (a.get_text(" ", strip=True) + " " + snip).strip()
            if text:
                blocks.append({"url": a.get("href", ""), "text": text})
    return blocks


def _session_headers(ua: str) -> dict:
    """A coherent, realistic browser header set for the chosen UA -- not a bare request.
    Matching Sec-Fetch + client-hint headers make each request look like a real browser."""
    h = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice(_ACCEPT_LANG),
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Referer": "https://duckduckgo.com/",
    }
    if random.random() < 0.5:
        h["DNT"] = "1"
    if "Chrome/" in ua:                        # Chromium client hints
        m = re.search(r"Chrome/(\d+)", ua)
        v = m.group(1) if m else "124"
        h["sec-ch-ua"] = f'"Chromium";v="{v}", "Google Chrome";v="{v}", "Not?A_Brand";v="99"'
        h["sec-ch-ua-mobile"] = "?0"
        h["sec-ch-ua-platform"] = '"Windows"' if "Windows" in ua else '"macOS"'
    return h


class DDGSession:
    """Reuse one HTTP session for many lookups. No browser, no CAPTCHA.
    Layered humanization: realistic per-session headers + cookies, homepage warm-up,
    jittered gaps with occasional long pauses, and idle cycles every few leads."""

    def __init__(self, **_ignored):
        self._sess = None
        self._n = 0
        self._empty_streak = 0
        self._challenged = False
        self._since_idle = 0
        self._next_idle = random.randint(5, 10)   # take a human "break" every 5-10 leads

    def __enter__(self):
        self._sess = requests.Session()
        self._sess.headers.update(_session_headers(random.choice(_UAS)))
        _load_ddg_cookies(self._sess)          # reuse cookies => returning-visitor signal
        try:                                   # warm up like a real visit before searching
            self._sess.get("https://duckduckgo.com/", timeout=20)
            _hp(0.8, 2.2)
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        if self._sess:
            _save_ddg_cookies(self._sess)
            self._sess.close()

    def _search(self, query: str) -> list[dict]:
        # Always lead with the html endpoint (richest results, best parsing); lite is
        # only a fallback. Don't degrade coverage for the sake of endpoint variety.
        data = {"q": query}
        if random.random() < 0.5:                  # sometimes include the US-English region
            data["kl"] = "us-en"
        for ep in _DDG_ENDPOINTS:
            try:
                r = self._sess.post(ep, data=data, timeout=25)
            except Exception:
                continue
            low = r.text.lower()
            # DDG anti-bot challenge ("select all squares containing a duck"). Status is
            # usually 202. Flag it so lookup() stops with a clear message.
            if r.status_code == 202 or "anomaly" in low or "made by a human" in low \
                    or "select all squares" in low:
                self._challenged = True
                return []
            if r.status_code == 200 and len(r.text) > 800:
                blocks = _ddg_blocks(r.text)
                if blocks:
                    return blocks
        return []

    @staticmethod
    def _merge_blocks(blocks, name, addr, state, phones: dict, emails: set) -> None:
        """Extract phones/emails from one pass's blocks and fold them into the running
        accumulators, keeping the highest score per number across passes."""
        ranked, em = _extract_blocks(blocks, name, addr, state)
        for d in ranked:
            cur = phones.get(d["phone"])
            if cur is None or d["score"] > cur["score"]:
                phones[d["phone"]] = d
        emails.update(em)

    def lookup(self, owner: str, street: str, city: str, state: str,
               *, use_cache: bool = True) -> dict:
        name = clean_owner(owner)
        addr = clean_addr(street)
        citystate = f"{city}-{state}".strip("-")
        key = _cache_key(name, citystate)

        cache = _load_cache()
        if use_cache and key in cache:
            out = dict(cache[key]); out["cached"] = True
            return out
        if not name:
            return {"phones": [], "emails": [], "name": name, "cached": False,
                    "error": "no usable owner name"}

        if self._n:
            # Human-style gap: usually the base range, occasionally a longer "reading"
            # pause -- a flat, identical interval every time is itself a bot tell.
            gap = random.uniform(DDG_MIN_GAP, DDG_MAX_GAP)
            if random.random() < 0.18:
                gap += random.uniform(DDG_MAX_GAP, DDG_MAX_GAP * 2.5)
            time.sleep(gap)
        self._n += 1

        # Idle cycle: every few leads, take a longer human break and sometimes wander
        # back to the homepage -- people don't search at a metronome pace forever.
        self._since_idle += 1
        if self._since_idle >= self._next_idle:
            time.sleep(random.uniform(15, 40))
            if random.random() < 0.5:
                try:
                    self._sess.get("https://duckduckgo.com/", timeout=20)
                    _hp(1.0, 3.0)
                except Exception:
                    pass
            self._since_idle = 0
            self._next_idle = random.randint(5, 10)

        # Each pass below is one independent DDG request, gated to fire only when the
        # earlier (cheaper, lower-block-risk) passes came up short. phones/emails
        # accumulate across passes; the full inter-pass gap keeps us off the rate-limit.
        phones: dict[str, dict] = {}
        emails: set[str] = set()
        queries: list[str] = []

        def _gap() -> None:
            time.sleep(random.uniform(DDG_MIN_GAP, DDG_MAX_GAP))

        # Pass 1 -- name + phone (the primary axis)
        query = random.choice(_QUERY_TEMPLATES).format(n=_query_name(name), c=city, s=state)
        queries.append(query)
        blocks = self._search(query)
        if self._challenged:
            raise Blocked("DuckDuckGo is rate-limiting this IP (anti-bot 'duck' challenge). "
                          "Pause ~30-60 min or switch networks (phone hotspot), then resume. "
                          "Slower pace avoids it -- this only trips on heavy bursts.")
        # Repeated total emptiness usually means DDG is rate-limiting us -> back off / stop.
        self._empty_streak = self._empty_streak + 1 if not blocks else 0
        if self._empty_streak >= 6:
            raise Blocked("DuckDuckGo returned nothing 6x in a row -- likely rate-limited; "
                          "pause a bit or slow the pace, then resume.")
        self._merge_blocks(blocks, name, addr, state, phones, emails)

        # Pass 2 -- reverse-address. Fires when the name search found no phone: the
        # property address is a second axis that catches garbled/odd owner names.
        if not phones and addr and not self._challenged:
            _gap()
            addr_query = random.choice(_ADDR_QUERY_TEMPLATES).format(addr=addr, c=city, s=state)
            queries.append(addr_query)
            self._merge_blocks(self._search(addr_query), name, addr, state, phones, emails)

        # Pass 3 -- dedicated email query (phone queries rarely surface email in snippets).
        if not emails and not self._challenged:
            _gap()
            email_query = random.choice(_EMAIL_QUERY_TEMPLATES).format(
                n=_query_name(name), c=city, s=state)
            queries.append(email_query)
            self._merge_blocks(self._search(email_query), name, addr, state, phones, emails)

        # Pass 4 -- site:-targeted fallback. ONLY when every prior pass missed entirely.
        # Highest-yield, but the scraper-style query raises bot score -> last + one domain.
        if not phones and not emails and not self._challenged:
            _gap()
            domain = random.choice(_SITE_DOMAINS)
            site_query = random.choice(_SITE_QUERY_TEMPLATES).format(
                n=_query_name(name), c=city, s=state, d=domain)
            queries.append(site_query)
            self._merge_blocks(self._search(site_query), name, addr, state, phones, emails)

        ranked = sorted(phones.values(), key=lambda d: (-d["score"], d["phone"]))
        result = {
            "name": name,
            "query": " | ".join(queries),
            "phones": [r["phone"] for r in ranked],
            "phone_details": ranked[:6],
            "emails": sorted(emails)[:4],
            "cached": False,
        }
        # Cache HITS only. Never cache a miss -- a blank may just be a throttle/coverage
        # gap that day, so it should re-query next run rather than poison the lead forever.
        if result["phones"] or result["emails"]:
            cache[key] = {k: result[k] for k in ("name", "query", "phones", "phone_details", "emails")}
            _save_cache(cache)
        return result


# --- Google backend: humanized HTTP first, headed-browser fallback ------------
# Google no longer serves result snippets to a plain HTTP client (JS shell), so the
# HTTP pass rarely yields anything -- it's kept as a cheap first try, and the headed
# browser (which runs JS) is what actually extracts snippets. CAPTCHA-prone: use a
# fresh/rested IP. Mirrors the "urllib first, browser fallback" idea from Recently-Solds.
GOOGLE_MIN_GAP = float(os.getenv("SKIPTRACE_GOOGLE_MIN_GAP", "6"))
GOOGLE_MAX_GAP = float(os.getenv("SKIPTRACE_GOOGLE_MAX_GAP", "12"))
GOOGLE_COOKIE_PATH = DATA_DIR / ".skiptrace_google_cookies.json"
PROFILE_DIR = DATA_DIR / ".skiptrace_profile"
IDENTITY_PATH = PROFILE_DIR / "identity.json"
_VIEWPORTS = [(1366, 768), (1440, 900), (1536, 864), (1280, 800), (1600, 900), (1920, 1080)]
_LOCALES = ["en-US", "en-GB", "en-CA"]
_TZS = ["America/Chicago", "America/New_York", "America/Denver", "America/Los_Angeles", "America/Phoenix"]
_GOOGLE_BLOCK = ("/sorry/", "unusual traffic", "detected unusual traffic",
                 "our systems have detected", "before you continue to google")
_GOOGLE_SNIPPET_PATTERNS = [
    r'<div class="VwiC3b[^"]*"[^>]*>(.*?)</div>',
    r'<span class="aCOpRe[^"]*"[^>]*>(.*?)</span>',
]


def _get_identity() -> dict:
    """One stable, realistic browser identity, generated once and reused (persisted
    with the profile). Rotating it every run is itself a bot tell."""
    try:
        return json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
    except Exception:
        vw, vh = random.choice(_VIEWPORTS)
        ident = {"ua": random.choice(_UAS), "vw": vw, "vh": vh,
                 "locale": random.choice(_LOCALES), "tz": random.choice(_TZS),
                 "accept_lang": random.choice(_ACCEPT_LANG)}
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        IDENTITY_PATH.write_text(json.dumps(ident, indent=2), encoding="utf-8")
        return ident


def _wiggle(page) -> None:
    for _ in range(random.randint(2, 5)):
        page.mouse.move(random.randint(50, 1200), random.randint(80, 700), steps=random.randint(3, 12))
        _hp(0.1, 0.4)


def _scroll(page) -> None:
    for _ in range(random.randint(1, 4)):
        page.mouse.wheel(0, random.randint(200, 650))
        _hp(0.3, 1.0)


def _dismiss_consent(page) -> None:
    for sel in ("button:has-text('Accept all')", "button:has-text('I agree')",
                "#L2AGLb", "button:has-text('Reject all')"):
        try:
            loc = page.locator(sel)
            if loc.count():
                loc.first.click(timeout=2500); _hp(0.4, 1.0); return
        except Exception:
            pass


_BLOCKS_JS = """() => {
    const out = []; const seen = new Set();
    const push = (url, text) => { if (!text) return;
        const k = (url||'') + '|' + text.slice(0,60); if (seen.has(k)) return; seen.add(k);
        out.push({ url: url || '', text }); };
    let nodes = document.querySelectorAll('div.MjjYud, div.g, div.tF2Cxc, div.N54PNb');
    if (nodes.length >= 2) { nodes.forEach(el => { const a = el.querySelector('a[href]');
        push(a ? a.href : '', el.innerText || ''); });
    } else { document.querySelectorAll('h3').forEach(h3 => { const a = h3.closest('a'); let el = h3;
        for (let i=0;i<5 && el.parentElement;i++){ el = el.parentElement; if (el.innerText && el.innerText.length>160) break; }
        push(a ? a.href : '', (el ? el.innerText : h3.innerText) || ''); }); }
    return out;
}"""


def _google_blocks(html: str) -> list[dict]:
    """Parse static Google HTML into {url,text} blocks; fall back to snippet regexes."""
    soup = BeautifulSoup(html, "lxml")
    blocks, seen = [], set()
    for node in soup.select("div.MjjYud, div.g, div.tF2Cxc, div.N54PNb"):
        a = node.find("a", href=True)
        href = a["href"] if a else ""
        if href.startswith("/url?"):
            href = parse_qs(urlparse(href).query).get("q", [""])[0]
        text = node.get_text(" ", strip=True)
        key = href + "|" + text[:60]
        if text and key not in seen:
            seen.add(key); blocks.append({"url": href, "text": text})
    if not blocks:                                   # JS-shell: salvage snippet text via regex
        chunks = []
        for pat in _GOOGLE_SNIPPET_PATTERNS:
            for m in re.findall(pat, html, flags=re.I | re.S):
                t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m)).strip()
                if t:
                    chunks.append(t)
        if chunks:
            blocks.append({"url": "", "text": " ".join(chunks)})
    return blocks


class GoogleSession:
    """Google engine: humanized HTTP first, headed-browser fallback when HTTP is empty
    or challenged. Higher coverage than DDG but CAPTCHA-prone -- needs a rested IP."""

    def __init__(self, headless: bool = False, challenge_timeout: float = 150.0,
                 max_challenges: int = 3, **_ignored):
        self.headless = headless
        self.challenge_timeout = challenge_timeout
        self.max_challenges = max_challenges
        self._challenge_count = 0
        self._sess = None
        self._pw = self._ctx = self._page = None
        self._n = 0
        self._since_idle = 0
        self._next_idle = random.randint(5, 10)

    def __enter__(self):
        self._sess = requests.Session()
        self._sess.headers.update(_session_headers(random.choice(_UAS)))
        self._sess.headers["Referer"] = "https://www.google.com/"
        try:
            data = json.loads(GOOGLE_COOKIE_PATH.read_text(encoding="utf-8"))
            self._sess.cookies.update(requests.utils.cookiejar_from_dict(data))
        except Exception:
            pass
        try:
            self._sess.get("https://www.google.com/", timeout=20); _hp(0.8, 2.0)
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        try:
            GOOGLE_COOKIE_PATH.write_text(
                json.dumps(requests.utils.dict_from_cookiejar(self._sess.cookies)), encoding="utf-8")
        except Exception:
            pass
        if self._sess:
            self._sess.close()
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _http_search(self, query: str):
        """Returns blocks, or None if Google challenged the HTTP request."""
        url = "https://www.google.com/search?" + urlencode({"q": query, "hl": "en", "num": "10", "pws": "0"})
        try:
            r = self._sess.get(url, timeout=25)
        except Exception:
            return []
        low = r.text.lower()
        if "/sorry/" in r.url.lower() or any(m in low for m in _GOOGLE_BLOCK):
            return None
        return _google_blocks(r.text)

    def _ensure_browser(self):
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        ident = _get_identity()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=ident["ua"], locale=ident["locale"], timezone_id=ident["tz"],
            viewport={"width": ident["vw"], "height": ident["vh"]},
            extra_http_headers={"Accept-Language": ident["accept_lang"]},
        )
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});window.chrome={runtime:{}};")
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=60000)
        _hp(1.0, 2.5); _dismiss_consent(self._page)
        return self._page

    def _is_challenge(self) -> bool:
        if "/sorry/" in self._page.url.lower():
            return True
        try:
            return any(m in self._page.inner_text("body").lower() for m in _GOOGLE_BLOCK)
        except Exception:
            return False

    def _wait_for_human(self) -> None:
        if self.headless:
            raise Blocked("Google CAPTCHA and running headless.")
        deadline = time.time() + self.challenge_timeout
        print(f"\n  [!] Google CAPTCHA -> solve it in the window (up to {int(self.challenge_timeout)}s)...", flush=True)
        while time.time() < deadline:
            time.sleep(3)
            if not self._is_challenge():
                print("  [+] cleared, resuming.\n", flush=True); _hp(1.0, 2.0); return
        raise Blocked("Google CAPTCHA not solved within timeout.")

    def _browser_search(self, query: str) -> list[dict]:
        page = self._ensure_browser()
        box = page.locator("textarea[name=q], input[name=q]").first
        box.click(); _hp(0.3, 0.9)
        try:
            box.fill("")
        except Exception:
            pass
        for ch in query:
            box.type(ch, delay=random.randint(35, 160))
        _hp(0.4, 1.1); box.press("Enter")
        page.wait_for_load_state("domcontentloaded"); _hp(1.5, 3.0); _dismiss_consent(page)
        if self._is_challenge():
            self._challenge_count += 1
            if self._challenge_count >= self.max_challenges:
                raise Blocked(f"Google challenged {self._challenge_count}x -- IP rate-limited; "
                              "wait or switch networks, then resume.")
            self._wait_for_human()
            if "/search" not in page.url.lower():
                page.goto("https://www.google.com/", wait_until="domcontentloaded"); _hp(1.0, 2.0)
            box = page.locator("textarea[name=q], input[name=q]").first
            box.click(); box.fill(""); box.type(query, delay=random.randint(35, 120)); box.press("Enter")
            page.wait_for_load_state("domcontentloaded"); _hp(1.5, 3.0)
            if self._is_challenge():
                raise Blocked("Still challenged after solving -- IP rate-limited; stop and switch IP.")
        _wiggle(page); _scroll(page)
        try:
            return page.evaluate(_BLOCKS_JS) or []
        except Exception:
            return []

    def _search(self, query: str) -> list[dict]:
        blocks = self._http_search(query)
        if blocks:                                   # HTTP gave us something usable
            return blocks
        # HTTP empty (JS shell) or challenged -> use the browser, which runs JS
        return self._browser_search(query)

    def lookup(self, owner: str, street: str, city: str, state: str,
               *, use_cache: bool = True) -> dict:
        name = clean_owner(owner); addr = clean_addr(street)
        key = _cache_key(name, f"{city}-{state}".strip("-"))
        cache = _load_cache()
        if use_cache and key in cache:
            out = dict(cache[key]); out["cached"] = True
            return out
        if not name:
            return {"phones": [], "emails": [], "name": name, "cached": False, "error": "no usable owner name"}

        if self._n:
            _hp(GOOGLE_MIN_GAP, GOOGLE_MAX_GAP)
        self._n += 1
        self._since_idle += 1
        if self._since_idle >= self._next_idle:
            time.sleep(random.uniform(20, 50)); self._since_idle = 0; self._next_idle = random.randint(5, 10)

        # try up to 2 phrasings (keeps Google volume low while improving hit rate)
        ranked, emails, used_q = [], [], ""
        for tmpl in random.sample(_QUERY_TEMPLATES, k=min(2, len(_QUERY_TEMPLATES))):
            q = tmpl.format(n=_query_name(name), c=city, s=state)
            used_q = used_q or q
            blocks = self._search(q)
            ranked, emails = _extract_blocks(blocks, name, addr, state)
            if ranked:
                used_q = q
                break

        # Dedicated email pass when the phone search found nothing
        if not emails and not self._is_challenge():
            _hp(GOOGLE_MIN_GAP, GOOGLE_MAX_GAP)
            eq = random.choice(_EMAIL_QUERY_TEMPLATES).format(n=_query_name(name), c=city, s=state)
            email_blocks = self._search(eq)
            if email_blocks:
                _, emails = _extract_blocks(email_blocks, name, addr, state)
                if emails:
                    used_q = used_q + f" | {eq}"

        result = {"name": name, "query": used_q, "phones": [r["phone"] for r in ranked],
                  "phone_details": ranked[:6], "emails": emails[:4], "cached": False}
        if result["phones"] or result["emails"]:        # cache hits only (don't poison misses)
            cache[key] = {k: result[k] for k in ("name", "query", "phones", "phone_details", "emails")}
            _save_cache(cache)
        return result


class ComboSession:
    """DDG first (free, no CAPTCHA); Google headed-browser fallback only for the leads
    DDG misses. Best coverage; Google touches only the misses, keeping its volume low."""

    def __init__(self, headless: bool = False, **_ignored):
        self.headless = headless
        self._ddg = None
        self._g = None

    def __enter__(self):
        self._ddg = DDGSession().__enter__()
        return self

    def __exit__(self, *exc):
        try:
            if self._ddg:
                self._ddg.__exit__(*exc)
        finally:
            if self._g:
                self._g.__exit__(*exc)

    def _google(self):
        if self._g is None:
            self._g = GoogleSession(headless=self.headless).__enter__()
        return self._g

    def lookup(self, owner: str, street: str, city: str, state: str, *, use_cache: bool = True) -> dict:
        name = clean_owner(owner)
        key = _cache_key(name, f"{city}-{state}".strip("-"))
        cache = _load_cache()
        if use_cache and key in cache:
            out = dict(cache[key]); out["cached"] = True
            return out
        r = self._ddg.lookup(owner, street, city, state, use_cache=False)
        if r.get("phones"):
            r["engine"] = "ddg"
            return r
        g = self._google().lookup(owner, street, city, state, use_cache=False)
        g["engine"] = "google" if g.get("phones") else "ddg"
        return g if g.get("phones") else r


# --- Serper.dev backend: Google results via API (no scraping, no IP rate-limit) ---
# Serper proxies the search on THEIR infrastructure and returns JSON, so your IP never
# trips a CAPTCHA/rate-limit. Costs ~$1 per 1,000 queries. Same multi-pass + name-beside-
# number extraction as DDG -- only the transport differs (API call vs HTML scrape).
SERPER_ENDPOINT = "https://google.serper.dev/search"
# No DDG-style burst limit on YOUR IP, so gaps are tiny -- just polite to Serper's plan.
SERPER_MIN_GAP = float(os.getenv("SKIPTRACE_SERPER_MIN_GAP", "0.4"))
SERPER_MAX_GAP = float(os.getenv("SKIPTRACE_SERPER_MAX_GAP", "1.0"))


def _serper_key() -> str:
    """Read the key at runtime (with a dotenv fallback for direct CLI use), not at import
    -- the env may not be loaded yet when this module is first imported."""
    key = os.getenv("SERPER_API_KEY", "")
    if not key:
        try:
            from dotenv import load_dotenv
            load_dotenv()
            key = os.getenv("SERPER_API_KEY", "")
        except Exception:
            pass
    return key


def _serper_blocks(data: dict) -> list[dict]:
    """Turn a Serper JSON response into {url,text} blocks for _extract_blocks."""
    blocks: list[dict] = []
    for item in (data.get("organic") or []):
        text = " ".join(p for p in (item.get("title"), item.get("snippet")) if p)
        for sl in (item.get("sitelinks") or []):
            if sl.get("title"):
                text += " " + sl["title"]
        if text:
            blocks.append({"url": item.get("link", ""), "text": text})
    kg = data.get("knowledgeGraph") or {}      # can carry a phone directly
    if kg:
        bits = [kg.get("title") or "", kg.get("description") or ""]
        bits += [f"{k}: {v}" for k, v in (kg.get("attributes") or {}).items()]
        t = " ".join(b for b in bits if b)
        if t:
            blocks.append({"url": kg.get("website", ""), "text": t})
    return blocks


class SerperSession:
    """Google results via the Serper.dev API -- no HTML scraping, no CAPTCHA, and the
    requests go through Serper, so your IP is never rate-limited. Same four-pass flow and
    name-beside-number precision gate as the DDG backend."""

    def __init__(self, **_ignored):
        self._key = _serper_key()
        if not self._key:
            raise Blocked("SERPER_API_KEY is not set -- add it to your .env to use the Serper engine.")
        self._sess = None
        self._n = 0

    def __enter__(self):
        self._sess = requests.Session()
        self._sess.headers.update({"X-API-KEY": self._key, "Content-Type": "application/json"})
        return self

    def __exit__(self, *exc):
        if self._sess:
            self._sess.close()

    @staticmethod
    def _merge_blocks(blocks, name, addr, state, phones: dict, emails: set) -> None:
        ranked, em = _extract_blocks(blocks, name, addr, state)
        for d in ranked:
            cur = phones.get(d["phone"])
            if cur is None or d["score"] > cur["score"]:
                phones[d["phone"]] = d
        emails.update(em)

    def _search(self, query: str) -> list[dict]:
        body = json.dumps({"q": query, "gl": "us", "hl": "en", "num": 20})
        try:
            r = self._sess.post(SERPER_ENDPOINT, data=body, timeout=30)
        except Exception:
            return []
        if r.status_code in (401, 403):
            raise Blocked("Serper rejected the API key (HTTP %d) -- check SERPER_API_KEY in .env." % r.status_code)
        if r.status_code == 429:
            raise Blocked("Serper credit/rate limit reached (HTTP 429) -- top up credits or slow down.")
        if r.status_code != 200:
            return []
        try:
            return _serper_blocks(r.json())
        except Exception:
            return []

    def lookup(self, owner: str, street: str, city: str, state: str,
               *, use_cache: bool = True) -> dict:
        name = clean_owner(owner)
        addr = clean_addr(street)
        key = _cache_key(name, f"{city}-{state}".strip("-"))
        cache = _load_cache()
        if use_cache and key in cache:
            out = dict(cache[key]); out["cached"] = True
            return out
        if not name:
            return {"phones": [], "emails": [], "name": name, "cached": False,
                    "error": "no usable owner name"}

        if self._n:
            _hp(SERPER_MIN_GAP, SERPER_MAX_GAP)
        self._n += 1

        phones: dict[str, dict] = {}
        emails: set[str] = set()
        queries: list[str] = []

        # Pass 1 -- name + phone (UNQUOTED -- Google returns ~nothing for "exact match")
        q = random.choice(_SERPER_PHONE_TEMPLATES).format(n=_query_name(name), c=city, s=state)
        queries.append(q)
        self._merge_blocks(self._search(q), name, addr, state, phones, emails)
        # Pass 2 -- reverse-address (only if no phone yet)
        if not phones and addr:
            _hp(SERPER_MIN_GAP, SERPER_MAX_GAP)
            q = random.choice(_ADDR_QUERY_TEMPLATES).format(addr=addr, c=city, s=state)
            queries.append(q)
            self._merge_blocks(self._search(q), name, addr, state, phones, emails)
        # Pass 3 -- dedicated email query
        if not emails:
            _hp(SERPER_MIN_GAP, SERPER_MAX_GAP)
            q = random.choice(_SERPER_EMAIL_TEMPLATES).format(n=_query_name(name), c=city, s=state)
            queries.append(q)
            self._merge_blocks(self._search(q), name, addr, state, phones, emails)
        # Pass 4 -- site: fallback (only on total miss)
        if not phones and not emails:
            _hp(SERPER_MIN_GAP, SERPER_MAX_GAP)
            domain = random.choice(_SITE_DOMAINS)
            q = random.choice(_SITE_QUERY_TEMPLATES).format(n=_query_name(name), c=city, s=state, d=domain)
            queries.append(q)
            self._merge_blocks(self._search(q), name, addr, state, phones, emails)

        ranked = sorted(phones.values(), key=lambda d: (-d["score"], d["phone"]))
        result = {
            "name": name, "engine": "serper", "query": " | ".join(queries),
            "phones": [r["phone"] for r in ranked], "phone_details": ranked[:6],
            "emails": sorted(emails)[:4], "cached": False,
        }
        if result["phones"] or result["emails"]:
            cache[key] = {k: result[k] for k in ("name", "query", "phones", "phone_details", "emails")}
            _save_cache(cache)
        return result


def get_session(engine: str = "ddg", **kw):
    """Factory: 'ddg' (no browser, default), 'google' (HTTP+browser),
    'combo' (DDG first, Google fallback), or 'serper' (Google via API, no IP blocks)."""
    if engine == "google":
        return GoogleSession(**kw)
    if engine == "combo":
        return ComboSession(**kw)
    if engine == "serper":
        return SerperSession(**kw)
    return DDGSession(**kw)


def lookup(owner: str, street: str, city: str, state: str,
           *, engine: str = "ddg", headless: bool = False, use_cache: bool = True) -> dict:
    """Convenience: one lead. Defaults to the no-browser DuckDuckGo backend."""
    with get_session(engine, headless=headless) as s:
        return s.lookup(owner, street, city, state, use_cache=use_cache)


if __name__ == "__main__":
    import sys
    owner = sys.argv[1] if len(sys.argv) > 1 else "FALAHI, MOHAMMAD"
    street = sys.argv[2] if len(sys.argv) > 2 else "2705 B DICKERSON PIKE"
    city = sys.argv[3] if len(sys.argv) > 3 else "Nashville"
    state = sys.argv[4] if len(sys.argv) > 4 else "TN"
    print(json.dumps(lookup(owner, street, city, state, use_cache=False), indent=2))
