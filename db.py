"""Account and order persistence for the storefront.

Railway deployments default to SQLite on the mounted persistent volume. Appwrite
and Postgres remain available when ACCOUNT_BACKEND explicitly selects them.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import hashlib
from contextlib import closing
from functools import wraps
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
_initialized_backend = ""
_lead_update_lock = threading.RLock()


def _serialize_lead_update(view):
    """Appwrite snapshots need serialization in the single Railway process."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        with _lead_update_lock:
            return view(*args, **kwargs)
    return wrapped


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


def _sqlite_path():
    explicit = _env("SQLITE_DB")
    if explicit:
        return os.path.abspath(explicit)
    volume = _env("RAILWAY_VOLUME_MOUNT_PATH")
    return os.path.abspath(os.path.join(volume, "foreclosure.sqlite3")) if volume else ""


def _use_sqlite():
    selected = _env("ACCOUNT_BACKEND").lower()
    if selected:
        return selected == "sqlite"
    return bool(_env("RAILWAY_VOLUME_MOUNT_PATH") or _env("SQLITE_DB"))


def is_configured():
    return (_use_sqlite() and bool(_sqlite_path())) or appwrite_configured() or _postgres_configured()


def backend_name():
    if _use_sqlite() and _sqlite_path():
        return "sqlite"
    if _use_appwrite():
        return "appwrite"
    if _postgres_configured():
        return "postgres"
    return None


def _use_appwrite():
    selected = _env("ACCOUNT_BACKEND").lower()
    return not _use_sqlite() and appwrite_configured() and selected != "postgres"


def _sqlite_conn():
    path = _sqlite_path()
    if not path:
        raise DatabaseNotConfigured("SQLite account storage is not configured.")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


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


def _appwrite_oauth_collection_id():
    return _env("APPWRITE_OAUTH_COLLECTION_ID", "oauth_identities")


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


def _appwrite_create_oauth_collection():
    dbid = _appwrite_database_id()
    coll = _appwrite_oauth_collection_id()
    try:
        _appwrite_request("POST", f"/databases/{dbid}/collections", data={
            "collectionId": coll, "name": "OAuth Identities", "permissions": [],
            "documentSecurity": False, "enabled": True,
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
    oauth = _appwrite_oauth_collection_id()
    _appwrite_create_database()
    _appwrite_create_users_collection()
    _appwrite_create_orders_collection()
    _appwrite_create_oauth_collection()

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

    _appwrite_attribute(oauth, "string", "provider", size=40)
    _appwrite_attribute(oauth, "string", "subject", size=255)
    _appwrite_attribute(oauth, "string", "user_id", size=128)
    _appwrite_attribute(oauth, "string", "created_at", size=64)
    _appwrite_wait_for_attributes(oauth, ["provider", "subject", "user_id", "created_at"])


def get_conn():
    url = _database_url()
    if not url:
        raise DatabaseNotConfigured(
            "Database is not configured. Set Appwrite env vars or DATABASE_URL."
        )
    return psycopg.connect(url, row_factory=dict_row, prepare_threshold=None)


def init_db():
    """Create backing tables/collections if needed. Safe to call repeatedly."""
    global _initialized, _initialized_backend
    backend = "sqlite:" + _sqlite_path() if _use_sqlite() else ("appwrite" if _use_appwrite() else "postgres")
    if _initialized and _initialized_backend == backend:
        return
    with _init_lock:
        if _initialized and _initialized_backend == backend:
            return
        if _use_sqlite():
            _sqlite_init()
        elif _use_appwrite():
            _appwrite_init()
        else:
            _postgres_init()
        _initialized = True
        _initialized_backend = backend


def _sqlite_init():
    with closing(_sqlite_conn()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_orders (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                email TEXT,
                stripe_session_id TEXT UNIQUE NOT NULL,
                amount_cents INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                leads_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES account_users(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_oauth_identities (
                provider TEXT NOT NULL,
                subject TEXT NOT NULL,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (provider, subject),
                UNIQUE (provider, user_id),
                FOREIGN KEY (user_id) REFERENCES account_users(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_account_orders_user_status ON account_orders(user_id, status)")


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
    if _use_sqlite():
        init_db()
        user_id = _safe_doc_id(email)
        created_at = datetime.now(timezone.utc).isoformat()
        with closing(_sqlite_conn()) as conn, conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO account_users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user_id, email, password_hash or "", created_at),
            )
            if not cur.rowcount:
                return None
        return {"id": user_id, "email": email, "password_hash": password_hash or "", "created_at": created_at}
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
    if _use_sqlite():
        init_db()
        with closing(_sqlite_conn()) as conn, conn:
            row = conn.execute(
                "SELECT id, email, password_hash, created_at FROM account_users WHERE email = ?", (email,)
            ).fetchone()
        return dict(row) if row else None
    if _use_appwrite():
        return get_user_by_id(_safe_doc_id(email))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, email, password_hash, created_at FROM users WHERE email = %s",
            (email,),
        )
        return cur.fetchone()


def get_user_by_id(user_id):
    if _use_sqlite():
        init_db()
        with closing(_sqlite_conn()) as conn, conn:
            row = conn.execute(
                "SELECT id, email, password_hash, created_at FROM account_users WHERE id = ?", (str(user_id),)
            ).fetchone()
        return dict(row) if row else None
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


def get_or_create_oauth_user(provider, subject, email):
    """Resolve an OAuth identity by immutable provider subject, linking only a
    provider-verified email to an existing account."""
    provider = str(provider or "").strip().lower()
    subject = str(subject or "").strip()
    email = _normalize_email(email)
    if not provider or not subject or not email:
        raise ValueError("OAuth identity is incomplete.")
    init_db()
    if _use_appwrite():
        coll = _appwrite_oauth_collection_id()
        dbid = _appwrite_database_id()
        identity_id = _safe_doc_id(f"{provider}:{subject}")
        try:
            identity = _appwrite_request(
                "GET", f"/databases/{dbid}/collections/{coll}/documents/{identity_id}"
            )
            user = get_user_by_id(identity.get("user_id"))
            if not user:
                raise AppwriteError("OAuth identity points to a missing user")
            return user, False
        except AppwriteError as exc:
            if "404" not in str(exc):
                raise

        user = get_user_by_email(email)
        created = False
        if not user:
            user = create_user(email, "")
            created = True
        identities = _appwrite_request(
            "GET", f"/databases/{dbid}/collections/{coll}/documents",
            params=[("queries[]", json.dumps({"method": "limit", "values": [100]}))],
        ).get("documents", [])
        linked = next((doc for doc in identities
                       if doc.get("provider") == provider and str(doc.get("user_id")) == str(user["id"])), None)
        if linked and linked.get("subject") != subject:
            raise ValueError("This account is already linked to another Google identity.")
        try:
            _appwrite_request(
                "POST", f"/databases/{dbid}/collections/{coll}/documents",
                data={"documentId": identity_id, "data": {
                    "provider": provider, "subject": subject, "user_id": str(user["id"]),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }, "permissions": []},
            )
        except AppwriteError as exc:
            if "409" not in str(exc):
                raise
        return user, created

    if not _use_sqlite():
        raise DatabaseNotConfigured("OAuth identities require SQLite or Appwrite.")
    with closing(_sqlite_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT u.id, u.email, u.password_hash, u.created_at
               FROM account_oauth_identities i
               JOIN account_users u ON u.id = i.user_id
               WHERE i.provider = ? AND i.subject = ?""",
            (provider, subject),
        ).fetchone()
        if row:
            return dict(row), False

        row = conn.execute(
            "SELECT id, email, password_hash, created_at FROM account_users WHERE email = ?", (email,)
        ).fetchone()
        created = False
        if row:
            user = dict(row)
        else:
            created_at = datetime.now(timezone.utc).isoformat()
            user = {"id": _safe_doc_id(email), "email": email, "password_hash": "", "created_at": created_at}
            conn.execute(
                "INSERT INTO account_users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user["id"], email, "", created_at),
            )
            created = True
        try:
            conn.execute(
                "INSERT INTO account_oauth_identities (provider, subject, user_id, created_at) VALUES (?, ?, ?, ?)",
                (provider, subject, user["id"], datetime.now(timezone.utc).isoformat()),
            )
        except sqlite3.IntegrityError:
            # One local account can have one identity per provider. If this
            # races another callback, accept only the same immutable subject.
            linked = conn.execute(
                "SELECT subject FROM account_oauth_identities WHERE provider = ? AND user_id = ?",
                (provider, user["id"]),
            ).fetchone()
            if not linked or linked["subject"] != subject:
                raise ValueError("This account is already linked to another Google identity.")
        return user, created


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


def _sqlite_order_to_row(row):
    if not row:
        return None
    result = dict(row)
    try:
        result["leads_json"] = json.loads(result.get("leads_json") or "[]")
    except (TypeError, ValueError):
        result["leads_json"] = []
    created = result.get("created_at")
    try:
        result["created_at"] = datetime.fromisoformat(str(created).replace("Z", "+00:00")) if created else None
    except ValueError:
        result["created_at"] = None
    return result


def _list_order_docs(*queries):
    dbid = _appwrite_database_id()
    coll = _appwrite_orders_collection_id()
    # Appwrite returns only 25 documents by default. Walk every page so buyer
    # history and the staff queue do not silently lose older purchases.
    documents = []
    offset = 0
    page_size = 100
    while True:
        page_queries = list(queries) + [
            json.dumps({"method": "limit", "values": [page_size]}),
            json.dumps({"method": "offset", "values": [offset]}),
        ]
        page = _appwrite_request(
            "GET",
            f"/databases/{dbid}/collections/{coll}/documents",
            params=[("queries[]", query) for query in page_queries],
        )
        batch = page.get("documents", [])
        documents.extend(batch)
        offset += len(batch)
        total = int(page.get("total") or 0)
        if not batch or len(batch) < page_size or (total and offset >= total):
            return {"total": total or len(documents), "documents": documents}


def create_pending_order(user_id, email, stripe_session_id, amount_cents, leads):
    if _use_sqlite():
        init_db()
        order_id = _safe_doc_id(stripe_session_id)
        with closing(_sqlite_conn()) as conn, conn:
            conn.execute(
                """INSERT OR IGNORE INTO account_orders
                   (id, user_id, email, stripe_session_id, amount_cents, status, leads_json, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (order_id, str(user_id), _normalize_email(email), str(stripe_session_id), int(amount_cents),
                 json.dumps(leads, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )
            row = conn.execute("SELECT id FROM account_orders WHERE stripe_session_id = ?", (str(stripe_session_id),)).fetchone()
        return row["id"] if row else None
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
        if row:
            return row["id"]
        cur.execute("SELECT id FROM orders WHERE stripe_session_id = %s", (stripe_session_id,))
        existing = cur.fetchone()
        return existing["id"] if existing else None


def mark_order_paid(stripe_session_id):
    return set_order_status(stripe_session_id, "paid")


def set_order_status(stripe_session_id, status):
    if status not in {"paid", "refunded"}:
        raise ValueError("Unsupported order status")
    if _use_sqlite():
        init_db()
        with closing(_sqlite_conn()) as conn, conn:
            cur = conn.execute(
                "UPDATE account_orders SET status = ? WHERE stripe_session_id = ?",
                (status, str(stripe_session_id)),
            )
        return cur.rowcount > 0
    if _use_appwrite():
        doc_id = _safe_doc_id(stripe_session_id)
        try:
            _appwrite_request("GET", f"/databases/{_appwrite_database_id()}/collections/{_appwrite_orders_collection_id()}/documents/{doc_id}")
        except AppwriteError:
            return False
        _appwrite_request("PATCH", f"/databases/{_appwrite_database_id()}/collections/{_appwrite_orders_collection_id()}/documents/{doc_id}", data={
            "data": {"status": status},
        })
        return True
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE orders SET status = %s WHERE stripe_session_id = %s",
            (status, stripe_session_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_order_leads(stripe_session_id):
    """Leads stored on one order, looked up by its Stripe session id.
    Used at fulfillment time to mark the purchased leads sold."""
    if _use_appwrite():
        doc_id = _safe_doc_id(stripe_session_id)
        try:
            doc = _appwrite_request("GET", f"/databases/{_appwrite_database_id()}/collections/{_appwrite_orders_collection_id()}/documents/{doc_id}")
        except AppwriteError:
            return []
        row = _order_doc_to_row(doc)
        return list(row["leads_json"] or []) if row else []
    order = get_order_by_session(stripe_session_id)
    return list(order["leads_json"] or []) if order else []


def get_order_by_session(stripe_session_id):
    """Return the complete order row for fulfillment work."""
    if _use_sqlite():
        init_db()
        with closing(_sqlite_conn()) as conn, conn:
            row = conn.execute(
                """SELECT id, user_id, email, stripe_session_id, amount_cents,
                          status, leads_json, created_at
                   FROM account_orders WHERE stripe_session_id = ?""",
                (str(stripe_session_id),),
            ).fetchone()
        return _sqlite_order_to_row(row)
    if _use_appwrite():
        doc_id = _safe_doc_id(stripe_session_id)
        try:
            doc = _appwrite_request(
                "GET",
                f"/databases/{_appwrite_database_id()}/collections/{_appwrite_orders_collection_id()}/documents/{doc_id}",
            )
        except AppwriteError:
            return None
        return _order_doc_to_row(doc)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, user_id, email, stripe_session_id, amount_cents,
                   status, leads_json, created_at
            FROM orders WHERE stripe_session_id = %s
            """,
            (stripe_session_id,),
        )
        return cur.fetchone()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT leads_json FROM orders WHERE stripe_session_id = %s",
            (stripe_session_id,),
        )
        row = cur.fetchone()
        return list(row["leads_json"] or []) if row else []


def get_paid_orders_for_user(user_id):
    if _use_sqlite():
        init_db()
        with closing(_sqlite_conn()) as conn, conn:
            rows = conn.execute(
                """SELECT id, user_id, email, stripe_session_id, amount_cents, status, leads_json, created_at
                   FROM account_orders WHERE user_id = ? AND status = 'paid' ORDER BY created_at DESC""",
                (str(user_id),),
            ).fetchall()
        return [_sqlite_order_to_row(row) for row in rows]
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
            SELECT id, stripe_session_id, amount_cents, leads_json, created_at
            FROM orders
            WHERE user_id = %s AND status = 'paid'
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        return cur.fetchall()


def get_paid_orders():
    if _use_sqlite():
        init_db()
        with closing(_sqlite_conn()) as conn, conn:
            rows = conn.execute(
                """SELECT id, user_id, email, stripe_session_id, amount_cents, status, leads_json, created_at
                   FROM account_orders WHERE status = 'paid' ORDER BY created_at DESC"""
            ).fetchall()
        return [_sqlite_order_to_row(row) for row in rows]
    if _use_appwrite():
        data = _list_order_docs()
        rows = [_order_doc_to_row(doc) for doc in data.get("documents", []) if doc.get("status") == "paid"]
        return sorted(rows, key=lambda row: row.get("created_at") or datetime.min, reverse=True)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, user_id, email, stripe_session_id, amount_cents, leads_json, created_at
            FROM orders
            WHERE status = 'paid'
            ORDER BY created_at DESC
            """
        )
        return cur.fetchall()


@_serialize_lead_update
def update_order_lead_contacts(order_id, lead_id, contact_fields):
    if _use_sqlite():
        init_db()
        with closing(_sqlite_conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT leads_json FROM account_orders WHERE id = ? AND status = 'paid'", (str(order_id),)
            ).fetchone()
            if not row:
                return False
            try:
                leads = json.loads(row["leads_json"] or "[]")
            except (TypeError, ValueError):
                leads = []
            lead = next((item for item in leads if str(item.get("id")) == str(lead_id)), None)
            if not lead:
                return False
            lead.update(contact_fields)
            conn.execute("UPDATE account_orders SET leads_json = ? WHERE id = ?",
                         (json.dumps(leads, ensure_ascii=False), str(order_id)))
        return True
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
            "SELECT leads_json FROM orders WHERE id = %s AND status = 'paid' FOR UPDATE",
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


@_serialize_lead_update
def update_order_lead_tracking(order_id, user_id, lead_id, tracking_fields):
    allowed = {
        "buyer_folder", "buyer_status", "buyer_notes", "buyer_priority",
        "buyer_updated_at", "buyer_purchased_at", "buyer_note_history", "buyer_activity",
    }
    updates = {key: value for key, value in tracking_fields.items() if key in allowed}
    if not updates:
        return False

    if _use_sqlite():
        init_db()
        with closing(_sqlite_conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT leads_json FROM account_orders WHERE id = ? AND user_id = ? AND status = 'paid'",
                (str(order_id), str(user_id)),
            ).fetchone()
            if not row:
                return False
            try:
                leads = json.loads(row["leads_json"] or "[]")
            except (TypeError, ValueError):
                leads = []
            lead = next((item for item in leads if str(item.get("id")) == str(lead_id)), None)
            if not lead:
                return False
            _apply_tracking_history(lead, updates)
            lead.update(updates)
            conn.execute("UPDATE account_orders SET leads_json = ? WHERE id = ? AND user_id = ?",
                         (json.dumps(leads, ensure_ascii=False), str(order_id), str(user_id)))
        return True

    if _use_appwrite():
        doc = _appwrite_request("GET", f"/databases/{_appwrite_database_id()}/collections/{_appwrite_orders_collection_id()}/documents/{order_id}")
        row = _order_doc_to_row(doc)
        if not row or str(row.get("user_id")) != str(user_id) or row.get("status") != "paid":
            return False
        leads = list(row["leads_json"] or [])
        updated = False
        for lead in leads:
            if str(lead.get("id")) == str(lead_id):
                _apply_tracking_history(lead, updates)
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
            "SELECT leads_json FROM orders WHERE id = %s AND user_id = %s AND status = 'paid' FOR UPDATE",
            (order_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            return False
        leads = list(row["leads_json"] or [])
        updated = False
        for lead in leads:
            if str(lead.get("id")) == str(lead_id):
                _apply_tracking_history(lead, updates)
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


def _tracking_now(updates):
    return updates.get("buyer_updated_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tracking_label(key):
    return {
        "buyer_folder": "Folder changed",
        "buyer_status": "Status changed",
        "buyer_priority": "Priority changed",
        "buyer_notes": "Note updated",
    }.get(key, "Lead updated")


def _apply_tracking_history(lead, updates):
    when = _tracking_now(updates)
    activity = lead.get("buyer_activity")
    if not isinstance(activity, list):
        purchased = lead.get("buyer_purchased_at") or lead.get("buyer_updated_at") or when
        activity = [{
            "at": purchased,
            "label": "Purchased",
            "detail": f"Lead added to {lead.get('buyer_folder') or 'New Leads'}",
        }]
    note_history = lead.get("buyer_note_history")
    if not isinstance(note_history, list):
        existing_note = str(lead.get("buyer_notes") or "").strip()
        note_history = ([{"at": lead.get("buyer_updated_at") or when, "note": existing_note}] if existing_note else [])

    for key in ("buyer_folder", "buyer_status", "buyer_priority"):
        if key not in updates:
            continue
        old_value = str(lead.get(key) or "").strip()
        new_value = str(updates.get(key) or "").strip()
        if old_value != new_value:
            activity.append({
                "at": when,
                "label": _tracking_label(key),
                "detail": f"{old_value or '—'} → {new_value or '—'}",
            })

    if "buyer_notes" in updates:
        old_note = str(lead.get("buyer_notes") or "").strip()
        new_note = str(updates.get("buyer_notes") or "").strip()
        if old_note != new_note:
            activity.append({
                "at": when,
                "label": "Note updated",
                "detail": new_note[:180] if new_note else "Note cleared",
            })
            if new_note:
                note_history.append({"at": when, "note": new_note})

    updates["buyer_activity"] = activity[-50:]
    updates["buyer_note_history"] = note_history[-25:]
