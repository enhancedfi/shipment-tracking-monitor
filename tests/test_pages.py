"""Public tracking-page text: UPS captures from 2026-10-13, plus FedEx and USPS patterns."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shipment_monitor.carriers import UPSClient
from shipment_monitor.cli import _carriers
from shipment_monitor.models import DELIVERED, OUT_FOR_DELIVERY, PENDING, SHIPPED, Shipment
from shipment_monitor.page_source import WebPageClient, active_page_targets, tracking_page_url
from shipment_monitor.page_text import parse_tracking_page

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "examples" / "page-text"
SAMPLE = ROOT / "examples" / "shipments.sample.json"
PREPARING = "1Z999AA10123456784"
ON_VEHICLE = "1Z999AA10987654321"


def _page(number: str) -> str:
    return (PAGES / f"{number}.txt").read_text(encoding="utf-8")


def _clean_env() -> dict[str, str]:
    skip = {"SHIPMENT_ALLOW_EMAIL", "SHIPMENT_PAGE_TEXT_DIR", "SHIPMENT_SENDER_SCRIPT", "SHIPMENT_USE_API"}
    env = {}
    for key, value in os.environ.items():
        if key in skip or key.endswith("_CLIENT_ID") or key.endswith("_CLIENT_SECRET") or key.endswith("_API_BASE"):
            continue
        env[key] = value
    return env


class PageTextTests(unittest.TestCase):
    def test_ups_preparing_for_delivery_is_in_transit_shipped(self):
        snapshot = parse_tracking_page("ups", _page(PREPARING), PREPARING)
        self.assertEqual(snapshot.status, SHIPPED)
        self.assertNotEqual(snapshot.status, OUT_FOR_DELIVERY)
        self.assertNotEqual(snapshot.status, DELIVERED)
        self.assertEqual(snapshot.description, "Preparing for Delivery")
        self.assertEqual(snapshot.raw_code, "preparing for delivery")
        self.assertEqual(snapshot.event_time, "2026-10-13T07:52:00")
        self.assertEqual(snapshot.tracking_number, PREPARING)
        self.assertEqual(snapshot.carrier, "ups")
        self.assertEqual(snapshot.source, "page")
        self.assertIn("Signature Required", _page(PREPARING))
        self.assertIn("Delivered", _page(PREPARING))

    def test_ups_loaded_on_delivery_vehicle_is_out_for_delivery(self):
        snapshot = parse_tracking_page("ups", _page(ON_VEHICLE), ON_VEHICLE)
        self.assertEqual(snapshot.status, OUT_FOR_DELIVERY)
        self.assertEqual(snapshot.description, "Loaded on Delivery Vehicle")
        self.assertEqual(snapshot.raw_code, "loaded on delivery vehicle")
        self.assertEqual(snapshot.event_time, "2026-10-13T08:36:00")
        self.assertEqual(snapshot.source, "page")

    def test_single_line_clock_matches_the_ups_capture(self):
        text = "\n".join(
            [
                "Preparing for Delivery",
                "Estimated delivery today by 9:00 PM",
                "Shipment Progress",
                "10/13/2026 7:52 AM",
                "Processing at UPS Facility",
                "Faketown, ZZ",
            ]
        )
        snapshot = parse_tracking_page("ups", text, PREPARING)
        self.assertEqual(snapshot.status, SHIPPED)
        self.assertEqual(snapshot.event_time, "2026-10-13T07:52:00")

    def test_dated_delivery_scan_beats_an_earlier_pickup(self):
        text = "\n".join(
            [
                "In Transit",
                "Shipment Progress",
                "10/13/2026",
                "4:00 P.M.",
                "Delivered",
                "Faketown, ZZ",
                "10/13/2026",
                "8:00 A.M.",
                "Pickup Scan",
                "Faketown, ZZ",
            ]
        )
        snapshot = parse_tracking_page("ups", text, "1Z999AA10123456784")
        self.assertEqual(snapshot.status, DELIVERED)
        self.assertEqual(snapshot.description, "Delivered")
        self.assertEqual(snapshot.event_time, "2026-10-13T16:00:00")

    def test_fedex_page_patterns(self):
        on_the_way = "\n".join(
            [
                "On the way",
                "Estimated delivery",
                "Tuesday, October 13, 2026",
                "by 9:00 PM",
                "Travel History",
                "Oct 13, 2026",
                "6:12 AM",
                "Arrived at FedEx location",
                "Faketown, ZZ",
            ]
        )
        out = "\n".join(
            [
                "Out for delivery",
                "Travel History",
                "Oct 13, 2026 8:36 AM",
                "On FedEx vehicle for delivery",
                "Faketown, ZZ",
            ]
        )
        delayed = "\n".join(
            [
                "Delivery exception",
                "Travel History",
                "Oct 13, 2026 5:01 AM",
                "Weather delay",
                "Faketown, ZZ",
            ]
        )
        self.assertEqual(parse_tracking_page("fedex", on_the_way, "877000000010").status, SHIPPED)
        self.assertEqual(parse_tracking_page("fedex", on_the_way, "877000000010").event_time, "2026-10-13T06:12:00")
        vehicle = parse_tracking_page("fedex", out, "877000000011")
        self.assertEqual(vehicle.status, OUT_FOR_DELIVERY)
        self.assertEqual(vehicle.description, "Out for delivery")
        self.assertEqual(vehicle.event_time, "2026-10-13T08:36:00")
        self.assertEqual(parse_tracking_page("fedex", delayed, "877000000012").status, "delayed")

    def test_usps_page_patterns(self):
        in_transit = "\n".join(
            [
                "Status",
                "In Transit",
                "Expected Delivery",
                "October 14, 2026",
                "Tracking History",
                "October 13, 2026, 7:52 am",
                "Arrived at USPS Regional Facility",
                "Faketown, ZZ 99999",
            ]
        )
        pickup = "\n".join(
            [
                "Status",
                "Available for Pickup",
                "Tracking History",
                "October 13, 2026, 9:05 am",
                "Available for Pickup",
                "Faketown, ZZ 99999",
            ]
        )
        alert = "\n".join(
            [
                "Status",
                "Alert",
                "Tracking History",
                "October 13, 2026, 1:15 pm",
                "Delivery Attempt",
                "Faketown, ZZ 99999",
            ]
        )
        label = "\n".join(["Status", "Pre-Shipment", "Tracking History"])
        self.assertEqual(parse_tracking_page("usps", in_transit, "9400111899223344556677").status, SHIPPED)
        self.assertEqual(
            parse_tracking_page("usps", in_transit, "9400111899223344556677").event_time,
            "2026-10-13T07:52:00",
        )
        self.assertEqual(parse_tracking_page("usps", pickup, "9400111899223344556678").status, OUT_FOR_DELIVERY)
        self.assertEqual(parse_tracking_page("usps", alert, "9400111899223344556679").status, "delayed")
        self.assertEqual(parse_tracking_page("usps", label, "9400111899223344556680").status, PENDING)
        for page in (in_transit, pickup, alert):
            self.assertIn("Faketown, ZZ 99999", page)
            self.assertNotIn("WYOMING", page)
            self.assertNotIn("49519", page)

    def test_missing_page_file_is_not_a_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = WebPageClient("ups", Path(tmp))
            self.assertEqual(client.track_many([PREPARING]), [])
            blank = Path(tmp) / f"{PREPARING}.txt"
            blank.write_text("  \n", encoding="utf-8")
            self.assertEqual(client.track_many([PREPARING]), [])


class PageFlowTests(unittest.TestCase):
    def test_urls_skip_delivered_and_closed_orders(self):
        store = json.loads(SAMPLE.read_text(encoding="utf-8"))
        shipments = [Shipment.from_dict(item) for item in store["shipments"]]
        shipments.append(
            Shipment(
                shipment_id="shared",
                order_id="99-1",
                customer_name="Riley Chen",
                customer_email="riley@example.com",
                company="North Pier Dental",
                line="6165550108",
                device="iPhone 17",
                carrier="ups",
                tracking_number="1Z999AA10123456784",
                status=SHIPPED,
            )
        )
        targets = active_page_targets(shipments)
        self.assertEqual(
            targets,
            [
                ("fedex", "877000000001"),
                ("ups", "1Z999AA10123456784"),
                ("usps", "9400111899223344556677"),
            ],
        )
        self.assertEqual(
            tracking_page_url("ups", PREPARING),
            f"https://www.ups.com/track?tracknum={PREPARING}",
        )
        self.assertEqual(
            tracking_page_url("fedex", "877000000001"),
            "https://www.fedex.com/fedextrack/?trknbr=877000000001",
        )
        self.assertEqual(
            tracking_page_url("usps", "9400111899223344556677"),
            "https://tools.usps.com/go/TrackConfirmAction?tLabels=9400111899223344556677",
        )

    def test_list_script_prints_public_urls(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "list_tracking_pages.py"), str(SAMPLE)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            env=_clean_env(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                "https://www.fedex.com/fedextrack/?trknbr=877000000001",
                "https://www.ups.com/track?tracknum=1Z999AA10123456784",
                "https://tools.usps.com/go/TrackConfirmAction?tLabels=9400111899223344556677",
            ],
        )

    def test_check_without_keys_or_pages_is_still_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "shipments.json"
            store.write_text(_one_shipment(PREPARING, "pending"), encoding="utf-8")
            completed = self._run(
                [
                    "check",
                    "--store",
                    str(store),
                    "--now",
                    "2026-10-13T16:00:00+00:00",
                ]
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout.splitlines()[-1])
        self.assertEqual(payload["reason"], "carrier_not_configured")

    def test_check_uses_page_text_when_no_api_keys_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "shipments.json"
            store.write_text(_one_shipment(PREPARING, "pending"), encoding="utf-8")
            completed = self._run(
                [
                    "check",
                    "--store",
                    str(store),
                    "--pages",
                    str(PAGES),
                    "--now",
                    "2026-10-13T16:00:00+00:00",
                ]
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout.splitlines()[-1])
        self.assertEqual(payload["action"], "would_send")
        self.assertEqual(payload["event"], SHIPPED)
        self.assertNotEqual(payload["reason"], "carrier_not_configured")
        self.assertNotIn("send-shipment-updates.py", completed.stderr)

    def test_run_imports_polls_and_dry_run_does_not_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = tmp_path / "shipments.json"
            incoming = tmp_path / "incoming.json"
            sent = tmp_path / "sent.txt"
            sender = tmp_path / "send-shipment-updates.py"
            sender.write_text(
                f"from pathlib import Path\nPath({str(sent)!r}).write_text('sent', encoding='utf-8')\n",
                encoding="utf-8",
            )
            incoming.write_text(
                json.dumps(
                    [
                        _row("prep", PREPARING, "a@example.com", "6165550101"),
                        _row("vehicle", ON_VEHICLE, "b@example.com", "6165550102"),
                    ]
                ),
                encoding="utf-8",
            )
            completed = self._run(
                [
                    "run",
                    "--store",
                    str(store),
                    "--input",
                    str(incoming),
                    "--pages",
                    str(PAGES),
                    "--now",
                    "2026-10-13T16:00:00+00:00",
                    "--sender",
                    str(sender),
                ]
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(sent.exists())
            self.assertIn("stored 2 shipments", completed.stdout)
            by_id = {
                json.loads(line)["shipment_id"]: json.loads(line)
                for line in completed.stdout.splitlines()
                if line.startswith("{")
            }
            self.assertEqual(by_id["prep"]["action"], "would_send")
            self.assertEqual(by_id["prep"]["event"], SHIPPED)
            self.assertEqual(by_id["vehicle"]["action"], "would_send")
            self.assertEqual(by_id["vehicle"]["event"], OUT_FOR_DELIVERY)
            saved = json.loads(store.read_text(encoding="utf-8"))
            prep = next(item for item in saved["shipments"] if item["shipment_id"] == "prep")
            self.assertEqual(prep["status"], SHIPPED)
            self.assertEqual(prep["last_description"], "Preparing for Delivery")
            self.assertEqual(prep["last_event_at"], "2026-10-13T07:52:00")
            self.assertEqual(saved["email_log"], [])

    def test_run_execute_calls_the_stub_sender_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = tmp_path / "shipments.json"
            incoming = tmp_path / "incoming.json"
            log = tmp_path / "sender.log"
            sender = tmp_path / "send-shipment-updates.py"
            sender.write_text(
                "import os, sys\nfrom pathlib import Path\n"
                "Path(os.environ['STUB_LOG']).open('a').write('\\n'.join(sys.argv) + '\\n---\\n')\n",
                encoding="utf-8",
            )
            incoming.write_text(json.dumps([_row("prep", PREPARING, "a@example.com", "6165550101")]), encoding="utf-8")
            env = _clean_env()
            env["SHIPMENT_ALLOW_EMAIL"] = "1"
            env["STUB_LOG"] = str(log)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "shipment_monitor",
                    "run",
                    "--store",
                    str(store),
                    "--input",
                    str(incoming),
                    "--pages",
                    str(PAGES),
                    "--now",
                    "2026-10-13T16:00:00+00:00",
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
            self.assertIn("\nshipped\n", sent)
            self.assertIn("--status-description\nPreparing for Delivery\n", sent)
            self.assertIn(PREPARING, sent)
            self.assertNotIn("\ndelivered\n", sent)

    def test_api_clients_remain_when_page_text_is_not_configured(self):
        from datetime import datetime, timezone

        from shipment_monitor.clock import FakeClock

        args = type("Args", (), {"command": "check", "fixtures": None, "pages": None, "api": False})()
        keys = ("UPS_CLIENT_ID", "UPS_CLIENT_SECRET", "SHIPMENT_PAGE_TEXT_DIR", "SHIPMENT_USE_API")
        previous = {key: os.environ.get(key) for key in keys}
        os.environ["UPS_CLIENT_ID"] = "id"
        os.environ["UPS_CLIENT_SECRET"] = "secret"
        os.environ.pop("SHIPMENT_PAGE_TEXT_DIR", None)
        os.environ.pop("SHIPMENT_USE_API", None)
        try:
            blocked = _carriers(args, FakeClock(datetime(2026, 10, 13, tzinfo=timezone.utc)))
            args.api = True
            clients = _carriers(args, FakeClock(datetime(2026, 10, 13, tzinfo=timezone.utc)))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(blocked, {})
        self.assertIsInstance(clients["ups"], UPSClient)
        self.assertNotIn("fedex", clients)

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "shipment_monitor", *argv],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            env=_clean_env(),
        )


def _row(shipment_id: str, tracking: str, email: str, line: str) -> dict:
    return {
        "shipment_id": shipment_id,
        "order_id": "99-1",
        "customer_name": "Riley Chen",
        "customer_email": email,
        "company": "North Pier Dental",
        "line": line,
        "device": "iPhone 17",
        "carrier": "ups",
        "tracking_number": tracking,
        "status": "pending",
    }


def _one_shipment(tracking: str, status: str) -> str:
    row = _row("only", tracking, "a@example.com", "6165550101")
    row["status"] = status
    return json.dumps({"shipments": [row], "email_log": []})


if __name__ == "__main__":
    unittest.main()
