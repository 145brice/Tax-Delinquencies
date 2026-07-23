"""
Main runner for source-registered public-record scrapers.

Usage:
    python scraper_runner.py
    python scraper_runner.py --county duval_jaxdailyrecord
    python scraper_runner.py --county davidson wilson
"""
import argparse
import csv
import os
import re
import time
from dataclasses import fields
from datetime import date, datetime, timedelta

from scrapers.base_scraper import PropertyRecord
from scrapers.source_registry import ALL_SCRAPERS, REGION_BY_KEY, SOURCE_METADATA


DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CSV_FIELDNAMES = [f.name for f in fields(PropertyRecord)]


def _record_date(value: str, today: date) -> date | None:
    value = (value or "").strip()
    if not value:
        return None

    # Handle ranges like "March 13 - March 18, 2026" by using the first date.
    range_match = re.search(r"([A-Z][a-z]+)\s+(\d{1,2})\s*-\s*[A-Z][a-z]+\s+\d{1,2},\s+(\d{4})", value)
    if range_match:
        value = f"{range_match.group(1)} {range_match.group(2)}, {range_match.group(3)}"

    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def _within_lookback(record: dict, lookback_days: int | None, today: date | None = None) -> bool:
    if not lookback_days:
        return True
    today = today or date.today()
    cutoff = today - timedelta(days=lookback_days)

    # Standing statuses (a parcel currently on the county's delinquent roll)
    # are current by virtue of appearing in today's scrape, even though the
    # underlying bill was posted months or years ago. Judge those by
    # scraped_date; sale_date on them is the original posting/bill date and
    # would wrongly age them out.
    record_type = (record.get("record_type") or "").lower()
    if "delinquent" in record_type or "tax lien" in record_type:
        dt = _record_date(record.get("scraped_date", ""), today)
        return dt >= cutoff if dt else True

    for field in ("sale_date", "scraped_date"):
        dt = _record_date(record.get(field, ""), today)
        if dt:
            # Future auction/sale dates are active, so keep them.
            return dt >= cutoff
    # Some source lists do not expose record dates; keep those instead of silently
    # throwing away current public lists.
    return True


def _fmt_secs(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def run_scrapers(county_names: list[str], lookback_days: int | None = None) -> list[dict]:
    all_records = []
    timings: list[tuple[str, float, int]] = []
    for name in county_names:
        key = name.lower()
        cls = ALL_SCRAPERS.get(key)
        if not cls:
            print(f"[WARNING] Unknown scraper source: {name}")
            continue
        scraper = cls()
        scraper.lookback_days = lookback_days
        label = SOURCE_METADATA.get(key, {}).get("label", key)
        t0 = time.monotonic()
        try:
            records = scraper.scrape()
            elapsed = time.monotonic() - t0
            rows = [r.to_dict() for r in records]
            kept = [r for r in rows if _within_lookback(r, lookback_days)]
            all_records.extend(kept)
            timings.append((label, elapsed, len(kept)))
            if len(kept) != len(rows):
                print(f"[{label}] {len(rows)} records scraped, {len(kept)} within {lookback_days} days. ({_fmt_secs(elapsed)})")
            else:
                print(f"[{label}] {len(records)} records scraped. ({_fmt_secs(elapsed)})")
        except Exception as e:
            elapsed = time.monotonic() - t0
            timings.append((label, elapsed, 0))
            print(f"[{label}] ERROR after {_fmt_secs(elapsed)}: {e}")

    # Timing summary, slowest first — spot sources worth cutting.
    if len(timings) > 1:
        print("\n=== Scrape timing (slowest first) ===")
        for label, elapsed, kept in sorted(timings, key=lambda t: -t[1]):
            rate = f" ({elapsed / kept:.1f}s/record)" if kept else ""
            print(f"  {_fmt_secs(elapsed):>8s}  {label} - {kept} records{rate}")
        total = sum(t[1] for t in timings)
        print(f"  {_fmt_secs(total):>8s}  TOTAL")
    return all_records


def save_csv(records: list[dict], output_path: str):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(records)
    print(f"[CSV] Saved {len(records)} records to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Public-record foreclosure source runner")
    parser.add_argument(
        "--county",
        nargs="+",
        default=list(ALL_SCRAPERS.keys()),
        choices=list(ALL_SCRAPERS.keys()),
        help="Scraper source key or keys to scrape (default: all)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output CSV file path (default: data/<region>_<date>.csv)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="Only keep records with parseable sale/scraped dates within this many days.",
    )
    args = parser.parse_args()

    if args.output:
        output_path = args.output
    else:
        regions = {REGION_BY_KEY.get(c, "scrape") for c in args.county}
        prefix = next(iter(regions)) if len(regions) == 1 else "multi"
        output_path = os.path.join(DATA_DIR, f"{prefix}_{date.today()}.csv")

    print(f"Scraping sources: {', '.join(args.county)}")
    records = run_scrapers(args.county, lookback_days=args.lookback_days)
    save_csv(records, output_path)
    print(f"\nDone. {len(records)} total records. CSV: {output_path}")
    return output_path


if __name__ == "__main__":
    main()
