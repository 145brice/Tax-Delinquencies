"""Account and order persistence for the storefront.

Primary backend: Appwrite Cloud, configured with APPWRITE_ENDPOINT,
APPWRITE_PROJECT_ID, and APPWRITE_API_KEY. Purchased lead snapshots are stored
in an Appwrite database/collection.

Fallback backend: Postgres, kept for older deployments that still use
DATABASE_URL / POSTGRES_URL.
"""

from __future__ import annotations

import json
import os
import threading
import time
import hashlib
from datetime import datetime, timezone

import requests
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


_DB_URL_ENV_VARS = (
    "DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRES_PRISMA_URL",
    "POSTGRES_URL_NON_POOLING",
)

_init_lock = threading.Lock()
_initialized = False


class DatabaseNotConfigured(RuntimeError):
    """Raised when no account/order backend is configured."""


class AppwriteError(RuntimeError):
    """Raised for Appwrite REST API failures."""


def _env(name, default=""):
    return (os.getenv(name) or default).strip()


def _database_url():
    for name in _DB_URL_ENV_VARS:
        value = _env(name)
        if value:
            return value
    return ""


def appwrite_configured():
    return bool(_env("APPWRITE_ENDPOINT") and _env("APPWRITE_PROJECT_ID") and _env("APPWRITE_API_KEY"))


def _postgres_configured():
    return bool(_database_url())


def is_configured():
    return appwrite_configured() or _postgres_configured()


def _use_appwrite():
    return appwrite_configured()


def _appwrite_endpoint():
    return _env("APPWRITE_ENDPOINT").rstrip("/")


def _appwrite_project_id():
    return _env("APPWRITE_PROJECT_ID")


def _appwrite_api_key():
    return _env("APPWRITE_API_KEY")


def _appwrite_database_id():
    return _env("APPWRITE_DATABASE_ID", "tax_delinquencies")


def _appwrite_orders_collection_id():
    return _env("APPWRITE_ORDERS_COLLECTION_ID", "orders")


def _appwrite_users_collection_id():
    return _env("APPWRITE_USERS_COLLECTION_ID", "users")


def _appwrite_headers(api_key=True, session_secret=""):
    headers = {
        "Content-Type": "application/json",
        "X-Appwrite-Project": _appwrite_project_id(),
    }
    if api_key:
        headers["X-Appwrite-Key"] = _appwrite_api_key()
    if session_secret:
        headers["X-Appwrite-Session"] = session_secret
    return headers


def _appwrite_request(method, path, *, data=None, params=None, api_key=True, session_secret="", ok=(200, 201, 202, 204)):
    if not appwrite_configured():
        raise DatabaseNotConfigured("Appwrite is not configured.")
    url = _appwrite_endpoint() + path
    resp = requests.request(
        method,
        url,
        headers=_appwrite_headers(api_key=api_key, session_secret=session_secret),
        json=data,
        params=params,
        timeout=30,
    )
    if resp.status_code not in ok:
        try:
            body = resp.json()
            message = body.get("message") or body.get("error") or resp.text
        except Exception:
            message = resp.text
        raise AppwriteError(f"{method} {path} failed ({resp.status_code}): {message[:300]}")
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def _safe_doc_id(value):
    out = []
    for ch in str(value or ""):
        if ch.isalnum() or ch in "._-":
            out.append(ch)
        else:
            out.append("_")
    doc_id = "".join(out).strip("._-")
    if not doc_id:
        return "doc"
    if len(doc_id) <= 36:
        return doc_id
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]


def _appwrite_create_database():
    dbid = _appwrite_database_id()
    try:
        _appwrite_request("POST", "/databases", data={
            "databaseId": dbid,
            "name": "Tax Delinquencies",
            "enabled": True,
        })
    except AppwriteError as exc:
        msg = str(exc)
        if "409" in msg:
            return
        if "maximum number of databases" in msg.lower():
            _appwrite_request("GET", f"/databases/{dbid}")
            return
        if "403" not in msg:
            raise
        _appwrite_request("GET", f"/databases/{dbid}")


def _appwrite_create_orders_collection():
    dbid = _appwrite_database_id()
    coll = _appwrite_orders_collection_id()
    try:
        _appwrite_request("POST", f"/databases/{dbid}/collections", data={
            "collectionId": coll,
            "name": "Orders",
            "permissions": [],
            "documentSecurity": False,
            "enabled": True,
        })
    except AppwriteError as exc:
        if "409" not in str(exc):
            raise


def _appwrite_create_users_collection():
    dbid = _appwrite_database_id()
    coll = _appwrite_users_collection_id()
    try:
        _appwrite_request("POST", f"/databases/{dbid}/collections", data={
            "collectionId": coll,
            "name": "Users",
            "permissions": [],
            "documentSecurity": False,
            "enabled": True,
        })
    except AppwriteError as exc:
        if "409" not in str(exc):
            raise


def _appwrite_attribute(collection_id, kind, key, **kwargs):
    dbid = _appwrite_database_id()
    path = f"/databases/{dbid}/collections/{collection_id}/attributes/{kind}"
    payload = {"key": key, "required": kwargs.pop("required", True)}
    payload.update(kwargs)
    try:
        _appwrite_request("POST", path, data=payload)
    except AppwriteError as exc:
        if "409" not in str(exc):
            raise


def _appwrite_wait_for_attributes(collection_id, keys, timeout=20):
    dbid = _appwrite_database_id()
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = _appwrite_request("GET", f"/databases/{dbid}/collections/{collection_id}/attributes")
        attrs = {item.get("key"): item.get("status") for item in data.get("attributes", [])}
        if all(attrs.get(key) == "available" for key in keys):
            return
        time.sleep(0.5)


def _appwrite_init():
    users = _appwrite_users_collection_id()
    orders = _appwrite_orders_collection_id()
    _appwrite_create_database()
    _appwrite_create_users_collection()
    _appwrite_create_orders_collection()

    _appwrite_attribute(users, "string", "email", size=320)
    _appwrite_attribute(users, "string", "password_hash", size=255)
    _appwrite_attribute(users, "string", "created_at", size=64)
    _appwrite_wait_for_attributes(users, ["email", "password_hash", "created_at"])

    _appwrite_attribute(orders, "string", "user_id", size=128)
    _appwrite_attribute(orders, "string", "email", size=320)
    _appwrite_attribute(orders, "string", "stripe_session_id", size=255)
    _appwrite_attribute(orders, "integer", "amount_cents")
    _appwrite_attribute(orders, "string", "status", size=32)
    _appwrite_attribute(orders, "string", "leads_json", size=1000000)
    _appwrite_attribute(orders, "string", "created_at", size=64)
    _appwrite_wait_for_attributes(orders, [
        "user_id", "email", "stripe_session_id", "amount_cents",
        "status", "leads_json", "created_at",
    ])


def get_conn():
    url = _database_url()
    if not url:
        raise DatabaseNotConfigured(
            "Database is not configured. Set Appwrite env vars or DATABASE_URL."
        )
    return psycopg.connect(url, row_factory=dict_row, prepare_threshold=None)


def init_db():
    """Create backing tables/collections if needed. Safe to call repeatedly."""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        if _use_appwrite():
            _appwrite_init()
            _initialized = True
            return
        _postgres_init()
        _initialized = True


def _postgres_init():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                email         TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id                 SERIAL PRIMARY KEY,
                user_id            INTEGER NOT NULL REFERENCES users(id),
                email              TEXT,
                stripe_session_id  TEXT UNIQUE NOT NULL,
                amount_cents       INTEGER NOT NULL DEFAULT 0,
                status             TEXT NOT NULL DEFAULT 'pending',
                leads_json         JSONB NOT NULL DEFAULT '[]'::jsonb,
                created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.commit()


def _normalize_email(email):
    return (email or "").strip().lower()


def create_user(email, password_hash=None, password=None):
    email = _normalize_email(email)
    if _use_appwrite():
        init_db()
        user_id = _safe_doc_id(email)
        try:
            doc = _appwrite_request("POST", f"/databases/{_appwrite_database_id()}/collections/{_appwrite_users_collection_id()}/documents", data={
                "documentId": user_id,
                "data": {
                    "email": email,
                    "password_hash": password_hash or "",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                "permissions": [],
            })
        except AppwriteError as exc:
            if "409" in str(exc) or "already" in str(exc).lower():
                return None
            raise
        return _appwrite_user_doc_to_row(doc)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (email, password_hash)
            VALUES (%s, %s)
            ON CONFLICT (email) DO NOTHING
            RETURNING id, email, password_hash, created_at
            """,
            (email, password_hash),
        )
        row = cur.fetchone()
        conn.commit()
        return row


def create_session(email, password):
    return None


def get_account(session_secret):
    return None


def delete_session(session_secret):
    return


def _appwrite_user_row(user):
    if not user:
        return None
    return {
        "id": user.get("$id") or user.get("userId"),
        "email": user.get("email", ""),
        "password_hash": "",
        "created_at": user.get("$createdAt"),
    }


def _appwrite_user_doc_to_row(doc):
    if not doc:
        return None
    return {
        "id": doc.get("$id"),
        "email": doc.get("email", ""),
        "password_hash": doc.get("password_hash", ""),
        "created_at": doc.get("created_at") or doc.get("$createdAt"),
    }


def get_user_by_email(email):
    email = _normalize_email(email)
    if _use_appwrite():
        return get_user_by_id(_safe_doc_id(email))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, email, password_hash, created_at FROM users WHERE email = %s",
            (email,),
        )
        return cur.fetchone()


def get_user_by_id(user_id):
    if _use_appwrite():
        try:
            doc = _appwrite_request("GET", f"/databases/{_appwrite_database_id()}/collections/{_appwrite_users_collection_id()}/documents/{user_id}")
            return _appwrite_user_doc_to_row(doc)
        except AppwriteError:
            return None
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, email, password_hash, created_at FROM users WHERE id = %s",
            (user_id,),
        )
        return cur.fetchone()


def _order_doc_to_row(doc):
    if not doc:
        return None
    try:
        leads = json.loads(doc.get("leads_json") or "[]")
    except Exception:
        leads = []
    created = doc.get("created_at") or doc.get("$createdAt")
    try:
        created_at = datetime.fromisoformat(str(created).replace("Z", "+00:00")) if created else None
    except ValueError:
        created_at = None
    return {
        "id": doc.get("$id") or doc.get("id"),
        "user_id": doc.get("user_id"),
        "email": doc.get("email"),
        "stripe_session_id": doc.get("stripe_session_id"),
        "amount_cents": int(doc.get("amount_cents") or 0),
        "status": doc.get("status"),
        "leads_json": leads,
        "created_at": created_at,
    }


def _list_order_docs(*queries):
    dbid = _appwrite_database_id()
    coll = _appwrite_orders_collection_id()
    return _appwrite_request("GET", f"/databases/{dbid}/collections/{coll}/documents")


def create_pending_order(user_id, email, stripe_session_id, amount_cents, leads):
    if _use_appwrite():
        init_db()
        doc_id = _safe_doc_id(stripe_session_id)
        now = datetime.now(timezone.utc).isoformat()
        try:
            _appwrite_request("POST", f"/databases/{_appwrite_database_id()}/collections/{_appwrite_orders_collection_id()}/documents", data={
                "documentId": doc_id,
                "data": {
                    "user_id": str(user_id),
                    "email": _normalize_email(email),
                    "stripe_session_id": str(stripe_session_id),
                    "amount_cents": int(amount_cents),
                    "status": "pending",
                    "leads_json": json.dumps(leads, ensure_ascii=False),
                    "created_at": now,
                },
                "permissions": [],
            })
            return doc_id
        except AppwriteError as exc:
            if "409" in str(exc):
                return doc_id
            raise
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders
                (user_id, email, stripe_session_id, amount_cents, status, leads_json)
            VALUES (%s, %s, %s, %s, 'pending', %s)
            ON CONFLICT (stripe_session_id) DO NOTHING
            RETURNING id
            """,
            (user_id, _normalize_email(email), stripe_session_id, amount_cents, Jsonb(leads)),
        )
        row = cur.fetchone()
        conn.commit()
        return row["id"] if row else None


def mark_order_paid(stripe_session_id):
    if _use_appwrite():
        doc_id = _safe_doc_id(stripe_session_id)
        try:
            _appwrite_request("GET", f"/databases/{_appwrite_database_id()}/collections/{_appwrite_orders_collection_id()}/documents/{doc_id}")
        except AppwriteError:
            return False
        _appwrite_request("PATCH", f"/databases/{_appwrite_database_id()}/collections/{_appwrite_orders_collection_id()}/documents/{doc_id}", data={
            "data": {"status": "paid"},
        })
        return True
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE orders SET status = 'paid' WHERE stripe_session_id = %s",
            (stripe_session_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def get_paid_orders_for_user(user_id):
    if _use_appwrite():
        data = _list_order_docs()
        rows = [
            _order_doc_to_row(doc) for doc in data.get("documents", [])
            if str(doc.get("user_id")) == str(user_id) and doc.get("status") == "paid"
        ]
        return sorted(rows, key=lambda row: row.get("created_at") or datetime.min, reverse=True)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, amount_cents, leads_json, created_at
            FROM orders
            WHERE user_id = %s AND status = 'paid'
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        return cur.fetchall()


def get_paid_orders():
    if _use_appwrite():
        data = _list_order_docs()
        rows = [_order_doc_to_row(doc) for doc in data.get("documents", []) if doc.get("status") == "paid"]
        return sorted(rows, key=lambda row: row.get("created_at") or datetime.min, reverse=True)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, user_id, email, amount_cents, leads_json, created_at
            FROM orders
            WHERE status = 'paid'
            ORDER BY created_at DESC
            """
        )
        return cur.fetchall()


def update_order_lead_contacts(order_id, lead_id, contact_fields):
    if _use_appwrite():
        doc = _appwrite_request("GET", f"/databases/{_appwrite_database_id()}/collections/{_appwrite_orders_collection_id()}/documents/{order_id}")
        row = _order_doc_to_row(doc)
        if not row:
            return False
        leads = list(row["leads_json"] or [])
        updated = False
        for lead in leads:
            if str(lead.get("id")) == str(lead_id):
                lead.update(contact_fields)
                updated = True
                break
        if not updated:
            return False
        _appwrite_request("PATCH", f"/databases/{_appwrite_database_id()}/collections/{_appwrite_orders_collection_id()}/documents/{order_id}", data={
            "data": {"leads_json": json.dumps(leads, ensure_ascii=False)},
        })
        return True
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT leads_json FROM orders WHERE id = %s AND status = 'paid'",
            (order_id,),
        )
        row = cur.fetchone()
        if not row:
            return False

        leads = list(row["leads_json"] or [])
        updated = False
        for lead in leads:
            if str(lead.get("id")) == str(lead_id):
                lead.update(contact_fields)
                updated = True
                break
        if not updated:
            return False

        cur.execute(
            "UPDATE orders SET leads_json = %s WHERE id = %s",
            (Jsonb(leads), order_id),
        )
        conn.commit()
        return True


def update_order_lead_tracking(order_id, user_id, lead_id, tracking_fields):
    allowed = {"buyer_folder", "buyer_status", "buyer_notes", "buyer_priority", "buyer_updated_at"}
    updates = {key: value for key, value in tracking_fields.items() if key in allowed}
    if not updates:
        return False

    if _use_appwrite():
        doc = _appwrite_request("GET", f"/databases/{_appwrite_database_id()}/collections/{_appwrite_orders_collection_id()}/documents/{order_id}")
        row = _order_doc_to_row(doc)
        if not row or str(row.get("user_id")) != str(user_id) or row.get("status") != "paid":
            return False
        leads = list(row["leads_json"] or [])
        updated = False
        for lead in leads:
            if str(lead.get("id")) == str(lead_id):
                lead.update(updates)
                updated = True
                break
        if not updated:
            return False
        _appwrite_request("PATCH", f"/databases/{_appwrite_database_id()}/collections/{_appwrite_orders_collection_id()}/documents/{order_id}", data={
            "data": {"leads_json": json.dumps(leads, ensure_ascii=False)},
        })
        return True

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT leads_json FROM orders WHERE id = %s AND user_id = %s AND status = 'paid'",
            (order_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            return False
        leads = list(row["leads_json"] or [])
        updated = False
        for lead in leads:
            if str(lead.get("id")) == str(lead_id):
                lead.update(updates)
                updated = True
                break
        if not updated:
            return False
        cur.execute(
            "UPDATE orders SET leads_json = %s WHERE id = %s AND user_id = %s",
            (Jsonb(leads), order_id, user_id),
        )
        conn.commit()
        return True
