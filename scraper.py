import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta
from threading import Event
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

# ─── County configuration ────────────────────────────────────────────────────
# Data sources used (all free, no subscription required):
#   1. HUD Homestore  – government-owned REO foreclosures (hudhomestore.gov)
#   2. Fannie Mae HomePath – Fannie Mae REO properties (homepath.com)
#   3. Davidson County Register of Deeds – lis pendens / TN foreclosure notices
#   4. Wilson County public records – TN tax & foreclosure notices

COUNTY_CONFIG = {
    "miami-dade":  {"state": "FL", "county": "Miami-Dade"},
    "harris":      {"state": "TX", "county": "Harris"},
    "fulton":      {"state": "GA", "county": "Fulton"},
    "cook":        {"state": "IL", "county": "Cook"},
    "maricopa":    {"state": "AZ", "county": "Maricopa"},
    "los-angeles": {"state": "CA", "county": "Los Angeles"},
    "davidson":    {"state": "TN", "county": "Davidson"},
    "wilson-tn":   {"state": "TN", "county": "Wilson"},
}

STATUS_MAP = {
    "foreclosure":     "Auction",
    "pre_foreclosure": "Pre-foreclosure",
    "pre-foreclosure": "Pre-foreclosure",
    "tax_lien":        "Tax Lien",
    "tax lien":        "Tax Lien",
    "lis pendens":     "Pre-foreclosure",
    "notice of default": "Pre-foreclosure",
    "auction":         "Auction",
    "reo":             "Auction",
    "hud":             "Auction",
}


def dedup_key(address, date):
    return hashlib.md5(f"{address.lower().strip()}-{date}".encode()).hexdigest()


def normalize_status(raw):
    raw = raw.lower().strip()
    for k, v in STATUS_MAP.items():
        if k in raw:
            return v
    return raw.title()


def clean_price(raw):
    digits = re.sub(r'[^\d]', '', str(raw))
    return int(digits) if digits else 0


def make_listing(address, status, bid, date_str, county, link, source):
    key = dedup_key(address, date_str)
    return {
        "id":      key[:8],
        "address": address,
        "status":  status,
        "price":   5,           # $5/lead — buyer price, not property value
        "bid":     bid,
        "date":    date_str,
        "county":  county,
        "link":    link,
        "source":  source,
    }


# ─── Source 1: HUD Homestore ──────────────────────────────────────────────────
async def scrape_hud(page, state_code, county_name, lookback_days, seen):
    """
    Scrape HUD Homestore for government REO (foreclosed FHA-insured) properties.
    Site was rebuilt with Yardi in late 2023 — new SPA at hudhomestore.gov/searchresult.
    Intercepts the Yardi JSON API call; falls back to HTML card parsing.
    """
    results = []
    tag = f"HUD/{state_code}/{county_name}"
    intercepted = []

    async def capture(response):
        ct = response.headers.get("content-type", "")
        if "json" in ct and any(k in response.url for k in ("search", "propert", "listing", "result")):
            try:
                data = await response.json()
                intercepted.append(data)
            except Exception:
                pass

    page.on("response", capture)
    try:
        print(f"[{tag}] Fetching HUD Homestore (Yardi SPA)...")
        await page.goto(
            f"https://www.hudhomestore.gov/searchresult?citystate={state_code}",
            wait_until="networkidle",
            timeout=45000,
        )
        await page.wait_for_timeout(4000)
    except Exception as e:
        print(f"[{tag}] Navigation error: {e}")
    finally:
        page.remove_listener("response", capture)

    # Parse intercepted JSON from Yardi backend
    for data in intercepted:
        items = (
            data.get("properties") or data.get("listings") or
            data.get("results") or data.get("data") or
            (data if isinstance(data, list) else [])
        )
        for item in (items or [])[:200]:
            try:
                address = ", ".join(filter(None, [
                    item.get("streetAddress") or item.get("address") or "",
                    item.get("city", ""), item.get("state", ""),
                    str(item.get("zip") or item.get("zipCode") or ""),
                ])).strip(", ")
                if len(address) < 8 or state_code not in address.upper():
                    continue
                if county_name.lower() not in address.lower():
                    continue
                bid = clean_price(item.get("listPrice") or item.get("price") or 0)
                date_str = datetime.now().strftime('%Y-%m-%d')
                key = dedup_key(address, date_str)
                if key in seen:
                    continue
                seen.add(key)
                href = item.get("link") or item.get("url") or item.get("propertyUrl") or ""
                link = href if href.startswith("http") else f"https://www.hudhomestore.gov{href}"
                results.append(make_listing(address, "Auction", bid, date_str,
                                            county_name.lower(), link, "HUD"))
            except Exception:
                continue

    if results:
        print(f"[{tag}] Returning {len(results)} listings (API)")
        return results

    # HTML fallback — parse rendered Yardi SPA cards
    try:
        html = await page.content()
    except Exception as e:
        print(f"[{tag}] Could not get page content (site may be blocking headless): {e}")
        return results
    soup = BeautifulSoup(html, 'html.parser')
    cards = (
        soup.select('[class*="property-card"]') or
        soup.select('[class*="PropertyCard"]') or
        soup.select('[class*="listing-item"]') or
        soup.select('[class*="search-result"]') or
        []
    )
    print(f"[{tag}] {len(cards)} HTML cards found (fallback)")
    for card in cards:
        try:
            addr_el = (card.select_one('[class*="address" i]') or
                       card.select_one('h3') or card.select_one('h2'))
            if not addr_el:
                continue
            address = addr_el.get_text(separator=' ', strip=True)
            if len(address) < 8 or state_code not in address.upper():
                continue
            price_el = card.select_one('[class*="price" i]')
            bid = clean_price(price_el.get_text(strip=True)) if price_el else 0
            date_str = datetime.now().strftime('%Y-%m-%d')
            link_el = card.select_one('a[href]')
            href = (link_el['href'] if link_el else '').strip()
            if href.startswith('http'):
                link = href
            elif href and not href.startswith('javascript'):
                link = f"https://www.hudhomestore.gov{href}"
            else:
                link = "https://www.hudhomestore.gov"
            key = dedup_key(address, date_str)
            if key in seen:
                continue
            seen.add(key)
            results.append(make_listing(address, "Auction", bid, date_str,
                                        county_name.lower(), link, "HUD"))
        except Exception:
            continue

    print(f"[{tag}] Returning {len(results)} listings")
    return results


# ─── Source 2: Fannie Mae HomePath ────────────────────────────────────────────
async def scrape_homepath(page, state_code, county_name, seen):
    """
    Scrape Fannie Mae HomePath for REO properties.
    Domain moved to homepath.fanniemae.com in 2024.
    Intercepts the React SPA's JSON API calls during page load.
    """
    results = []
    tag = f"HomePath/{state_code}/{county_name}"
    intercepted = []

    async def capture(response):
        ct = response.headers.get("content-type", "")
        if "json" in ct:
            try:
                data = await response.json()
                if isinstance(data, (dict, list)):
                    intercepted.append(data)
            except Exception:
                pass

    page.on("response", capture)
    try:
        print(f"[{tag}] Fetching HomePath (homepath.fanniemae.com)...")
        # bounds=minLat,minLng,maxLat,maxLng for the state; county filter applied in code
        state_bounds = {
            "TN": "34.98,-90.31,36.68,-81.65",
            "FL": "24.39,-87.63,31.05,-79.97",
            "TX": "25.84,-106.65,36.50,-93.51",
            "GA": "30.36,-85.61,35.00,-80.77",
            "IL": "36.97,-91.51,42.51,-87.02",
            "AZ": "31.33,-114.82,37.00,-109.04",
            "CA": "32.53,-124.48,42.01,-114.13",
        }
        bounds = state_bounds.get(state_code, f"24,-125,49,-66")
        await page.goto(
            f"https://www.homepath.fanniemae.com/property-finder?bounds={bounds}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_timeout(6000)  # let React fire its API calls
    except Exception as e:
        print(f"[{tag}] Navigation error: {e}")
    finally:
        page.remove_listener("response", capture)

    print(f"[{tag}] {len(intercepted)} JSON responses intercepted")

    for data in intercepted:
        if isinstance(data, list):
            items = data
        else:
            items = (
                data.get("properties") or data.get("listings") or
                data.get("results") or data.get("data") or []
            )
        for item in (items or [])[:200]:
            if not isinstance(item, dict):
                continue
            try:
                item_state = (item.get("state") or "").strip().upper()
                item_county = (item.get("county") or "").strip().lower()
                if item_state != state_code:
                    continue
                if county_name.lower() not in item_county and item_county not in county_name.lower():
                    continue

                street = (item.get("addressLine1") or "").strip()
                city   = (item.get("city") or "").strip()
                zip_c  = str(item.get("zipCode") or "").strip()
                address = f"{street}, {city}, {item_state} {zip_c}".strip(", ") if street else f"{city}, {item_state}"
                if len(address) < 8:
                    continue

                # price field is in cents
                bid = clean_price(item.get("price") or 0) // 100
                date_str = datetime.now().strftime('%Y-%m-%d')
                key = dedup_key(address, date_str)
                if key in seen:
                    continue
                seen.add(key)
                reo_id = item.get("reoId") or item.get("externalSystemId") or ""
                link = (f"https://www.homepath.fanniemae.com/property/{reo_id}"
                        if reo_id else "https://www.homepath.fanniemae.com")
                results.append(make_listing(address, "Auction", bid, date_str,
                                            county_name.lower(), link, "HomePath"))
            except Exception:
                continue

    print(f"[{tag}] Returning {len(results)} listings")
    return results


# ─── Source 3: Davidson County (Nashville) public records ─────────────────────
async def scrape_davidson(page, lookback_days, seen):
    """
    Scrape Davidson County delinquent tax sale listings.
    Tax sales are run by the Chancery Court Clerk (not the Trustee).
    Property lists are published ~4 weeks before each sale in the TN Ledger.
    Falls back to Register of Deeds lis pendens search.
    """
    results = []
    tag = "Davidson/TN"
    addr_re = re.compile(
        r'\d+\s+\w[\w\s]{3,40}(?:Ave|St|Dr|Rd|Blvd|Ln|Ct|Way|Pike|Cir|'
        r'Road|Lane|Drive|Court|Boulevard|Street|Avenue|Trail|Terrace)\b',
        re.IGNORECASE,
    )

    # ── Primary: Chancery Court Clerk and Master ──────────────────────────────
    try:
        print(f"[{tag}] Fetching Chancery Court delinquent tax sales page...")
        await page.goto(
            "https://chanceryclerkandmaster.nashville.gov/fees/delinquent-tax-sales/",
            wait_until="networkidle",
            timeout=30000,
        )
        await page.wait_for_timeout(2000)
        html = await page.content()
        soup = BeautifulSoup(html, 'html.parser')
        for el in soup.select('p, li, td, strong'):
            for address in addr_re.findall(el.get_text(strip=True)):
                address = f"{address.strip()}, Nashville, TN"
                date_str = datetime.now().strftime('%Y-%m-%d')
                key = dedup_key(address, date_str)
                if key in seen:
                    continue
                seen.add(key)
                results.append(make_listing(
                    address, "Tax Lien", 0, date_str, "davidson",
                    "https://chanceryclerkandmaster.nashville.gov/fees/delinquent-tax-sales/",
                    "Davidson Co.",
                ))
        print(f"[{tag}] {len(results)} from Chancery Court page")
    except Exception as e:
        print(f"[{tag}] Chancery Court error: {e}")

    # ── Fallback: TN Ledger legal notices ────────────────────────────────────
    if not results:
        try:
            print(f"[{tag}] Trying TN Ledger legal notices...")
            await page.goto(
                "https://www.tnledger.com/editorial/Article.aspx?type=CL&state=TN&county=Davidson",
                wait_until="networkidle",
                timeout=30000,
            )
            await page.wait_for_timeout(2000)
            html = await page.content()
            soup = BeautifulSoup(html, 'html.parser')
            for el in soup.select('p, .article-content p, .listing-item'):
                text = el.get_text(strip=True)
                if not any(k in text.lower() for k in ('foreclos', 'tax sale', 'lis pendens')):
                    continue
                for address in addr_re.findall(text):
                    address = f"{address.strip()}, Nashville, TN"
                    date_str = datetime.now().strftime('%Y-%m-%d')
                    key = dedup_key(address, date_str)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append(make_listing(
                        address, "Pre-foreclosure", 0, date_str,
                        "davidson", "https://www.tnledger.com", "TN Ledger",
                    ))
            print(f"[{tag}] {len(results)} from TN Ledger")
        except Exception as e:
            print(f"[{tag}] TN Ledger error: {e}")


    print(f"[{tag}] Returning {len(results)} listings")
    return results


# ─── Source 4: Wilson County (Lebanon, TN) public records ─────────────────────
async def scrape_wilson(page, lookback_days, seen):
    """
    Scrape Wilson County Clerk and Master court sales.
    Actual listing site is wilcoclerkandmaster.com/court-sales/ (WordPress).
    Properties appear as plain HTML prose in <p>/<strong> tags, not tables.
    """
    results = []
    tag = "Wilson/TN"
    addr_re = re.compile(
        r'\d+\s+\w[\w\s]{3,40}(?:Ave|St|Dr|Rd|Blvd|Ln|Ct|Way|Pike|Cir|'
        r'Hollow|Hill|Road|Lane|Drive|Court|Boulevard|Street|Avenue|Trail|Terrace)\b',
        re.IGNORECASE,
    )
    date_re = re.compile(
        r'\b(?:January|February|March|April|May|June|July|August|September|'
        r'October|November|December)\s+\d{1,2},?\s+\d{4}\b',
        re.IGNORECASE,
    )

    try:
        print(f"[{tag}] Fetching Wilson County Clerk and Master court sales...")
        await page.goto(
            "https://wilcoclerkandmaster.com/court-sales/",
            wait_until="networkidle",
            timeout=30000,
        )
        await page.wait_for_timeout(2000)

        html = await page.content()
        soup = BeautifulSoup(html, 'html.parser')

        content = soup.select_one('.entry-content') or soup.select_one('article') or soup
        current_date = None

        for el in content.select('h1, h2, h3, h4, p, li, strong'):
            text = el.get_text(strip=True)

            # Track sale date from headings
            dm = date_re.search(text)
            if dm:
                try:
                    current_date = datetime.strptime(
                        dm.group().replace(',', ''), '%B %d %Y'
                    ).strftime('%Y-%m-%d')
                except Exception:
                    current_date = datetime.now().strftime('%Y-%m-%d')

            for address in addr_re.findall(text):
                address = address.strip()
                if 'TN' not in address and 'Lebanon' not in address:
                    address = f"{address}, Lebanon, TN"
                date_str = current_date or datetime.now().strftime('%Y-%m-%d')
                key = dedup_key(address, date_str)
                if key in seen:
                    continue
                seen.add(key)
                results.append(make_listing(
                    address, "Tax Lien", 0, date_str,
                    "wilson-tn",
                    "https://wilcoclerkandmaster.com/court-sales/",
                    "Wilson Co.",
                ))

        print(f"[{tag}] {len(results)} entries found")

    except Exception as e:
        print(f"[{tag}] Error: {e}")

    print(f"[{tag}] Returning {len(results)} listings")
    return results


# ─── Main per-county dispatcher ───────────────────────────────────────────────
DEFAULT_SCRAPE_OPTIONS = {
    "include_tax_records": True,
    "include_hud": True,
    "include_homepath": True,
}


def emit_results(callback, results):
    if callback and results:
        callback(results)


def emit_progress(callback, message):
    if callback:
        callback(message)


async def scrape_county(
    page,
    county_key,
    lookback_days,
    seen,
    stop_event=None,
    options=None,
    on_results=None,
    on_progress=None,
):
    if stop_event and stop_event.is_set():
        return []
    options = {**DEFAULT_SCRAPE_OPTIONS, **(options or {})}
    config = COUNTY_CONFIG.get(county_key)
    if not config:
        print(f"[{county_key}] No config, skipping.")
        return []

    state  = config["state"]
    county = config["county"]

    all_results = []

    # Tennessee counties get dedicated public-records scrapers
    if options["include_tax_records"] and county_key == "davidson":
        emit_progress(on_progress, "Davidson TN tax records")
        results = await scrape_davidson(page, lookback_days, seen)
        all_results += results
        emit_results(on_results, results)
        emit_progress(on_progress, f"Davidson TN tax records complete ({len(results)} found)")

    elif options["include_tax_records"] and county_key == "wilson-tn":
        emit_progress(on_progress, "Wilson TN tax records")
        results = await scrape_wilson(page, lookback_days, seen)
        all_results += results
        emit_results(on_results, results)
        emit_progress(on_progress, f"Wilson TN tax records complete ({len(results)} found)")

    if stop_event and stop_event.is_set():
        return all_results

    # All counties also get HUD Homestore + HomePath
    if options["include_hud"]:
        emit_progress(on_progress, f"{county} {state} HUD auctions")
        results = await scrape_hud(page, state, county, lookback_days, seen)
        all_results += results
        emit_results(on_results, results)
        emit_progress(on_progress, f"{county} {state} HUD auctions complete ({len(results)} found)")
    if stop_event and stop_event.is_set():
        return all_results
    if options["include_homepath"]:
        emit_progress(on_progress, f"{county} {state} HomePath REO")
        results = await scrape_homepath(page, state, county, seen)
        all_results += results
        emit_results(on_results, results)
        emit_progress(on_progress, f"{county} {state} HomePath REO complete ({len(results)} found)")

    return all_results


# ─── Runner ───────────────────────────────────────────────────────────────────
async def run_scraper(
    counties,
    lookback_days=30,
    stop_event=None,
    options=None,
    on_results=None,
    on_progress=None,
):
    options = {**DEFAULT_SCRAPE_OPTIONS, **(options or {})}
    seen = set()
    all_results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        # Hide webdriver flag that sites use to detect headless browsers
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        for county in counties:
            if stop_event and stop_event.is_set():
                break
            emit_progress(on_progress, f"Starting {county}")
            results = await scrape_county(
                page,
                county,
                lookback_days,
                seen,
                stop_event=stop_event,
                options=options,
                on_results=on_results,
                on_progress=on_progress,
            )
            all_results.extend(results)
            emit_progress(on_progress, f"Finished {county} ({len(results)} found)")

        await browser.close()

    return all_results


def scrape_sync(
    counties,
    lookback_days=30,
    stop_event: Event = None,
    options=None,
    on_results=None,
    on_progress=None,
):
    """Synchronous wrapper for calling from Flask threads on Windows."""
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            run_scraper(
                counties,
                lookback_days,
                stop_event=stop_event,
                options=options,
                on_results=on_results,
                on_progress=on_progress,
            )
        )
    finally:
        loop.close()


if __name__ == "__main__":
    import sys, os

    def _debug_progress(msg):
        print(f"  >> {msg}")

    counties = sys.argv[1:] or ["davidson", "wilson-tn"]
    data = scrape_sync(counties, lookback_days=30, on_progress=_debug_progress)
    print(json.dumps(data, indent=2))
    print(f"\nTotal: {len(data)} leads")

    # Write results into listings.json so the dashboard reflects the run
    listings_file = os.path.join(os.path.dirname(__file__), "listings.json")
    if os.path.exists(listings_file):
        with open(listings_file) as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                existing = []
    else:
        existing = []
    seen_ids = {str(x.get("id")) for x in existing}
    merged = existing + [x for x in data if str(x.get("id")) not in seen_ids]
    with open(listings_file, "w") as f:
        json.dump(merged, f, indent=4)
    print(f"listings.json updated -> {len(merged)} total")
