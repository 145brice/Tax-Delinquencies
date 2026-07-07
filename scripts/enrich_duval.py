"""
Enrich Duval County tax-lien listings with real data from the Duval County
Property Appraiser (paopropertysearch.coj.net).

The scraped Jax records only stored an APN (e.g. "APN 004210-0000, Jacksonville,
FL") plus a bid, a junk price=5, and some corrupted bid values. Every APN is a
real Duval RE#, and the PAO detail page has the site address, owner name,
mailing address, and market value. This backfills those.

Resumable: every lookup is cached to parcels_cache_duval.json, so re-running
picks up where it left off. Run standalone; it rewrites listings.json only at
the end (and periodically) so an interrupted run never corrupts the store.

    python scripts/enrich_duval.py            # full run
    python scripts/enrich_duval.py --limit 15 # validate on a sample
"""
import argparse
import json
import os
import random
import re
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LISTINGS = os.path.join(ROOT, "listings.json")
CACHE = os.path.join(ROOT, "data", "parcels_cache_duval.json")
PAO_URL = "https://paopropertysearch.coj.net/Basic/Detail.aspx?RE={re}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _inner(txt, span_id):
    m = re.search(re.escape(span_id) + r"[^>]*>(.*?)</span>", txt, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


def _money_to_int(val):
    if not val:
        return None
    digits = re.sub(r"[^\d]", "", val.split(".")[0])
    return int(digits) if digits else None


def fetch_parcel(re_num, session):
    """Return dict of enriched fields for a Duval RE#, or {} on miss."""
    r = session.get(PAO_URL.format(re=re_num), headers=HEADERS, timeout=20)
    if r.status_code != 200 or len(r.text) < 5000:
        return {}
    t = r.text
    line1 = _inner(t, "ctl00_cphBody_lblPrimarySiteAddressLine1")
    line2 = _inner(t, "ctl00_cphBody_lblPrimarySiteAddressLine2")
    owner = _inner(t, "ctl00_cphBody_repeaterOwnerInformation_ctl00_lblOwnerName")
    mail1 = _inner(t, "ctl00_cphBody_repeaterOwnerInformation_ctl00_lblMailingAddressLine1")
    mail2 = _inner(t, "ctl00_cphBody_repeaterOwnerInformation_ctl00_lblMailingAddressLine2")
    mail3 = _inner(t, "ctl00_cphBody_repeaterOwnerInformation_ctl00_lblMailingAddressLine3")
    just = _money_to_int(_inner(t, "ctl00_cphBody_lblJustMarketValueCertified")) \
        or _money_to_int(_inner(t, "ctl00_cphBody_lblJustMarketValueInProgress"))

    site = None
    if line1:
        # normalize "Jacksonville FL 32219-" -> "Jacksonville, FL 32219"
        loc = re.sub(r"\s+", " ", (line2 or "")).strip().rstrip("-").strip()
        loc = re.sub(r"\b([A-Za-z ]+) (FL) (\d{5})", r"\1, \2 \3", loc)
        site = f"{line1.strip()}, {loc}" if loc else line1.strip()

    mailing = ", ".join(p.strip() for p in (mail1, mail2, mail3) if p and p.strip())
    return {
        "site_address": site,
        "owner": owner or None,
        "mailing_address": mailing or None,
        "market_value": just,
    }


def re_num_for(rec):
    """Duval RE#: prefer the stored parcel_id, else parse it out of the address."""
    pid = (rec.get("parcel_id") or "").strip()
    if pid:
        return pid.replace("-", "")
    m = re.search(r"APN\s+([\d-]+)", rec.get("address") or "")
    return m.group(1).replace("-", "") if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="only process N records (sample)")
    ap.add_argument("--sleep", type=float, default=0.25, help="base delay between requests")
    args = ap.parse_args()

    listings = json.load(open(LISTINGS, encoding="utf-8"))
    cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}

    targets = [
        r for r in listings
        if (r.get("county") or "").lower() == "duval"
        and re_num_for(r)
        and not (r.get("street") or "").strip()
    ]
    if args.limit:
        targets = targets[: args.limit]
    print(f"Duval records needing a street address: {len(targets)} (cache has {len(cache)})")

    session = requests.Session()
    ok = miss = fromcache = 0
    for i, rec in enumerate(targets, 1):
        re_num = re_num_for(rec)
        if not re_num:
            miss += 1
            continue

        if re_num in cache:
            info = cache[re_num]
            fromcache += 1
        else:
            try:
                info = fetch_parcel(re_num, session)
            except Exception as e:
                print(f"  [{i}/{len(targets)}] {re_num} ERROR {e}")
                info = {}
            cache[re_num] = info
            time.sleep(args.sleep + random.random() * 0.2)

        if info.get("site_address"):
            ok += 1
        else:
            miss += 1

        if i % 100 == 0:
            json.dump(cache, open(CACHE, "w", encoding="utf-8"))
            print(f"  [{i}/{len(targets)}] ok={ok} miss={miss} cached={fromcache}")

    json.dump(cache, open(CACHE, "w", encoding="utf-8"))
    print(f"Lookups done: ok={ok} miss={miss} from_cache={fromcache}")

    # ---- Apply enrichment, then write back ----
    # NOTE: `bid` already holds amount_owed in cents (bid 210595 == "$2,105.95")
    # and is left untouched. We only backfill the missing street address, the
    # real market value (the stored price=5 is junk), owner if blank, and the
    # owner's mailing address (useful for skip tracing).
    applied = 0
    for rec in listings:
        if (rec.get("county") or "").lower() != "duval":
            continue
        re_num = re_num_for(rec)
        info = cache.get(re_num) if re_num else None
        if not (info and info.get("site_address")):
            continue

        line1 = info["site_address"].split(",")[0].strip()
        rec["apn"] = re_num
        rec["address"] = info["site_address"]
        rec["street"] = line1
        if "FL" in info["site_address"]:
            zipm = re.search(r"(\d{5})", info["site_address"].split(",")[-1])
            if zipm:
                rec["zip"] = zipm.group(1)
        if info.get("market_value"):
            rec["price"] = info["market_value"]   # replaces junk price=5
        elif rec.get("price") == 5:
            rec["price"] = None
        if info.get("owner") and not (rec.get("owner") or "").strip():
            rec["owner"] = info["owner"]
        if info.get("mailing_address"):
            rec["mailing_address"] = info["mailing_address"]
        applied += 1

    json.dump(listings, open(LISTINGS, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"Applied enrichment to {applied} records. listings.json rewritten.")


if __name__ == "__main__":
    main()
