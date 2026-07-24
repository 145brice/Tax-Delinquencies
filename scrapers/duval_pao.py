"""
Duval County (Jacksonville) Property Appraiser parcel -> street address lookup.

The Duval tax-certificate list gives a parcel (RE) number, owner, and amount but
no street address. The Property Appraiser's public detail page resolves an RE
number to the property's situs (street) address:

    https://paopropertysearch.coj.net/Basic/Detail.aspx?RE=<10-digit RE>

We fetch that page and parse the "Property Address" field. City is Jacksonville
(the whole county is the Jacksonville metro); the page carries no situs ZIP.
"""
import re
import time

import requests

DETAIL_URL = "https://paopropertysearch.coj.net/Basic/Detail.aspx"
_ADDR_RE = re.compile(r'Property Address"[^>]*>([^<]+)', re.I)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Politeness between requests to the county server (seconds).
REQUEST_GAP = 0.6


def re_number(parcel_id: str) -> str:
    """Normalize a parcel id like '008629-0160' to the 10-digit RE number."""
    return re.sub(r"\D", "", str(parcel_id or ""))


def lookup_street(parcel_id: str, session: requests.Session | None = None,
                  timeout: int = 20) -> str:
    """Return the situs street address for a Duval RE number, or '' if not
    found. A leading '0' house number (vacant land) is returned as-is — it's
    the county's real value, not an error."""
    re_num = re_number(parcel_id)
    if len(re_num) < 8:
        return ""
    sess = session or requests
    try:
        resp = sess.get(DETAIL_URL, params={"RE": re_num},
                        headers={"User-Agent": _UA}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        return ""
    m = _ADDR_RE.search(resp.text)
    if not m:
        return ""
    street = re.sub(r"\s+", " ", m.group(1)).strip()
    # Skip empty / placeholder responses.
    if not street or street in {"0", "-"} or not re.search(r"[A-Za-z]", street):
        return ""
    return street


def enrich_parcels(parcels, session=None, on_progress=None):
    """Look up a list of parcel ids. Yields (parcel_id, street) for each hit.
    Reuses one session and paces requests politely."""
    sess = session or requests.Session()
    for i, parcel in enumerate(parcels):
        street = lookup_street(parcel, session=sess)
        if street:
            yield parcel, street
        if on_progress:
            on_progress(i + 1, len(parcels))
        time.sleep(REQUEST_GAP)
