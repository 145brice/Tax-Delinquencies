"""Postgres-backed accounts + orders for the storefront.

Vercel's serverless filesystem is ephemeral, so user accounts and purchase
records live in an external Postgres (Neon / Vercel Postgres). Connection
string comes from the DATABASE_URL env var. If it is unset the helpers raise
DatabaseNotConfigured, which the app surfaces as a clear "set DATABASE_URL"
message (mirroring the existing Stripe-not-configured pattern).
"""

import os
import threading

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# Accept whichever connection-string env var is present. Vercel Postgres / Neon
# integrations set POSTGRES_URL (pooled, serverless-friendly); a manual setup may
# use DATABASE_URL. Pooled URLs are preferred for short-lived serverless conns.
_DB_URL_ENV_VARS = (
    "DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRES_PRISMA_URL",
    "POSTGRES_URL_NON_POOLING",
)

_init_lock = threading.Lock()
_initialized = False


class DatabaseNotConfigured(RuntimeError):
    """Raised when no database connection string is set."""


def _database_url():
    for name in _DB_URL_ENV_VARS:
        value = os.getenv(name)
        if value:
            return value
    return ""


def is_configured():
    return bool(_database_url())


def get_conn():
    url = _database_url()
    if not url:
        raise DatabaseNotConfigured(
            "Database is not configured. Set DATABASE_URL (or POSTGRES_URL) and restart the app."
        )
    # One connection per call keeps things simple and serverless-friendly.
    return psycopg.connect(url, row_factory=dict_row)


def init_db():
    """Create tables if they do not exist. Safe to call repeatedly; the real
    work runs once per process (cold start)."""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
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
        _initialized = True


def _normalize_email(email):
    return (email or "").strip().lower()


# ---- Users -----------------------------------------------------------------

def create_user(email, password_hash):
    """Insert a new user. Returns the user row, or None if the email exists."""
    email = _normalize_email(email)
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


def get_user_by_email(email):
    email = _normalize_email(email)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, email, password_hash, created_at FROM users WHERE email = %s",
            (email,),
        )
        return cur.fetchone()


def get_user_by_id(user_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, email, password_hash, created_at FROM users WHERE id = %s",
            (user_id,),
        )
        return cur.fetchone()


# ---- Orders ----------------------------------------------------------------

def create_pending_order(user_id, email, stripe_session_id, amount_cents, leads):
    """Record a not-yet-paid order with a full snapshot of the purchased leads."""
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
    """Flip an order to paid. Idempotent. Returns True if a row now exists paid."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE orders SET status = 'paid' WHERE stripe_session_id = %s",
            (stripe_session_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def get_paid_orders_for_user(user_id):
    """All paid orders for a user, newest first, each with its leads snapshot."""
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
