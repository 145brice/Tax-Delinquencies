import ast
import copy
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask, jsonify, request
from lead_pricing import age_days, discovery_date, normalize_listing_dates, price_cents


class PricingTests(unittest.TestCase):
    def test_storefront_payload_includes_age_days(self):
        tree = ast.parse(Path("app.py").read_text(encoding="utf-8"))
        index = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "index")
        payload_keys = {
            key.value
            for node in ast.walk(index)
            if isinstance(node, ast.Dict)
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        self.assertIn("age_days", payload_keys)

    def test_all_tier_boundaries_and_floors(self):
        today = date(2026, 9, 11)
        expected = {
            ("beta", False): [600, 510, 390, 270, 200, 200],
            ("beta", True): [1500, 1275, 975, 675, 375, 350],
            ("post_beta", False): [1800, 1530, 1170, 810, 450, 300],
            ("post_beta", True): [3000, 2550, 1950, 1350, 750, 500],
        }
        for (phase, traced), amounts in expected.items():
            for tier, days in enumerate(((0, 3), (4, 7), (8, 14), (15, 30), (31, 60), (61, 365))):
                for day in days:
                    item = {"scraped_date": (today - timedelta(days=day)).isoformat()}
                    self.assertEqual(price_cents(item, traced, phase, today), amounts[tier])
            self.assertEqual(amounts, sorted(amounts, reverse=True))

    def test_age_uses_discovery_not_sale_date(self):
        self.assertIsNone(age_days({"scraped_date": "bad", "sale_date": "2000-01-01"}))
        self.assertEqual(price_cents({}), 270)
        self.assertEqual(price_cents({}, traced=True), 675)
        self.assertEqual(price_cents({}, phase="post_beta"), 810)
        self.assertEqual(price_cents({}, traced=True, phase="post_beta"), 1350)
        self.assertEqual(age_days({"scraped_date": "2099-01-01"}), 0)
        self.assertEqual(discovery_date({"first_seen": "2025-01-01", "scraped_date": "2026-01-01"}), date(2025, 1, 1))

    def test_legacy_acquisition_date_is_separated_safely(self):
        legacy = {"date": "2026-05-31", "scraped_date": "", "sale_date": ""}
        self.assertTrue(normalize_listing_dates(legacy))
        self.assertEqual(legacy["scraped_date"], "2026-05-31")
        self.assertEqual(legacy["first_seen"], "2026-05-31")

        event = {"date": "2026-05-31", "scraped_date": "", "sale_date": "2026-05-31"}
        self.assertFalse(normalize_listing_dates(event))
        self.assertNotIn("first_seen", event)


class PromoTests(unittest.TestCase):
    """Exercise real route functions with isolated storage, no startup migration."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        app = Flask(__name__)
        self.ns = {
            "app": app, "json": json, "jsonify": jsonify, "secrets": secrets, "closing": closing,
            "copy": copy, "datetime": datetime, "timezone": timezone,
            "PROMO_LOCK": threading.Lock(), "WALLET_LOCK": threading.Lock(), "age_days": age_days,
            "_accounts_ready": lambda: True, "current_user": lambda: {"id": "new", "email": "test@example.com"},
            "_sqlite_conn": self.connection,
            "_sqlite_enabled": lambda: True,
            "_mark_leads_sold": Mock(),
            "_start_order_skiptraces": Mock(),
            "db": SimpleNamespace(init_db=Mock(), create_pending_order=Mock(return_value="order"), mark_order_paid=Mock(return_value=True)),
            "_is_publishable_listing": lambda item: not item.get("sold_at"),
            "sort_storefront_listings": lambda items: items,
            "price_cents": price_cents, "PRICING_PHASE": "beta",
            "request": request, "os": os,
            "_subscriptions_ready": lambda: False,
            "_user_active_sub_counties": lambda uid: set(),
            "_wallet_balance_cents": lambda uid: 10000,
            "_wallet_adjust": Mock(return_value=9800),
            "stripe": SimpleNamespace(api_key="test", checkout=SimpleNamespace(Session=SimpleNamespace(
                create=Mock(return_value=SimpleNamespace(id="checkout-test", url="https://example.com/checkout"))))),
        }
        names = {"_grant_signup_promo", "_promo_status", "_promo_reserved_ids", "claim_aged_leads",
                 "publishable_storefront_listings", "_lead_is_traced", "_lead_price", "_listing_price_cents",
                 "_purchase_price_cents", "_requested_lead_modes", "_upgrade_listing",
                 "_utc_now_iso", "_default_buyer_folder", "_normalize_buyer_tracking", "_prepare_purchased_leads",
                 "_wallet_credit_once",
                 "create_checkout_session", "unlock_with_credits"}
        tree = ast.parse(Path("app.py").read_text(encoding="utf-8"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.ns["discovery_date"] = discovery_date
        exec(compile(ast.Module(body=functions, type_ignores=[]), "app.py", "exec"), self.ns)
        self.items = [{"id": f"lead-{i}", "scraped_date": "2020-01-01", "county": "test"} for i in range(5)]
        self.ns["current_listings"] = lambda: self.items
        self.client = app.test_client()

    def connection(self):
        conn = sqlite3.connect(str(Path(self.tmp.name) / "test.sqlite"))
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS app_json (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
        return conn

    def grant(self):
        self.ns["_grant_signup_promo"]("new")

    def claim(self):
        return self.client.post("/api/claim-aged-leads")

    def test_three_max_and_no_wallet_credit(self):
        self.grant()
        self.grant()
        self.assertEqual(self.claim().json["unlocked"], 3)
        self.assertEqual(self.claim().json["unlocked"], 0)
        self.assertEqual(self.ns["db"].create_pending_order.call_args.kwargs["amount_cents"], 0)
        self.assertEqual(len(self.ns["publishable_storefront_listings"](self.items)), 2)
        self.assertEqual(self.ns["_listing_price_cents"](self.items[0]), 200)

    def test_partial_inventory_preserves_remaining(self):
        self.grant()
        self.items = self.items[:1]
        self.assertEqual(self.claim().json["remaining"], 2)
        self.assertEqual(self.claim().json["unlocked"], 0)
        self.items.append({"id": "later", "scraped_date": "2020-01-01"})
        self.assertEqual(self.claim().json["remaining"], 1)

    def test_fresh_unknown_and_sold_are_ineligible(self):
        self.grant()
        self.items = [{"id": "fresh", "scraped_date": date.today().isoformat()}, {"id": "unknown"},
                      {"id": "sold", "scraped_date": "2020-01-01", "sold_at": "yes"}]
        self.assertEqual(self.claim().json["unlocked"], 0)
        self.assertEqual(self.ns["_promo_status"]("new")["remaining"], 3)

    def test_retry_uses_same_order_and_reserved_inventory(self):
        self.grant()
        self.ns["db"].mark_order_paid.side_effect = [False, True]
        self.assertEqual(self.claim().status_code, 503)
        first = self.ns["db"].create_pending_order.call_args.kwargs["stripe_session_id"]
        self.assertEqual(self.claim().json["unlocked"], 3)
        self.assertEqual(self.ns["db"].create_pending_order.call_args.kwargs["stripe_session_id"], first)
        self.assertEqual(self.ns["_promo_status"]("new")["remaining"], 0)

    def test_existing_account_and_anonymous_not_granted(self):
        self.assertEqual(self.claim().json["unlocked"], 0)
        self.ns["current_user"] = lambda: None
        self.assertEqual(self.claim().status_code, 401)

    def test_rescrape_preserves_age(self):
        item = {"scraped_date": "2020-01-01"}
        self.ns["_upgrade_listing"](item, {"scraped_date": "2026-09-11"})
        self.assertEqual(item["first_seen"], "2020-01-01")

    def test_checkout_and_wallet_match_display_price(self):
        for phase in ("beta", "post_beta"):
            self.ns["PRICING_PHASE"] = phase
            for mode in ("raw", "skip"):
                expected = price_cents(self.items[0], traced=(mode == "skip"), phase=phase)
                payload = {"lead_ids": ["lead-0"], "lead_modes": {"lead-0": mode}, "price": 1}
                response = self.client.post('/api/create-checkout-session', json=payload)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.ns["stripe"].checkout.Session.create.call_args.kwargs["line_items"][0]["price_data"]["unit_amount"], expected)
                response = self.client.post('/api/unlock', json=payload)
                self.assertEqual(response.json["charged_cents"], expected)

    def test_checkout_defaults_to_raw_and_ignores_unknown_mode(self):
        expected = price_cents(self.items[0], traced=False, phase="beta")
        for lead_modes in ({}, {"lead-0": "expensive-client-value"}):
            response = self.client.post('/api/create-checkout-session', json={
                "lead_ids": ["lead-0"], "lead_modes": lead_modes,
            })
            self.assertEqual(response.status_code, 200)
            charged = self.ns["stripe"].checkout.Session.create.call_args.kwargs["line_items"][0]["price_data"]["unit_amount"]
            self.assertEqual(charged, expected)

    def test_purchase_snapshot_separates_raw_delivery_from_evidence(self):
        lead = {
            "id": "private-contact", "county": "test", "scraped_date": "2020-01-01",
            "owner": "Owner", "address": "123 Main St", "primary_phone": "5551234567",
            "email_1": "owner@example.com",
        }
        raw = self.ns["_prepare_purchased_leads"]([lead], {"private-contact": "raw"})[0]
        self.assertEqual(raw["purchase_mode"], "raw")
        self.assertEqual(raw["primary_phone"], "")
        self.assertEqual(raw["email_1"], "")
        self.assertEqual(raw["_purchase_evidence"]["lead"]["primary_phone"], "5551234567")
        skipped = self.ns["_prepare_purchased_leads"]([lead], {"private-contact": "skip"})[0]
        self.assertEqual(skipped["purchase_mode"], "skip")
        self.assertEqual(skipped["skiptrace_status"], "pending")
        self.assertGreater(skipped["purchase_price_cents"], skipped["raw_price_cents"])

    def test_failed_trace_credit_is_applied_exactly_once(self):
        balance, applied = self.ns["_wallet_credit_once"]("new", 800, "refund:order:lead", "test")
        self.assertTrue(applied)
        self.assertEqual(balance, 800)
        balance, applied = self.ns["_wallet_credit_once"]("new", 800, "refund:order:lead", "test")
        self.assertFalse(applied)
        self.assertEqual(balance, 800)

    def test_promo_reservations_cannot_be_purchased(self):
        self.grant()
        self.claim()
        for endpoint in ('/api/create-checkout-session', '/api/unlock'):
            self.assertEqual(self.client.post(endpoint, json={"lead_ids": ["lead-0"]}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
