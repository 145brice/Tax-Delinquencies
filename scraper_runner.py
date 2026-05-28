"""
Main runner — scrapes all Nashville-area counties and saves results to CSV.

Usage:
    python scraper_runner.py                    # Run all counties
    python scraper_runner.py --county davidson  # Run one county
    python scraper_runner.py --county davidson williamson rutherford
"""
import argparse
import csv
import os
from datetime import date
from dataclasses import fields

from scrapers.base_scraper import PropertyRecord
from scrapers.davidson import DavidsonScraper
from scrapers.williamson import WilliamsonScraper
from scrapers.rutherford import RutherfordScraper
from scrapers.wilson import WilsonScraper
from scrapers.sumner import SumnerScraper
from scrapers.robertson import RobertsonScraper
from scrapers.cheatham import CheathamScraper
from scrapers.sandiego_taxsale import SanDiegoTaxSaleScraper
from scrapers.sandiego_legalnotices import SanDiegoLegalNoticesScraper
from scrapers.losangeles_legalnotices import LosAngelesLegalNoticesScraper
from scrapers.orange_legalnotices import OrangeLegalNoticesScraper
from scrapers.orange_taxsale import OrangeTaxSaleScraper
from scrapers.riverside_legalnotices import RiversideLegalNoticesScraper

ALL_SCRAPERS = {
    "davidson": DavidsonScraper,
    "williamson": WilliamsonScraper,
    "rutherford": RutherfordScraper,
    "wilson": WilsonScraper,
    "sumner": SumnerScraper,
    "robertson": RobertsonScraper,
    "cheatham": CheathamScraper,
    "sandiego_taxsale": SanDiegoTaxSaleScraper,
    "sandiego_legalnotices": SanDiegoLegalNoticesScraper,
    "losangeles_legalnotices": LosAngelesLegalNoticesScraper,
    "orange_taxsale": OrangeTaxSaleScraper,
    "orange_legalnotices": OrangeLegalNoticesScraper,
    "riverside_legalnotices": RiversideLegalNoticesScraper,
}

# Region grouping — used to pick the output CSV filename
REGION_BY_KEY = {
    "davidson": "nashville", "williamson": "nashville", "rutherford": "nashville",
    "wilson": "nashville", "sumner": "nashville", "robertson": "nashville",
    "cheatham": "nashville",
    "sandiego_taxsale": "sandiego", "sandiego_legalnotices": "sandiego",
    "losangeles_legalnotices": "losangeles",
    "orange_taxsale": "orange", "orange_legalnotices": "orange",
    "riverside_legalnotices": "riverside",
}

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CSV_FIELDNAMES = [f.name for f in fields(PropertyRecord)]


def run_scrapers(county_names: list[str]) -> list[dict]:
    all_records = []
    for name in county_names:
        cls = ALL_SCRAPERS.get(name.lower())
        if not cls:
            print(f"[WARNING] Unknown county: {name}")
            continue
        scraper = cls()
        try:
            records = scraper.scrape()
            all_records.extend([r.to_dict() for r in records])
            print(f"[{name.title()}] {len(records)} records scraped.")
        except Exception as e:
            print(f"[{name.title()}] ERROR: {e}")
    return all_records


def save_csv(records: list[dict], output_path: str):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(records)
    print(f"[CSV] Saved {len(records)} records to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Nashville-area tax delinquency & pre-foreclosure scraper")
    parser.add_argument(
        "--county",
        nargs="+",
        default=list(ALL_SCRAPERS.keys()),
        choices=list(ALL_SCRAPERS.keys()),
        help="County or counties to scrape (default: all)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output CSV file path (default: data/nashville_<date>.csv)",
    )
    args = parser.parse_args()

    if args.output:
        output_path = args.output
    else:
        regions = {REGION_BY_KEY.get(c, "scrape") for c in args.county}
        prefix = next(iter(regions)) if len(regions) == 1 else "multi"
        output_path = os.path.join(DATA_DIR, f"{prefix}_{date.today()}.csv")

    print(f"Scraping counties: {', '.join(args.county)}")
    records = run_scrapers(args.county)
    save_csv(records, output_path)
    print(f"\nDone. {len(records)} total records. CSV: {output_path}")
    return output_path


if __name__ == "__main__":
    main()
