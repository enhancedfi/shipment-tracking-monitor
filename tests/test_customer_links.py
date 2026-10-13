"""Order-status links, ETA-today delivery day, and the Pyramids UPS page text."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shipment_monitor.carriers import FixtureClient
from shipment_monitor.clock import FakeClock
from shipment_monitor.delivery_day import delivery_day_shipments
from shipment_monitor.email_body import render_email_body
from shipment_monitor.email_trigger import EmailTrigger, build_argv, email_handoff
from shipment_monitor.links import email_links, order_status_url
from shipment_monitor.models import OUT_FOR_DELIVERY, SHIPPED, Shipment
from shipment_monitor.monitor import run_check
from shipment_monitor.orders import load_shipments_document
from shipment_monitor.page_text import parse_estimated_delivery, parse_tracking_page
from shipment_monitor.store import Store

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "examples" / "page-text"
PREPARING = "1ZH4K6592930123854"
ON_VEHICLE = "1ZH4K6592920755842"
EASTERN = ZoneInfo("America/New_York")
CAPTURED = datetime(2026, 10, 13, 16, 0, tzinfo=EASTERN)


class OrderLinkTests(unittest.TestCase):
    def test_link_is_built_from_recorded_lookup_email_and_zip(self):
        url = order_status_url("buyer@pyramids.example", "49503")
        self.assertEqual(
            url,
            "https://www.att.com/acctmgmt/order/details?email=buyer%40pyramids.example&zip=49503",
        )
        shipment = _line(lookup_email="buyer@pyramids.example", lookup_zip="49503")
        tracking, tracking_html, order, order_html = email_links(shipment)
        self.assertEqual(tracking, f"https://www.ups.com/track?tracknum={PREPARING}")
        self.assertIn(f'<a href="{tracking}">Track shipment</a>', tracking_html)
        self.assertEqual(order, url)
        self.assertIn("<a href=", order_html)
        self.assertIn("acctmgmt/order/details", order_html)
        self.assertIn("zip=49503", order_html)
        argv = build_argv(Path("send-shipment-updates.py"), shipment, "shipped", "Preparing for Delivery", "2026-10-13T07:52:00")
        self.assertIn(order_html, argv)
        self.assertIn(tracking_html, argv)
        self.assertEqual(argv[argv.index("--order-status-url") + 1], url)

    def test_missing_lookup_email_or_zip_omits_the_order_link(self):
        self.assertIsNone(order_status_url("", "49503"))
        self.assertIsNone(order_status_url("buyer@pyramids.example", ""))
        self.assertIsNone(order_status_url("buyer@pyramids.example", "not-a-zip"))
        customer_only = _line(lookup_email="", lookup_zip="")
        customer_only.customer_email = "buyer@pyramids.example"
        _tracking, tracking_html, order, order_html = email_links(customer_only)
        self.assertIsNone(order)
        self.assertIsNone(order_html)
        self.assertIn("<a href=", tracking_html)
        argv = build_argv(Path("send-shipment-updates.py"), customer_only, "shipped", "In transit", "2026-10-13T07:52:00")
        self.assertNotIn("--order-status-url", argv)
        self.assertNotIn("--order-status-link", argv)
        self.assertNotIn("acctmgmt/order/details", " ".join(argv))
        self.assertIn("--tracking-link", argv)

        zip_only = _line(lookup_email="", lookup_zip="49503")
        self.assertIsNone(order_status_url(zip_only.lookup_email, zip_only.lookup_zip))
        email_only = _line(lookup_email="buyer@pyramids.example", lookup_zip="")
        self.assertIsNone(email_links(email_only)[2])

    def test_orders_json_copies_lookup_onto_shipment_records(self):
        document = {
            "orders": [
                {
                    "order_id": "99-pyramids",
                    "lookup_email": "buyer@pyramids.example",
                    "lookup_zip": "49503",
                    "customer_name": "Riley Chen",
                    "customer_email": "riley@example.com",
                    "company": "Pyramids",
                    "shipments": [
                        {
                            "shipment_id": "pyramids-1",
                            "line": "6165550101",
                            "device": "iPhone 17",
                            "carrier": "ups",
                            "tracking_number": PREPARING,
                        }
                    ],
                }
            ]
        }
        shipments = load_shipments_document(document)
        self.assertEqual(shipments[0].lookup_email, "buyer@pyramids.example")
        self.assertEqual(shipments[0].lookup_zip, "49503")
        self.assertEqual(shipments[0].customer_email, "riley@example.com")
        self.assertEqual(shipments[0].company, "Pyramids")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shipments.json"
            incoming = Path(tmp) / "orders.json"
            incoming.write_text(json.dumps(document), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-m", "shipment_monitor", "import-shipments", "--store", str(path), "--input", str(incoming)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            saved = Store(path).load()
            self.assertEqual(saved.shipments[0].lookup_email, "buyer@pyramids.example")
            self.assertEqual(saved.shipments[0].lookup_zip, "49503")

    def test_sender_argv_includes_the_rendered_anchors(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "send-shipment-updates.py"
            script.write_text("raise SystemExit(0)\n", encoding="utf-8")
            shipment = _line(lookup_email="buyer@pyramids.example", lookup_zip="49503", status="pending")
            calls = []
            email = EmailTrigger(script, mode="execute", runner=lambda argv: calls.append(list(argv)) or 0)
            store = Store(Path(tmp) / "shipments.json")
            store.shipments = [shipment]
            carriers = {
                "ups": FixtureClient(
                    "ups",
                    {PREPARING: parse_tracking_page("ups", _page(PREPARING), PREPARING, now=CAPTURED)},
                )
            }
            results = run_check(store, carriers, FakeClock(CAPTURED), email, persist=False)
            self.assertEqual(results[0].action, "send", results[0].reason)
            sent = calls[0]
            order_html = sent[sent.index("--order-status-link") + 1]
            tracking_html = sent[sent.index("--tracking-link") + 1]
            self.assertIn('<a href="https://www.att.com/acctmgmt/order/details?', order_html)
            self.assertIn("zip=49503", order_html)
            self.assertIn(f'<a href="https://www.ups.com/track?tracknum={PREPARING}">', tracking_html)

    def test_rendered_email_body_contains_the_order_and_tracking_links(self):
        shipment = _line(lookup_email="buyer@pyramids.example", lookup_zip="49503", status="pending")
        _tracking, tracking_html, order_url, order_html = email_links(shipment)
        body = render_email_body(shipment, "shipped", "Preparing for Delivery")
        self.assertIn(tracking_html, body)
        self.assertIn(order_html, body)
        self.assertIn(f"https://www.ups.com/track?tracknum={PREPARING}", body)
        self.assertIn("https://www.att.com/acctmgmt/order/details?", body)
        self.assertIn("zip=49503", body)
        self.assertEqual(order_url, "https://www.att.com/acctmgmt/order/details?email=buyer%40pyramids.example&zip=49503")
        handoff = email_handoff(shipment, "shipped", "Preparing for Delivery", "2026-10-13T07:52:00")
        self.assertEqual(handoff["body"], body)
        self.assertEqual(
            handoff["order_status_url"],
            "https://www.att.com/acctmgmt/order/details?email=buyer%40pyramids.example&zip=49503",
        )
        self.assertEqual(handoff["tracking_url"], f"https://www.ups.com/track?tracknum={PREPARING}")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "send-shipment-updates.py"
            body_path = Path(tmp) / "body.html"
            copy_path = Path(tmp) / "state-copy.json"
            mode_path = Path(tmp) / "mode.txt"
            script.write_text(
                "import json, os\n"
                "from pathlib import Path\n"
                "state_file = Path(os.environ['SHIPMENT_EMAIL_STATE'])\n"
                "raw = state_file.read_text(encoding='utf-8')\n"
                "state = json.loads(raw)\n"
                f"Path({str(body_path)!r}).write_text(state['body'], encoding='utf-8')\n"
                f"Path({str(copy_path)!r}).write_text(raw, encoding='utf-8')\n"
                f"Path({str(mode_path)!r}).write_text(str(state_file.stat().st_mode & 0o777), encoding='utf-8')\n",
                encoding="utf-8",
            )
            state_path = Path(tmp) / "shipments.json.email.json"
            email = EmailTrigger(script, mode="execute", state_path=state_path)
            store = Store(Path(tmp) / "shipments.json")
            store.shipments = [shipment]
            carriers = {
                "ups": FixtureClient(
                    "ups",
                    {PREPARING: parse_tracking_page("ups", _page(PREPARING), PREPARING, now=CAPTURED)},
                )
            }
            results = run_check(store, carriers, FakeClock(CAPTURED), email, persist=False)
            self.assertEqual(results[0].action, "send", results[0].reason)
            rendered = body_path.read_text(encoding="utf-8")
            self.assertIn(tracking_html, rendered)
            self.assertIn(order_html, rendered)
            self.assertIn("acctmgmt/order/details", rendered)
            self.assertIn("zip=49503", rendered)
            self.assertIn(f"track?tracknum={PREPARING}", rendered)
            self.assertEqual(int(mode_path.read_text(encoding="utf-8")), 0o600)
            self.assertFalse(state_path.exists())
            saved = json.loads(copy_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["body"], rendered)
            self.assertNotIn("--tracking-url", saved["body"])


class DeliveryDayTests(unittest.TestCase):
    def test_pyramids_ups_pages_estimate_today(self):
        for number in (PREPARING, ON_VEHICLE):
            eta = parse_estimated_delivery(_page(number), CAPTURED)
            self.assertEqual(eta, "2026-10-13", number)
        preparing = parse_tracking_page("ups", _page(PREPARING), PREPARING, now=CAPTURED)
        self.assertEqual(preparing.status, SHIPPED)
        self.assertEqual(preparing.estimated_delivery, "2026-10-13")
        vehicle = parse_tracking_page("ups", _page(ON_VEHICLE), ON_VEHICLE, now=CAPTURED)
        self.assertEqual(vehicle.status, OUT_FOR_DELIVERY)
        self.assertEqual(vehicle.estimated_delivery, "2026-10-13")

    def test_delivery_day_includes_eta_today_and_excludes_later_in_transit(self):
        with tempfile.TemporaryDirectory() as tmp:
            pages = Path(tmp)
            (pages / f"{PREPARING}.txt").write_text(_page(PREPARING), encoding="utf-8")
            later = "1Z999AA10123456784"
            (pages / f"{later}.txt").write_text(
                "\n".join(
                    [
                        "On the way",
                        "Estimated delivery",
                        "October 16, 2026",
                        "by 9:00 PM",
                        "Shipment Progress",
                        "10/13/2026",
                        "7:52 A.M.",
                        "In transit",
                        "Wyoming, MI",
                    ]
                ),
                encoding="utf-8",
            )
            today = _line(shipment_id="today", tracking=PREPARING, status=SHIPPED)
            in_transit = _line(shipment_id="later", tracking=later, status=SHIPPED)
            on_truck = _line(
                shipment_id="truck",
                tracking="1ZH4K6592900000000",
                status=OUT_FOR_DELIVERY,
                estimated_delivery="2026-10-16",
            )
            chosen = delivery_day_shipments([today, in_transit, on_truck], CAPTURED, pages)
            self.assertEqual({item.shipment_id for item in chosen}, {"today", "truck"})

    def test_delivery_day_poll_skips_the_in_transit_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            pages = Path(tmp)
            (pages / f"{PREPARING}.txt").write_text(_page(PREPARING), encoding="utf-8")
            later = "1Z999AA10123456784"
            today = _line(shipment_id="today", tracking=PREPARING, status="pending")
            in_transit = _line(shipment_id="later", tracking=later, status=SHIPPED, estimated_delivery="2026-10-16")
            chosen = {item.shipment_id for item in delivery_day_shipments([today, in_transit], CAPTURED, pages)}
            client = FixtureClient(
                "ups",
                {
                    PREPARING: parse_tracking_page("ups", _page(PREPARING), PREPARING, now=CAPTURED),
                    later: parse_tracking_page("ups", "On the way\nEstimated delivery\nOctober 16, 2026\n", later, now=CAPTURED),
                },
            )
            email = EmailTrigger(Path("send-shipment-updates.py"), mode="dry-run")
            store = Store(Path(tmp) / "shipments.json")
            store.shipments = [today, in_transit]
            run_check(store, {"ups": client}, FakeClock(CAPTURED), email, persist=False, force=True, only_ids=chosen)
            self.assertEqual(client.calls, [[PREPARING]])
            self.assertEqual(today.estimated_delivery, "2026-10-13")
            self.assertEqual(in_transit.estimated_delivery, "2026-10-16")

    def test_yearless_weekday_and_month_day_etas_infer_the_year(self):
        self.assertEqual(
            parse_estimated_delivery("Estimated delivery\nWednesday, October 14\nby 9:00 PM\n", CAPTURED),
            "2026-10-14",
        )
        self.assertEqual(parse_estimated_delivery("Expected delivery date\nOct 14\n", CAPTURED), "2026-10-14")
        self.assertEqual(parse_estimated_delivery("Estimated delivery\nOctober 12\n", CAPTURED), "2026-10-12")
        self.assertEqual(parse_estimated_delivery("Estimated delivery\nJanuary 5\n", CAPTURED), "2027-01-05")

    def test_yearless_late_date_stays_in_the_past_year(self):
        new_year = datetime(2027, 1, 1, 10, tzinfo=EASTERN)
        self.assertEqual(parse_estimated_delivery("Estimated delivery\nDec 31\n", new_year), "2026-12-31")
        self.assertEqual(parse_estimated_delivery("Estimated delivery\nDecember 31\n", new_year), "2026-12-31")

    def test_a_scan_date_is_not_read_as_the_eta(self):
        scan_only = "\n".join(
            [
                "On the way",
                "Estimated delivery",
                "10/12/2026",
                "3:15 P.M.",
                "Departed from facility",
                "Wyoming, MI",
            ]
        )
        self.assertIsNone(parse_estimated_delivery(scan_only, CAPTURED))
        promised = "\n".join(
            [
                "Estimated delivery",
                "Wednesday, October 14",
                "10/13/2026",
                "7:52 A.M.",
                "Processing at UPS Facility",
                "Wyoming, MI",
            ]
        )
        self.assertEqual(parse_estimated_delivery(promised, CAPTURED), "2026-10-14")

    def test_a_date_only_scan_line_is_not_the_eta(self):
        self.assertIsNone(parse_estimated_delivery("Estimated delivery\n10/13/2026\n", CAPTURED))
        self.assertIsNone(
            parse_estimated_delivery(
                "Estimated delivery\n10/13/2026\nProcessing at UPS Facility\n",
                CAPTURED,
            )
        )
        self.assertEqual(
            parse_estimated_delivery(
                "Estimated delivery\nOctober 16, 2026\nby 9:00 PM\n10/13/2026\n",
                CAPTURED,
            ),
            "2026-10-16",
        )
        self.assertEqual(
            parse_estimated_delivery(
                "Estimated delivery\nWednesday, October 14\n10/13/2026\n",
                CAPTURED,
            ),
            "2026-10-14",
        )

    def test_stale_saved_today_page_is_ignored_and_a_late_package_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            pages = Path(tmp)
            path = pages / f"{PREPARING}.txt"
            path.write_text(_page(PREPARING), encoding="utf-8")
            stale = datetime(2026, 10, 12, 15, 0, tzinfo=EASTERN).timestamp()
            os.utime(path, (stale, stale))
            shipment = _line(status=SHIPPED)
            self.assertEqual(delivery_day_shipments([shipment], CAPTURED, pages), [])
            fresh = datetime(2026, 10, 13, 15, 0, tzinfo=EASTERN).timestamp()
            os.utime(path, (fresh, fresh))
            self.assertEqual(
                [item.shipment_id for item in delivery_day_shipments([shipment], CAPTURED, pages)],
                ["pyramids-1"],
            )
        late = _line(
            shipment_id="late",
            tracking="1Z999AA10123456784",
            status=SHIPPED,
            estimated_delivery="2026-10-10",
        )
        future = _line(
            shipment_id="future",
            tracking="1Z999AA10123456785",
            status=SHIPPED,
            estimated_delivery="2026-10-16",
        )
        delivered = _line(
            shipment_id="done",
            tracking="1Z999AA10123456786",
            status="delivered",
            ever_delivered=True,
            estimated_delivery="2026-10-10",
        )
        chosen = {item.shipment_id for item in delivery_day_shipments([late, future, delivered], CAPTURED, None)}
        self.assertEqual(chosen, {"late"})


def _page(number: str) -> str:
    return (PAGES / f"{number}.txt").read_text(encoding="utf-8")


def _line(**overrides) -> Shipment:
    payload = dict(
        shipment_id="pyramids-1",
        order_id="99-pyramids",
        customer_name="Riley Chen",
        customer_email="riley@example.com",
        company="Pyramids",
        line="6165550101",
        device="iPhone 17",
        carrier="ups",
        tracking_number=PREPARING,
        status="pending",
    )
    if "tracking" in overrides:
        payload["tracking_number"] = overrides.pop("tracking")
    payload.update(overrides)
    return Shipment(**payload)


if __name__ == "__main__":
    unittest.main()
