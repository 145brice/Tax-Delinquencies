"""Paired, no-write skip-trace benchmark.

Runs the same balanced lead sample through two engines with caches disabled and
writes both a detailed JSON artifact and a review-friendly CSV. Production lead
records are never modified.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from scrapers import skiptrace_search as search


def _norm_phone(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def _norm_email(value: str) -> str:
    return str(value or "").strip().lower()


def _candidate(row: dict) -> bool:
    return bool(str(row.get("id") or "").strip()
                and str(row.get("owner") or "").strip()
                and str(row.get("address") or row.get("street") or "").strip())


def _balanced_sample(rows: list[dict], limit: int, seed: int) -> list[dict]:
    """Round-robin counties so one large market cannot dominate the result."""
    rng = random.Random(seed)
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if _candidate(row):
            groups[str(row.get("county") or "unknown").strip().lower()].append(row)
    queues = []
    for county in sorted(groups):
        rng.shuffle(groups[county])
        queues.append(deque(groups[county]))
    rng.shuffle(queues)
    picked = []
    while queues and len(picked) < limit:
        active = []
        for queue in queues:
            if queue and len(picked) < limit:
                picked.append(queue.popleft())
            if queue:
                active.append(queue)
        queues = active
    return picked


def _location(lead: dict) -> tuple[str, str]:
    city = str(lead.get("city") or "").strip()
    state = str(lead.get("state") or "").strip().upper()
    return city, state


def _run(session, lead: dict) -> dict:
    city, state = _location(lead)
    started = time.perf_counter()
    try:
        result = session.lookup(
            str(lead.get("owner") or ""),
            str(lead.get("street") or lead.get("address") or ""),
            city,
            state,
            use_cache=False,
        )
        error = ""
    except Exception as exc:
        result = {}
        error = f"{type(exc).__name__}: {exc}"
    return {
        "seconds": round(time.perf_counter() - started, 3),
        "phones": list(result.get("phones") or []),
        "phone_details": list(result.get("phone_details") or []),
        "emails": list(result.get("emails") or []),
        "query": str(result.get("query") or ""),
        "engine": str(result.get("engine") or ""),
        "error": error or str(result.get("error") or ""),
    }


def _overlap(left: dict, right: dict, field: str, normalizer) -> list[str]:
    a = {normalizer(value) for value in left.get(field, []) if normalizer(value)}
    b = {normalizer(value) for value in right.get(field, []) if normalizer(value)}
    return sorted(a & b)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two skip-trace engines on identical leads.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260911)
    parser.add_argument("--left", choices=["ddg", "google", "combo", "serper"], default="ddg")
    parser.add_argument("--right", choices=["ddg", "google", "combo", "serper"], default="serper")
    parser.add_argument("--input", default=str(ROOT / "listings.json"))
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "skiptrace_benchmarks"))
    args = parser.parse_args()
    if args.left == args.right:
        parser.error("--left and --right must be different engines")

    load_dotenv(ROOT / ".env")
    rows = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
    sample = _balanced_sample(rows, max(1, args.limit), args.seed)
    if not sample:
        raise SystemExit("No leads with an owner and address were available.")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    details = []

    with search.get_session(args.left, headless=True) as left_session, \
            search.get_session(args.right, headless=True) as right_session:
        for number, lead in enumerate(sample, 1):
            print(f"[{number}/{len(sample)}] {lead.get('county', '')}: {lead.get('owner', '')}", flush=True)
            left = _run(left_session, lead)
            right = _run(right_session, lead)
            phone_overlap = _overlap(left, right, "phones", _norm_phone)
            email_overlap = _overlap(left, right, "emails", _norm_email)
            details.append({
                "lead": {key: lead.get(key, "") for key in (
                    "id", "owner", "address", "street", "city", "state", "county",
                    "parcel_id", "case_number", "source", "link")},
                args.left: left,
                args.right: right,
                "phone_overlap": phone_overlap,
                "email_overlap": email_overlap,
                "review_required": bool((left["phones"] or left["emails"] or right["phones"] or right["emails"])
                                        and not (phone_overlap or email_overlap)),
            })

    def count(engine: str, field: str) -> int:
        return sum(bool(item[engine][field]) for item in details)

    summary = {
        "run_id": run_id,
        "sample_size": len(details),
        "seed": args.seed,
        "left_engine": args.left,
        "right_engine": args.right,
        args.left: {
            "phone_hits": count(args.left, "phones"), "email_hits": count(args.left, "emails"),
            "errors": count(args.left, "error"),
            "total_seconds": round(sum(item[args.left]["seconds"] for item in details), 3),
        },
        args.right: {
            "phone_hits": count(args.right, "phones"), "email_hits": count(args.right, "emails"),
            "errors": count(args.right, "error"),
            "total_seconds": round(sum(item[args.right]["seconds"] for item in details), 3),
        },
        "phone_agreements": sum(bool(item["phone_overlap"]) for item in details),
        "email_agreements": sum(bool(item["email_overlap"]) for item in details),
        "manual_review": sum(item["review_required"] for item in details),
        "production_records_modified": False,
    }
    json_path = output_dir / f"{run_id}_{args.left}_vs_{args.right}.json"
    csv_path = output_dir / f"{run_id}_{args.left}_vs_{args.right}.csv"
    json_path.write_text(json.dumps({"summary": summary, "results": details}, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fields = ["lead_id", "county", "owner", "address", "left_phones", "left_emails",
                  "right_phones", "right_emails", "phone_overlap", "email_overlap",
                  "left_seconds", "right_seconds", "left_error", "right_error", "review_required"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in details:
            writer.writerow({
                "lead_id": item["lead"]["id"], "county": item["lead"]["county"],
                "owner": item["lead"]["owner"], "address": item["lead"]["address"],
                "left_phones": "; ".join(item[args.left]["phones"]),
                "left_emails": "; ".join(item[args.left]["emails"]),
                "right_phones": "; ".join(item[args.right]["phones"]),
                "right_emails": "; ".join(item[args.right]["emails"]),
                "phone_overlap": "; ".join(item["phone_overlap"]),
                "email_overlap": "; ".join(item["email_overlap"]),
                "left_seconds": item[args.left]["seconds"], "right_seconds": item[args.right]["seconds"],
                "left_error": item[args.left]["error"], "right_error": item[args.right]["error"],
                "review_required": item["review_required"],
            })
    print(json.dumps(summary, indent=2))
    print(f"Detailed JSON: {json_path}\nReview CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
