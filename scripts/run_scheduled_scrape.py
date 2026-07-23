"""
Triggers a scrape run on the live app via /api/scrape for one frequency
bucket (daily / weekly / monthly) from county_schedule.json — then inspects
per-source results and auto-heals what it can:

  1. Sources that ERRORED are retried once (covers transient failures —
     slow county servers, dropped connections, rate limits).
  2. Sources that still error after the retry fail the workflow (exit 1),
     so GitHub Actions flags the run and emails the repo owner. Structural
     breakage (site redesigns, moved data) can't be auto-coded; loud early
     detection is the fix for that.
  3. Sources that come back EMPTY (stub/portal-link only) are listed as
     warnings — an empty result can be legitimate (e.g. a county with no
     sale currently posted), so it warns rather than fails.

Usage:
    python scripts/run_scheduled_scrape.py --frequency daily

Env vars required:
    SCRAPE_TARGET_URL   base URL of the deployed app
    ADMIN_TOKEN         matches ADMIN_TOKEN / SKIPTRACE_ADMIN_TOKEN on the server
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

SCHEDULE_PATH = Path(__file__).parent / "county_schedule.json"
POLL_INTERVAL = 20          # seconds between status checks
POLL_LIMIT = 180            # max polls (180 * 20s = 60 min per run)


def load_schedule() -> dict:
    return json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))["counties"]


def counties_for_frequency(schedule: dict, frequency: str) -> list[str]:
    return [ui for ui, cfg in schedule.items() if cfg["frequency"] == frequency]


def source_to_county(schedule: dict) -> dict:
    return {src: ui for ui, cfg in schedule.items() for src in cfg["sources"]}


class ScrapeClient:
    def __init__(self):
        self.base = os.environ["SCRAPE_TARGET_URL"].rstrip("/")
        self.token = os.environ["ADMIN_TOKEN"]

    def trigger(self, counties: list[str], lookback_days: int | None = None) -> dict:
        payload: dict = {"counties": counties}
        if lookback_days:
            payload["lookback_days"] = lookback_days
        r = requests.post(f"{self.base}/api/scrape", params={"token": self.token},
                          json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def status(self) -> dict:
        r = requests.get(f"{self.base}/api/scrape/status", params={"token": self.token},
                         timeout=15)
        r.raise_for_status()
        return r.json()

    def wait(self) -> dict:
        """Poll until the run finishes; returns the final status."""
        last = ""
        for _ in range(POLL_LIMIT):
            time.sleep(POLL_INTERVAL)
            try:
                st = self.status()
            except requests.RequestException as e:
                print(f"  (status check failed, will retry: {e})")
                continue
            step = st.get("current_step") or st.get("last") or ""
            if step != last:
                print(f"  {step}")
                last = step
            if not st.get("running"):
                return st
        print("Timed out waiting for scrape to finish (still running server-side).")
        return self.status()


def split_results(results: dict) -> tuple[list[str], list[str], list[str]]:
    """Partition per-source results into (ok, empty, errored) source keys."""
    ok, empty, errored = [], [], []
    for src, res in results.items():
        if src.startswith("_") or src == "hud_homepath":
            continue
        status = (res or {}).get("status", "")
        if status == "error":
            errored.append(src)
        elif status == "empty":
            empty.append(src)
        else:
            ok.append(src)
    return ok, empty, errored


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frequency", required=True, choices=["daily", "weekly", "monthly"])
    parser.add_argument("--lookback-days", type=int, default=None)
    args = parser.parse_args()

    missing = [v for v in ("SCRAPE_TARGET_URL", "ADMIN_TOKEN") if not os.environ.get(v)]
    if missing:
        print("CONFIGURATION ERROR - cannot reach the app.")
        print(f"Missing environment variable(s): {', '.join(missing)}")
        print("Fix: GitHub repo -> Settings -> Secrets and variables -> Actions ->")
        print("     New repository secret. Add SCRAPE_TARGET_URL (the deployed app's")
        print("     base URL) and ADMIN_TOKEN (the app's admin token).")
        print("Until then, scheduled scrapes cannot run.")
        return 1

    schedule = load_schedule()
    counties = counties_for_frequency(schedule, args.frequency)
    src_to_county = source_to_county(schedule)
    client = ScrapeClient()

    print(f"Triggering {args.frequency} scrape for {len(counties)} counties: {', '.join(counties)}")
    resp = client.trigger(counties, args.lookback_days)
    print(resp)
    if resp.get("status") == "already_running":
        print("A scrape was already in progress; nothing new started.")
        return 0

    final = client.wait()
    results = final.get("scraper_results") or {}
    ok, empty, errored = split_results(results)
    print(f"\nRun complete: {len(ok)} ok, {len(empty)} empty, {len(errored)} errored")

    # ── Auto-heal: retry errored sources once ────────────────────────────────
    if errored:
        retry_counties = sorted({src_to_county[s] for s in errored if s in src_to_county})
        print(f"\nAuto-retrying {len(errored)} errored source(s) via counties: {', '.join(retry_counties)}")
        for src in errored:
            note = (results.get(src) or {}).get("note", "")
            print(f"  {src}: {note}")
        resp = client.trigger(retry_counties, args.lookback_days)
        if resp.get("status") == "already_running":
            print("Retry blocked: another scrape started meanwhile.")
        else:
            retry_final = client.wait()
            retry_results = retry_final.get("scraper_results") or {}
            _, _, still_errored = split_results(
                {s: retry_results.get(s) for s in errored if s in retry_results}
            )
            recovered = [s for s in errored if s not in still_errored]
            if recovered:
                print(f"Recovered on retry: {', '.join(recovered)}")
            errored = still_errored

    # ── Report ───────────────────────────────────────────────────────────────
    if empty:
        print("\nWARNING - sources returned no real records (stub only).")
        print("May be legitimate (no current sale posted) or a broken scraper:")
        for src in empty:
            note = (results.get(src) or {}).get("note", "")
            print(f"  {src}: {note}")

    if errored:
        print("\nFAILED - sources still erroring after retry (needs a human):")
        for src in errored:
            print(f"  {src}")
        return 1

    print("\nAll sources completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
