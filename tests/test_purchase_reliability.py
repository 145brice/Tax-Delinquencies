import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_fixture import isolated_app
import commerce
import purchase_runtime
import db


class StripeObject(dict):
    __getattr__ = dict.__getitem__


class PurchaseTests(unittest.TestCase):
    def setUp(self):
        self.a = a = isolated_app(self)
        self.user = {"id": "buyer", "email": "buyer@example.com"}
        a.current_user = lambda: self.user
        a._accounts_ready = lambda: True
        a._subscriptions_ready = lambda: False
        a.app.before_request_funcs[None] = []
        self.client = a.app.test_client()
        self.orders = {}
        def create(**kw):
            sid = kw["stripe_session_id"]
            self.orders.setdefault(sid, {**kw, "id": sid, "status": "pending", "leads_json": copy.deepcopy(kw["leads"])})
            return sid
        def paid(sid):
            self.orders[sid]["status"] = "paid"
            return True
        def update(oid, lid, fields):
            lead = next(it for it in self.orders[oid]["leads_json"] if str(it["id"]) == lid)
            lead.update(fields)
            return True
        a.db = SimpleNamespace(init_db=Mock(), is_configured=lambda: True, get_paid_orders=Mock(return_value=[]), create_pending_order=Mock(side_effect=create),
            mark_order_paid=Mock(side_effect=paid), get_order_by_session=lambda sid: self.orders.get(sid),
            get_order_leads=lambda sid: self.orders[sid]["leads_json"],
            get_paid_orders_for_user=lambda uid: [o for o in self.orders.values() if o["user_id"] == uid and o["status"] == "paid"],
            update_order_lead_contacts=Mock(side_effect=update))
        self.sessions = {}
        def checkout(**kw):
            sid = "cs_" + kw["idempotency_key"]
            self.sessions.setdefault(sid, StripeObject(id=sid, url="https://checkout.example/" + sid,
                mode="payment", status="open", payment_status="unpaid", currency="usd",
                amount_total=kw["line_items"][0]["price_data"]["unit_amount"], metadata=kw["metadata"]))
            return self.sessions[sid]
        a.stripe = SimpleNamespace(api_key="test", checkout=SimpleNamespace(Session=SimpleNamespace(
            create=Mock(side_effect=checkout), retrieve=Mock(side_effect=lambda sid: self.sessions[sid]))),
            Webhook=SimpleNamespace(construct_event=Mock()),
            error=SimpleNamespace(SignatureVerificationError=ValueError))
        self.leads = [{"id": str(i), "county": "test", "source": "County source", "address": f"{i} Main Street, City, TN",
                       "owner": "Test Owner", "scraped_date": "2020-01-01"} for i in range(10, 20)]
        a._sqlite_set("listings", self.leads)
        a.purchase_store.credit("buyer", 10000, "initial")

    def test_card_reservation_blocks_second_buyer_and_promo(self):
        first = self.client.post("/api/create-checkout-session", json={"lead_ids": ["10"]})
        self.assertEqual(first.status_code, 200)
        self.user = {"id": "second", "email": "second@example.com"}
        self.assertEqual(self.client.post("/api/create-checkout-session", json={"lead_ids": ["10"]}).status_code, 409)
        self.assertEqual(self.client.post("/api/unlock", json={"lead_ids": ["10"]}).status_code, 409)
        self.a._grant_signup_promo("second")
        self.assertEqual(self.client.post("/api/claim-aged-leads").status_code, 200)
        self.assertTrue(all("10" not in [it["id"] for it in o["leads_json"]] for o in self.orders.values() if o["user_id"] == "second"))

    def test_concurrent_reservations_have_one_winner(self):
        barrier = threading.Barrier(2)
        def buy(uid):
            barrier.wait()
            try:
                self.a.purchase_store.reserve(uid, {"kind": "promo", "user_id": uid, "leads": [{"id": "10"}], "amount_cents": 0})
                return "won"
            except commerce.Unavailable:
                return "lost"
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(buy, ["one", "two"])), ["lost", "won"])

    def test_concurrent_wallet_orders_cannot_overspend(self):
        self.a._sqlite_set("credit_wallets", {"buyer": {"balance_cents": 200, "ledger": []}})
        barrier = threading.Barrier(2)
        def buy(lid):
            barrier.wait()
            try:
                self.a.purchase_store.reserve(lid, {"user_id": "buyer", "leads": [{"id": lid}], "amount_cents": 200}, wallet=True)
                return True
            except commerce.InsufficientCredit:
                return False
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(buy, ["10", "11"])), 1)
        self.assertEqual(self.a._wallet_balance_cents("buyer"), 0)
        self.assertEqual(len(self.a.purchase_store.claimed_ids()), 1)

    def test_failed_wallet_delivery_recovers_without_double_debit(self):
        self.a.db.mark_order_paid.side_effect = [False, True]
        self.assertEqual(self.client.post("/api/unlock", json={"lead_ids": ["10"]}).status_code, 503)
        self.assertEqual(self.a._wallet_balance_cents("buyer"), 9800)
        # A fresh store object models a process restart; recovery needs no browser.
        self.a.purchase_store = commerce.Store(self.a._sqlite_conn)
        job = self.a.purchase_store.take_job()
        self.assertTrue(purchase_runtime.process_job(self.a, job))
        self.assertEqual(self.client.post("/api/unlock", json={"lead_ids": ["10"]}).status_code, 200)
        self.assertEqual(self.a._wallet_balance_cents("buyer"), 9800)
        self.assertEqual(len(self.orders), 1)

    def test_paid_checkout_delivers_once_and_expired_checkout_releases(self):
        self.client.post("/api/create-checkout-session", json={"lead_ids": ["10"]})
        cs = next(iter(self.sessions.values()))
        cs["payment_status"] = "paid"
        self.assertTrue(self.a._fulfill_session(cs.id))
        self.assertTrue(self.a._fulfill_session(cs.id))
        self.assertEqual(len(self.orders), 1)
        self.assertIn("10", self.a.purchase_store.claimed_ids())
        self.client.post("/api/create-checkout-session", json={"lead_ids": ["11"]})
        second = list(self.sessions.values())[-1]
        order = self.a.purchase_store.get(session_id=second.id)
        second["status"] = "expired"
        self.assertTrue(purchase_runtime.process_job(self.a, {"id": order["id"], "kind": "purchase"}))
        self.assertNotIn("11", self.a.purchase_store.claimed_ids())

    def test_webhook_retries_false_and_exception(self):
        os.environ["STRIPE_WEBHOOK_SECRET"] = "test"
        self.a.stripe.Webhook.construct_event.return_value = {"type": "checkout.session.completed", "data": {"object": {"id": "cs_test", "mode": "payment"}}}
        self.a._fulfill_session = Mock(side_effect=[False, RuntimeError("offline"), True])
        for expected in [503, 503, 200]:
            self.assertEqual(self.client.post("/webhook/stripe").status_code, expected)

    def test_credit_pack_is_atomic_and_idempotent(self):
        self.sessions["pack"] = StripeObject(mode="payment", payment_status="paid", currency="usd",
            amount_total=2500, metadata={"kind": "credit_pack", "pack_id": "p25",
            "user_id": "buyer", "credit_cents": "2500"})
        self.assertTrue(self.a._fulfill_credit_pack("pack"))
        self.assertTrue(self.a._fulfill_credit_pack("pack"))
        self.assertEqual(self.a._wallet_balance_cents("buyer"), 12500)
        with self.assertRaises(RuntimeError):
            with self.a.purchase_store.transaction() as conn:
                self.a.purchase_store.wallet_change(conn, "buyer", 100, "broken-pack")
                raise RuntimeError("crash before commit")
        self.assertEqual(self.a.purchase_store.credit("buyer", 100, "broken-pack"), (12600, True))

    def test_credit_pack_rejects_payment_or_credit_mismatch(self):
        valid = {"mode": "payment", "payment_status": "paid", "currency": "usd",
                 "amount_total": 6000, "metadata": {"kind": "credit_pack", "pack_id": "p60",
                 "user_id": "buyer", "credit_cents": "7000"}}
        for key, bad_value in (("amount_total", 5999), ("currency", "eur"), ("mode", "subscription")):
            session = StripeObject(**valid)
            session[key] = bad_value
            self.sessions["bad-" + key] = session
            self.assertFalse(self.a._fulfill_credit_pack("bad-" + key))
        tampered = StripeObject(**valid)
        tampered["metadata"] = {**valid["metadata"], "credit_cents": "15000"}
        self.sessions["bad-credit"] = tampered
        self.assertFalse(self.a._fulfill_credit_pack("bad-credit"))
        self.assertEqual(self.a._wallet_balance_cents("buyer"), 10000)

    def test_trace_job_survives_restart_and_failed_refund_is_once(self):
        self.client.post("/api/unlock", json={"lead_ids": ["10"], "lead_modes": {"10": "skip"}})
        sid = next(iter(self.orders))
        with patch("scrapers.skiptrace_search.lookup", return_value={"phones": [], "emails": []}):
            self.a.purchase_store = commerce.Store(self.a._sqlite_conn)
            job = self.a.purchase_store.take_job()
            self.assertEqual(job["kind"], "trace")
            self.assertTrue(purchase_runtime.process_job(self.a, job))
            self.assertTrue(self.a._fulfill_order_skiptraces(sid))
        self.assertEqual(self.a._wallet_balance_cents("buyer"), 9800)
        self.assertEqual(self.orders[sid]["leads_json"][0]["skiptrace_status"], "failed_credited")

    def test_subscription_uses_allowance_then_only_charges_upgrade(self):
        self.a._subscriptions_ready = lambda: True
        self.a._claimed_counties = lambda **kw: {}
        self.a._sqlite_set("subscriptions", [{"user_id": "buyer", "county": "test", "status": "active", "tier": "starter",
            "stripe_subscription_id": "sub", "period_start": "period", "traces_used": 7}])
        result = self.client.post("/api/unlock", json={"lead_ids": ["10", "11"], "lead_modes": {"10": "skip", "11": "skip"}})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["charged_cents"], 150)
        self.assertEqual(self.a._load_subs()[0]["traces_used"], 8)
        sid = next(iter(self.orders))
        with patch("scrapers.skiptrace_search.lookup", return_value={"phones": [], "emails": []}):
            self.a._fulfill_order_skiptraces(sid)
            self.a._fulfill_order_skiptraces(sid)
        self.assertEqual(self.a._wallet_balance_cents("buyer"), 10000)
        self.assertEqual(self.a._load_subs()[0]["traces_used"], 7)

    def test_subscription_renewal_duplicate_cannot_reset_used_slots(self):
        self.a._sqlite_set("subscriptions", [{"stripe_subscription_id": "sub", "traces_used": 7}])
        invoice = {"id": "inv", "subscription": "sub", "period_start": 100, "billing_reason": "subscription_cycle"}
        self.a._renew_subscription_allowance(invoice)
        self.a._sqlite_set("subscriptions", [{"stripe_subscription_id": "sub", "traces_used": 3, "last_renewal": 100}])
        self.a._renew_subscription_allowance(invoice)
        self.assertEqual(self.a._load_subs()[0]["traces_used"], 3)

    def test_no_contact_reveal_does_not_bill(self):
        self.a._user_active_sub_counties = lambda uid: {"test"}
        before = self.a._wallet_balance_cents("buyer")
        result = self.client.post("/api/subscriber/reveal", json={"lead_id": "10"})
        self.assertTrue(result.json["no_contact"])
        self.assertEqual(self.a._wallet_balance_cents("buyer"), before)
        self.assertEqual(self.a.purchase_store.claimed_ids(), set())

    def test_quote_does_not_spend_or_consume_allowance_and_price_change_rejects(self):
        self.a._subscriptions_ready = lambda: True
        self.a._claimed_counties = lambda **kw: {}
        sub = {"user_id": "buyer", "county": "test", "status": "active", "tier": "starter",
               "stripe_subscription_id": "sub", "traces_used": 7}
        self.a._sqlite_set("subscriptions", [sub])
        payload = {"lead_ids": ["10"], "lead_modes": {"10": "skip"}}
        quote = self.client.post("/api/unlock/quote", json=payload)
        self.assertEqual(quote.json["amount_cents"], 0)
        self.assertEqual(self.a._load_subs()[0]["traces_used"], 7)
        sub["traces_used"] = 8
        self.a._sqlite_set("subscriptions", [sub])
        response = self.client.post("/api/unlock", json={**payload, "max_charge_cents": 0})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.a._wallet_balance_cents("buyer"), 10000)
        self.assertEqual(self.a.purchase_store.claimed_ids(), set())

    def test_existing_paid_inventory_import_blocks_resale(self):
        self.a.db.get_paid_orders.return_value = [{"stripe_session_id": "old", "leads_json": [self.leads[0]]}]
        response = self.client.post("/api/create-checkout-session", json={"lead_ids": ["10"]})
        self.assertEqual(response.status_code, 409)
        self.assertIn("10", self.a.purchase_store.claimed_ids())

    def test_legacy_checkout_conflict_refunds_instead_of_selling_twice(self):
        self.client.post("/api/unlock", json={"lead_ids": ["10"]})
        self.orders["old-checkout"] = {"id": "old-checkout", "status": "pending", "user_id": "other",
            "email": "other@example.com", "amount_cents": 200, "leads_json": [self.leads[0]]}
        self.sessions["old-checkout"] = StripeObject(id="old-checkout", mode="payment", payment_status="paid",
            currency="usd", amount_total=200, payment_intent="pi_old", metadata={})
        self.a.stripe.Refund = SimpleNamespace(create=Mock(return_value={"status": "succeeded"}))
        def status(sid, value):
            self.orders[sid]["status"] = value
            return True
        self.a.db.set_order_status = status
        self.assertTrue(self.a._fulfill_session("old-checkout"))
        self.assertTrue(self.a._fulfill_session("old-checkout"))
        self.assertEqual(self.orders["old-checkout"]["status"], "refunded")
        self.a.stripe.Refund.create.assert_called_once_with(payment_intent="pi_old", idempotency_key="exclusive-refund:old-checkout")

    def test_legacy_pending_promo_remains_retryable(self):
        self.a._grant_signup_promo("buyer")
        with self.a.purchase_store.transaction() as conn:
            conn.execute("UPDATE signup_promos SET payload=? WHERE user_id='buyer'", (json.dumps({"remaining": 2,
                "pending": {"session_id": "aged_old", "leads": [self.leads[0]]}, "reserved_ids": ["10"]}),))
        response = self.client.post("/api/claim-aged-leads")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["remaining"], 2)
        self.assertIn("aged_old", self.orders)

    def test_abandoned_job_lease_is_recovered_and_old_worker_cannot_finish_it(self):
        self.a.purchase_store.add_job("trace:test", "trace")
        first = self.a.purchase_store.take_job()
        self.assertIsNone(self.a.purchase_store.take_job())
        with self.a.purchase_store.transaction() as conn:
            conn.execute("UPDATE fulfillment_jobs SET lease_until=0")
        second = self.a.purchase_store.take_job()
        self.a.purchase_store.finish_job(first["id"], True, first["attempts"] + 1)
        with self.a.purchase_store.transaction() as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM fulfillment_jobs").fetchone())
        self.a.purchase_store.finish_job(second["id"], True, second["attempts"] + 1)

    def test_storefront_renders_with_real_storage_and_masks_contacts(self):
        self.a.current_user = lambda: None
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'10 Main Street', response.data)
        self.assertIn(b'/api/unlock/quote', response.data)


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.a = isolated_app(self)
        self.client = self.a.app.test_client()

    def test_logout_clears_admin_and_token_rotation_revokes_access(self):
        os.environ["ADMIN_TOKEN"] = "admin-test-token"
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "csrf"
        with self.a.app.test_request_context("/?token=admin-test-token"):
            self.assertTrue(self.a.admin_allowed())
            saved = dict(self.a.session)
        with self.client.session_transaction() as sess:
            sess.update(saved)
            sess["admin_allowed"] = True
            sess["skiptrace_admin"] = True
        self.assertEqual(self.client.post("/logout", headers={"X-CSRF-Token": "csrf"}).status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertFalse(sess)
        os.environ["ADMIN_TOKEN"] = "rotated"
        with self.a.app.test_request_context("/"):
            self.a.session.update(saved)
            self.assertFalse(self.a.admin_allowed())

    def test_unverified_admin_email_and_legacy_flags_do_not_grant_role(self):
        self.a.current_user = lambda: {"id": "unverified", "email": "145brice@gmail.com"}
        with self.a.app.test_request_context("/"):
            self.a.session["admin_allowed"] = True
            self.assertFalse(self.a.admin_allowed())
            os.environ["ADMIN_USER_IDS"] = "trusted"
            self.assertFalse(self.a.admin_allowed())
            os.environ["ADMIN_USER_IDS"] = "unverified"
            self.assertTrue(self.a.admin_allowed())

    def test_csv_download_confines_paths(self):
        self.a.admin_allowed = lambda: True
        Path(self.a.DATA_DIR, "allowed.csv").write_text("id\n1\n")
        for filename in ["../.env", "..\\.env", "/etc/passwd", "C:\\Windows\\win.ini", "allowed.csv/../../.env"]:
            self.assertEqual(self.client.get("/admin/data/download", query_string={"file": filename}).status_code, 404)
        with self.client.get("/admin/data/download?file=allowed.csv") as response:
            self.assertEqual(response.status_code, 200)

    def test_storage_must_be_under_volume(self):
        self.a.IS_HOSTED = self.a.IS_RAILWAY = True
        self.assertFalse(self.a._persistent_storage_ready())
        os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = self.a.DATA_DIR
        self.assertTrue(self.a._persistent_storage_ready())
        self.a.SQLITE_DB = self.a.DATA_DIR + "-other/state.sqlite"
        self.assertFalse(self.a._persistent_storage_ready())

    def test_login_redirect_rejects_external_hosts(self):
        self.a._accounts_ready = lambda: True
        self.a.current_user = lambda: None
        self.a.db = SimpleNamespace(init_db=Mock(), get_user_by_email=lambda email: {"id": "buyer", "password_hash": "hash"})
        self.a.check_password_hash = lambda *args: True
        for target in ["//evil.example", "/\\evil.example", "https://evil.example"]:
            with self.client.session_transaction() as sess:
                sess["_csrf_token"] = "csrf"
            response = self.client.post("/login", query_string={"next": target}, data={"email": "user@example.com", "password": "password", "csrf_token": "csrf"})
            self.assertEqual(response.location, "/account")

    def test_postgres_order_leads_fallback(self):
        with patch.object(db, "_use_sqlite", return_value=False), patch.object(db, "_use_appwrite", return_value=False), patch.object(db, "get_order_by_session", return_value={"leads_json": [{"id": "lead"}]}):
            self.assertEqual(db.get_order_leads("session"), [{"id": "lead"}])


if __name__ == "__main__":
    unittest.main()
