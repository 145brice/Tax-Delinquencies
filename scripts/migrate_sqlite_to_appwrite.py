"""Copy Railway SQLite accounts into Appwrite without deleting the source."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db


def create_document(collection: str, document_id: str, data: dict) -> bool:
    path = f"/databases/{db._appwrite_database_id()}/collections/{collection}/documents"
    try:
        db._appwrite_request("POST", path, data={
            "documentId": document_id, "data": data, "permissions": [],
        })
        return True
    except db.AppwriteError as exc:
        if "409" in str(exc):
            return False
        raise


def main() -> int:
    source = Path(db._sqlite_path())
    if not source.is_file():
        raise SystemExit(f"SQLite source does not exist: {source}")
    backup = source.with_name(
        f"{source.stem}-pre-appwrite-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}{source.suffix}"
    )
    with sqlite3.connect(source) as original, sqlite3.connect(backup) as snapshot:
        original.backup(snapshot)
    db._appwrite_init()
    counts = {"users_copied": 0, "users_existing": 0, "orders_copied": 0,
              "orders_existing": 0, "identities_copied": 0, "identities_existing": 0}
    with sqlite3.connect(backup) as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute("SELECT id, email, password_hash, created_at FROM account_users").fetchall()
        orders = conn.execute("SELECT id, user_id, email, stripe_session_id, amount_cents, status, leads_json, created_at FROM account_orders").fetchall()
        identities = conn.execute("SELECT provider, subject, user_id, created_at FROM account_oauth_identities").fetchall()

    for row in users:
        copied = create_document(db._appwrite_users_collection_id(), str(row["id"]), {
            "email": row["email"], "password_hash": row["password_hash"], "created_at": row["created_at"],
        })
        counts["users_copied" if copied else "users_existing"] += 1
    for row in orders:
        copied = create_document(db._appwrite_orders_collection_id(), str(row["id"]), {
            "user_id": str(row["user_id"]), "email": row["email"] or "",
            "stripe_session_id": row["stripe_session_id"], "amount_cents": int(row["amount_cents"]),
            "status": row["status"], "leads_json": row["leads_json"] or "[]", "created_at": row["created_at"],
        })
        counts["orders_copied" if copied else "orders_existing"] += 1
    for row in identities:
        identity_id = db._safe_doc_id(f'{row["provider"]}:{row["subject"]}')
        copied = create_document(db._appwrite_oauth_collection_id(), identity_id, {
            "provider": row["provider"], "subject": row["subject"],
            "user_id": str(row["user_id"]), "created_at": row["created_at"],
        })
        counts["identities_copied" if copied else "identities_existing"] += 1
    counts["backup"] = str(backup)
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
