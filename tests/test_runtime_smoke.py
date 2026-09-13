import csv
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import unittest
import urllib.request
from unittest.mock import Mock

from app_fixture import isolated_app


class RuntimeTests(unittest.TestCase):
    def test_sqlite_account_can_start_and_fulfill_card_checkout(self):
        a = isolated_app(self)
        a.app.before_request_funcs[None] = []
        client = a.app.test_client()
        registered = client.post("/register", data={"email": "buyer@example.com", "password": "password123"})
        self.assertEqual(registered.status_code, 302)
        lead = {"id": "lead-1", "county": "test", "source": "County source",
                "address": "123 Main Street", "owner": "Test Owner", "scraped_date": "2026-09-12"}
        a._sqlite_set("listings", [lead])

        session = {"id": "cs_sqlite_checkout", "url": "https://checkout.stripe.test/session",
                   "mode": "payment", "status": "complete", "payment_status": "paid",
                   "amount_total": 600, "currency": "usd", "metadata": {}}
        a.stripe.api_key = "sk_test"
        a.stripe.checkout.Session.create = Mock(return_value=type("Checkout", (dict,), {"__getattr__": dict.__getitem__})(session))
        a.stripe.checkout.Session.retrieve = Mock(return_value=session)
        checkout = client.post("/api/create-checkout-session", json={"lead_ids": ["lead-1"]})
        self.assertEqual(checkout.status_code, 200, checkout.get_data(as_text=True))

        order = a.purchase_store.get(session_id="cs_sqlite_checkout")
        session["metadata"] = {"purchase_id": order["id"], "user_id": order["user_id"]}
        import os
        os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
        a.stripe.Webhook.construct_event = Mock(return_value={"id": "evt_sqlite_checkout",
            "type": "checkout.session.completed", "data": {"object": session}})
        fulfilled = client.post("/webhook/stripe")
        self.assertEqual(fulfilled.status_code, 200, fulfilled.get_data(as_text=True))
        self.assertEqual(a.db.get_paid_orders_for_user(order["user_id"])[0]["stripe_session_id"], "cs_sqlite_checkout")

    def test_rendered_storefront_javascript_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed")
        a = isolated_app(self)
        html = a.app.test_client().get("/").get_data(as_text=True)
        scripts = "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S))
        result = subprocess.run([node, "--check"], input=scripts, text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_waitress_serves_health_check(self):
        try:
            from waitress import create_server
        except ImportError:
            self.skipTest("Install requirements.txt to smoke-test Waitress")
        a = isolated_app(self)
        server = create_server(a.app, host="127.0.0.1", port=0, threads=2)
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.effective_port}/healthz", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(json.load(response)["persistent_storage"])
        finally:
            server.task_dispatcher.shutdown()
            server.close()
            worker.join(timeout=2)

    def test_publisher_accepts_raw_and_rejects_masked_export(self):
        a = isolated_app(self)
        scripts = str(Path(__file__).resolve().parents[1] / "scripts")
        sys.path.insert(0, scripts)
        self.addCleanup(sys.path.remove, scripts)
        from publish_csv import read_records
        raw = Path(a.DATA_DIR) / "raw.csv"
        with raw.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["county", "property_address", "owner_name", "source_url", "scraped_date"])
            writer.writeheader()
            writer.writerow({"county": "Test", "property_address": "123 Main Street", "owner_name": "Test Owner",
                             "source_url": "https://example.com", "scraped_date": "2026-09-12"})
        self.assertEqual(len(read_records(raw)), 1)
        raw.write_text("id,address,owner\n1,*** Main Street,T***\n")
        with self.assertRaises(ValueError):
            read_records(raw)


if __name__ == "__main__":
    unittest.main()
