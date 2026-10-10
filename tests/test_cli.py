import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shipment_monitor.models import Shipment
from shipment_monitor.store import Store

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "shipments.sample.json"
FIXTURES = ROOT / "examples" / "fixtures"


class CliTests(unittest.TestCase):
    def test_preview_does_not_write_or_email_and_blocks_shipped_after_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "shipments.json"
            store.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
            before = store.read_text(encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "shipment_monitor",
                    "check",
                    "--store",
                    str(store),
                    "--fixtures",
                    str(FIXTURES),
                    "--now",
                    "2026-10-09T16:00:00+00:00",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(store.read_text(encoding="utf-8"), before)
            events = [
                json.loads(line)["event"]
                for line in completed.stdout.splitlines()
                if line.startswith("{") and json.loads(line)["action"] == "would_send"
            ]
            self.assertIn("delivered", events)
            self.assertNotIn("shipped", events)
            reasons = [json.loads(line)["reason"] for line in completed.stdout.splitlines() if line.startswith("{")]
            self.assertNotIn("delivered_line_guard", reasons)

    def test_execute_requires_an_explicit_opt_in(self):
        completed = subprocess.run(
            [sys.executable, "-m", "shipment_monitor", "check", "--store", str(SAMPLE), "--execute"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            env={key: value for key, value in os.environ.items() if key != "SHIPMENT_ALLOW_EMAIL"},
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Refusing to email", completed.stderr)

    def test_commit_execute_calls_the_existing_sender_interface(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = tmp_path / "shipments.json"
            store.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
            log = tmp_path / "sender.log"
            sender = tmp_path / "send-shipment-updates.py"
            sender.write_text(
                "import os, sys\nfrom pathlib import Path\nPath(os.environ['STUB_LOG']).open('a').write('\\n'.join(sys.argv) + '\\n---\\n')\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["SHIPMENT_ALLOW_EMAIL"] = "1"
            env["STUB_LOG"] = str(log)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "shipment_monitor",
                    "check",
                    "--store",
                    str(store),
                    "--fixtures",
                    str(FIXTURES),
                    "--now",
                    "2026-10-09T16:00:00+00:00",
                    "--commit",
                    "--execute",
                    "--sender",
                    str(sender),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            sent = log.read_text(encoding="utf-8")
            self.assertIn("\ndelivered\n", sent)
            self.assertNotIn("\nshipped\n", sent)
            self.assertIn("--customer-email", sent)
            saved = json.loads(store.read_text(encoding="utf-8"))
            delivered = next(item for item in saved["shipments"] if item["shipment_id"] == "north-pier-1")
            self.assertEqual(delivered["status"], "delivered")
            self.assertTrue(delivered["ever_delivered"])
            self.assertIn("delivered", delivered["notified_events"])
            self.assertTrue(any(entry["event"] == "delivered" for entry in saved["email_log"]))
            closed = next(item for item in saved["shipments"] if item["shipment_id"] == "closed-1")
            self.assertEqual(closed["notified_events"], ["shipped", "delivered"])

    def test_briefing_command(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "shipment_monitor",
                "briefing",
                "--store",
                str(SAMPLE),
                "--slot",
                "noon",
                "--now",
                "2026-10-09T16:00:00+00:00",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Noon shipment briefing, Friday, October 9.", completed.stdout)

    def test_import_cannot_reopen_a_delivered_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shipments.json"
            store = Store(path)
            store.shipments = [
                Shipment(
                    shipment_id="north-pier-1",
                    order_id="99-1",
                    customer_name="Riley Chen",
                    customer_email="riley@example.com",
                    company="North Pier Dental",
                    line="6165550101",
                    device="iPhone 17",
                    carrier="fedex",
                    tracking_number="877000000001",
                    status="delivered",
                    ever_delivered=True,
                    order_closed=True,
                    notified_events=["shipped", "delivered"],
                )
            ]
            store.save()
            incoming = Path(tmp) / "incoming.json"
            incoming.write_text(
                json.dumps(
                    [
                        {
                            "shipment_id": "north-pier-1",
                            "order_id": "99-1",
                            "customer_name": "Riley Chen",
                            "customer_email": "riley@example.com",
                            "company": "North Pier Dental",
                            "line": "6165550101",
                            "device": "iPhone 17",
                            "carrier": "fedex",
                            "tracking_number": "877000000001",
                            "order_closed": False,
                            "status": "pending",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, "-m", "shipment_monitor", "import-shipments", "--store", str(path), "--input", str(incoming)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            saved = Store(path).load()
            self.assertTrue(saved.shipments[0].ever_delivered)
            self.assertTrue(saved.shipments[0].order_closed)
            self.assertEqual(saved.shipments[0].status, "delivered")


if __name__ == "__main__":
    unittest.main()
