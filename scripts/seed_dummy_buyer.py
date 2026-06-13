from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

import db


DEFAULT_EMAIL = "demo@foreclosureleads.test"
DEFAULT_PASSWORD = "DemoLeads123!"


def sample_leads():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    leads = [
        {
            "id": "demo-barry-001",
            "status": "PRE-FORECLOSURE",
            "county": "barry",
            "state": "MI",
            "city": "Hastings",
            "zip": "49058",
            "address": "214 W State St, Hastings, MI 49058",
            "owner": "Bonny Wagner",
            "primary_phone": "(269) 555-0142",
            "phone_2": "(269) 555-0199",
            "email_1": "bonny.demo@example.com",
            "mailing_address": "PO Box 218, Hastings, MI 49058",
            "parcel_id": "08-55-123-001",
            "case_number": "DEMO-BARRY-001",
            "buyer_folder": "Barry Hot Followups",
            "buyer_status": "Contacted",
            "buyer_priority": "Hot",
            "buyer_notes": "Asked for a call back Friday afternoon. Wants cash offer range first.",
            "buyer_updated_at": now,
        },
        {
            "id": "demo-barry-002",
            "status": "PRE-FORECLOSURE",
            "county": "barry",
            "state": "MI",
            "city": "Delton",
            "zip": "49046",
            "address": "88 Oakwood Dr, Delton, MI 49046",
            "owner": "Diane Behan",
            "primary_phone": "(269) 555-0188",
            "email_1": "diane.demo@example.com",
            "mailing_address": "88 Oakwood Dr, Delton, MI 49046",
            "parcel_id": "08-44-903-022",
            "case_number": "DEMO-BARRY-002",
            "buyer_folder": "Barry Hot Followups",
            "buyer_status": "Follow-up",
            "buyer_priority": "Warm",
            "buyer_notes": "Send postcard and retry phone next week.",
            "buyer_updated_at": now,
        },
        {
            "id": "demo-wayne-001",
            "status": "TAX DELINQUENCY",
            "county": "wayne",
            "state": "MI",
            "city": "Detroit",
            "zip": "48205",
            "address": "15320 Bringard Dr, Detroit, MI 48205",
            "owner": "Marcus Reed",
            "primary_phone": "(313) 555-0114",
            "email_1": "marcus.demo@example.com",
            "mailing_address": "15320 Bringard Dr, Detroit, MI 48205",
            "parcel_id": "W22-880-140",
            "case_number": "DEMO-WAYNE-001",
            "buyer_folder": "Detroit Tax Leads",
            "buyer_status": "New",
            "buyer_priority": "Normal",
            "buyer_notes": "New lead. Check equity before outreach.",
            "buyer_updated_at": now,
        },
        {
            "id": "demo-wayne-002",
            "status": "TAX DELINQUENCY",
            "county": "wayne",
            "state": "MI",
            "city": "Livonia",
            "zip": "48150",
            "address": "9912 Auburndale St, Livonia, MI 48150",
            "owner": "Rachel Miller",
            "primary_phone": "(734) 555-0167",
            "email_1": "rachel.demo@example.com",
            "mailing_address": "9912 Auburndale St, Livonia, MI 48150",
            "parcel_id": "W46-103-765",
            "case_number": "DEMO-WAYNE-002",
            "buyer_folder": "Detroit Tax Leads",
            "buyer_status": "Offer Made",
            "buyer_priority": "Hot",
            "buyer_notes": "Demo offer at $72k. Waiting on seller docs.",
            "buyer_updated_at": now,
        },
        {
            "id": "demo-oakland-001",
            "status": "NOTICE",
            "county": "oakland",
            "state": "MI",
            "city": "Pontiac",
            "zip": "48342",
            "address": "43 Nevada Ave, Pontiac, MI 48342",
            "owner": "Samuel Ortiz",
            "primary_phone": "(248) 555-0131",
            "email_1": "samuel.demo@example.com",
            "mailing_address": "43 Nevada Ave, Pontiac, MI 48342",
            "parcel_id": "OAK-14-22-307-019",
            "case_number": "DEMO-OAKLAND-001",
            "buyer_folder": "Needs Research",
            "buyer_status": "New",
            "buyer_priority": "Cold",
            "buyer_notes": "Verify ownership chain before calling.",
            "buyer_updated_at": now,
        },
    ]
    for lead in leads:
        note = lead.get("buyer_notes", "")
        lead["buyer_purchased_at"] = now
        lead["buyer_note_history"] = [{"at": now, "note": note}] if note else []
        lead["buyer_activity"] = [
            {
                "at": now,
                "label": "Purchased",
                "detail": f"Lead added to {lead.get('buyer_folder') or 'New Leads'}",
            },
            {
                "at": now,
                "label": "Status set",
                "detail": lead.get("buyer_status") or "New",
            },
        ]
    return leads


def ensure_user(email: str, password: str):
    password_hash = generate_password_hash(password)
    user = db.create_user(email, password_hash)
    if user:
        return user

    user = db.get_user_by_email(email)
    if not user:
        raise RuntimeError(f"Could not create or load dummy user {email}")

    if db.appwrite_configured():
        db._appwrite_request(
            "PATCH",
            f"/databases/{db._appwrite_database_id()}/collections/{db._appwrite_users_collection_id()}/documents/{user['id']}",
            data={"data": {"password_hash": password_hash}},
        )
    else:
        with db.get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user["id"]))
            conn.commit()
    return db.get_user_by_email(email)


def upsert_paid_order(user, session_id: str, amount_cents: int, leads):
    order_id = db.create_pending_order(
        user_id=user["id"],
        email=user["email"],
        stripe_session_id=session_id,
        amount_cents=amount_cents,
        leads=leads,
    )
    if db.appwrite_configured():
        db._appwrite_request(
            "PATCH",
            f"/databases/{db._appwrite_database_id()}/collections/{db._appwrite_orders_collection_id()}/documents/{order_id}",
            data={
                "data": {
                    "amount_cents": int(amount_cents),
                    "status": "paid",
                    "leads_json": json.dumps(leads, ensure_ascii=False),
                }
            },
        )
        return order_id

    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE orders SET amount_cents = %s, status = 'paid', leads_json = %s WHERE id = %s",
            (amount_cents, db.Jsonb(leads), order_id),
        )
        conn.commit()
    return order_id


def main():
    parser = argparse.ArgumentParser(description="Seed a dummy buyer account with paid demo leads.")
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    args = parser.parse_args()

    load_dotenv()
    db.init_db()
    user = ensure_user(args.email, args.password)
    leads = sample_leads()
    first_order = upsert_paid_order(user, "demo_paid_order_county_pack_001", 1500, leads[:3])
    second_order = upsert_paid_order(user, "demo_paid_order_followup_pack_002", 1000, leads[3:])
    print(f"Seeded dummy buyer: {args.email}")
    print(f"Password: {args.password}")
    print(f"Orders: {first_order}, {second_order}")
    print(f"Leads: {len(leads)} across 3 folders")


if __name__ == "__main__":
    main()
