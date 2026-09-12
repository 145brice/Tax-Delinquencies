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

from app_fixture import isolated_app


class RuntimeTests(unittest.TestCase):
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
