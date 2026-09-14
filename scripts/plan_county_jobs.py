"""Select county jobs whose local posting window has just closed."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request
from zoneinfo import ZoneInfo


SCHEDULE_PATH = Path(__file__).with_name("county_schedule.json")


def load_config() -> dict:
    return json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))


def county_windows(config: dict) -> dict[str, dict]:
    windows = {}
    for window in config.get("posting_windows", {}).values():
        for county in window.get("counties", []):
            windows.setdefault(county, window)
    return windows


def load_inventory() -> list[dict]:
    base_url = os.environ.get("SCRAPE_TARGET_URL", "").strip().rstrip("/")
    token = os.environ.get("ADMIN_TOKEN", "").strip()
    if not base_url or not token:
        return []
    request = urllib.request.Request(
        f"{base_url}/api/admin/scrape-runs?limit=5000",
        headers={"X-Admin-Token": token, "User-Agent": "county-schedule-optimizer"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response).get("runs", [])
    except Exception as exc:
        print(f"Inventory unavailable; using baseline schedule: {exc}", file=sys.stderr)
        return []


def aggregate_county_runs(entries: list[dict]) -> dict[str, list[dict]]:
    """Combine multi-source receipts into one observation per county job."""
    grouped = {}
    for entry in entries:
        county = str(entry.get("county") or "")
        if not county or entry.get("status") not in {"complete", "failed"}:
            continue
        if entry.get("workflow_run_id"):
            job = f"{entry['workflow_run_id']}:{entry.get('workflow_run_attempt') or '1'}"
        else:
            job = str(entry.get("run_id") or entry.get("id") or "")
        key = (county, job)
        current = grouped.setdefault(key, {
            "county": county, "started_at": entry.get("started_at") or entry.get("first_received_at"),
            "added": 0, "raw": 0, "successful_sources": 0,
        })
        current["added"] += int(entry.get("added") or 0)
        current["raw"] += int(entry.get("raw") or 0)
        if entry.get("status") == "complete":
            current["successful_sources"] += 1
    by_county = defaultdict(list)
    for current in grouped.values():
        if current["successful_sources"]:
            by_county[current["county"]].append(current)
    return by_county


def _parse_time(value: str):
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def learned_slot(county: str, frequency: str, timezone_name: str, runs: list[dict]):
    """Return a proven local slot, or None while evidence is insufficient."""
    observations = []
    tz = ZoneInfo(timezone_name)
    for run in runs:
        started = _parse_time(run.get("started_at"))
        if started:
            observations.append((started.astimezone(tz), int(run.get("added") or 0)))
    thresholds = {
        "daily": (20, 4, 4, 21),
        "weekly": (12, 6, 3, 42),
        "monthly": (12, 6, 3, 90),
    }
    required, varied, productive, span_days = thresholds.get(frequency, (20, 6, 4, 42))
    if len(observations) < required or sum(added > 0 for _, added in observations) < productive:
        return None
    if (max(dt for dt, _ in observations) - min(dt for dt, _ in observations)).days < span_days:
        return None

    if frequency == "daily":
        bucket = lambda dt: dt.hour
    elif frequency == "weekly":
        bucket = lambda dt: (dt.weekday(), dt.hour)
    else:
        bucket = lambda dt: (dt.day, dt.hour)
    buckets = defaultdict(list)
    for dt, added in observations:
        buckets[bucket(dt)].append(added)
    if len(buckets) < varied:
        return None

    # Laplace-smoothed productivity rate, then average new inventory and
    # sample count. This resists switching to a slot after one lucky run.
    def score(item):
        _, values = item
        wins = sum(value > 0 for value in values)
        return ((wins + 1) / (len(values) + 2), sum(values) / len(values), len(values))
    sufficiently_sampled = [(key, values) for key, values in buckets.items() if len(values) >= 2]
    return max(sufficiently_sampled or list(buckets.items()), key=score)[0]


def exploration_slot(county: str, frequency: str, local: datetime):
    """Assign one deterministic probe slot per week/month across useful hours."""
    if frequency == "monthly":
        period = f"{local.year}-{local.month:02d}"
        candidates = [(day, hour) for day in (1, 7, 14, 21, 28) for hour in (7, 11, 15)]
    else:
        iso = local.isocalendar()
        period = f"{iso.year}-W{iso.week:02d}"
        days = range(5) if frequency == "daily" else range(6)
        hours = (4, 10, 13, 16, 19) if frequency == "daily" else (4, 7, 10, 13, 16, 19)
        candidates = [(day, hour) for day in days for hour in hours]
    digest = hashlib.sha256(f"{county}:{period}".encode()).digest()
    return candidates[int.from_bytes(digest[:4], "big") % len(candidates)]


def due_counties(config: dict, now: datetime, inventory: list[dict] | None = None,
                 detailed: bool = False) -> list:
    scheduled = []
    windows = county_windows(config)
    by_county = aggregate_county_runs(inventory or [])
    for county, county_config in config.get("counties", {}).items():
        window = windows.get(county)
        if not window:
            continue
        local = now.astimezone(ZoneInfo(window["timezone"]))
        frequency = county_config.get("frequency", "weekly")
        learned = learned_slot(county, frequency, window["timezone"], by_county.get(county, []))
        if frequency == "daily":
            baseline_due = local.weekday() in window.get("weekdays", []) and local.hour == int(window["local_hour"])
            learned_due = learned is not None and local.weekday() in window.get("weekdays", []) and local.hour == learned
        elif frequency == "weekly":
            baseline_due = local.weekday() in window.get("weekdays", []) and local.hour == int(window["local_hour"])
            learned_due = learned is not None and (local.weekday(), local.hour) == learned
        else:
            baseline_due = local.day in window.get("month_days", []) and local.hour == int(window["local_hour"])
            learned_due = learned is not None and (local.day, local.hour) == learned

        due = learned_due if learned is not None else baseline_due
        probe = exploration_slot(county, frequency, local)
        if frequency == "monthly":
            probe_due = (local.day, local.hour) == probe
        else:
            probe_due = (local.weekday(), local.hour) == probe
        # Continue one probe every fourth period after learning so changes in
        # county behavior can eventually displace a stale recommendation.
        if learned is not None:
            period_number = local.month if frequency == "monthly" else local.isocalendar().week
            probe_due = probe_due and period_number % 4 == 0
        if due or probe_due:
            reason = "learned" if learned_due else "baseline" if baseline_due else "exploration"
            scheduled.append({"county": county, "schedule_reason": reason,
                              "scheduled_local_time": local.isoformat()} if detailed else county)
            print(f"Scheduling {county}: {reason} local slot at {local.isoformat()}", file=sys.stderr)
    return scheduled


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--county", help="Plan one county, or 'all', for a manual run")
    parser.add_argument("--now", help="UTC ISO timestamp used for deterministic checks")
    args = parser.parse_args()
    config = load_config()

    if args.county == "all":
        jobs = [{"county": county, "schedule_reason": "manual", "scheduled_local_time": ""}
                for county in config["counties"]]
    elif args.county:
        if args.county not in config["counties"]:
            parser.error(f"unknown county: {args.county}")
        jobs = [{"county": args.county, "schedule_reason": "manual", "scheduled_local_time": ""}]
    else:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)
        jobs = due_counties(config, now, load_inventory(), detailed=True)

    print(json.dumps({"include": jobs}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
