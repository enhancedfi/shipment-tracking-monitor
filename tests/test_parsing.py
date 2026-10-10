import json
import unittest
from pathlib import Path

from shipment_monitor.models import DELAYED, DELIVERED, OUT_FOR_DELIVERY, PENDING, SHIPPED
from shipment_monitor.parsing import parse_fedex_body, parse_ups_body, parse_usps_body, snapshot_from_webhook

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "fixtures"


class ParsingTests(unittest.TestCase):
    def test_fedex_delivery_in_history_beats_a_pickup_latest_status(self):
        body = json.loads((FIXTURES / "fedex" / "877000000001.json").read_text(encoding="utf-8"))
        snapshot = parse_fedex_body(body)[0]
        self.assertEqual(snapshot.status, DELIVERED)
        self.assertEqual(snapshot.tracking_number, "877000000001")
        self.assertEqual(snapshot.description, "Delivered")
        self.assertEqual(snapshot.event_time, "2026-10-09T15:04:00")

    def test_fedex_codes(self):
        def status_for(code):
            body = {
                "output": {
                    "completeTrackResults": [
                        {
                            "trackingNumber": "877000000010",
                            "trackResults": [
                                {
                                    "trackingNumberInfo": {"trackingNumber": "877000000010"},
                                    "latestStatusDetail": {"code": code, "statusByLocale": code},
                                    "scanEvents": [{"eventType": code}],
                                }
                            ],
                        }
                    ]
                }
            }
            return parse_fedex_body(body)[0].status

        self.assertEqual(status_for("OC"), PENDING)
        self.assertEqual(status_for("PU"), SHIPPED)
        self.assertEqual(status_for("IT"), SHIPPED)
        self.assertEqual(status_for("OD"), OUT_FOR_DELIVERY)
        self.assertEqual(status_for("DL"), DELIVERED)
        self.assertEqual(status_for("DE"), DELAYED)

    def test_ups_out_for_delivery_is_not_a_generic_in_transit(self):
        body = json.loads((FIXTURES / "ups" / "1Z999AA10123456784.json").read_text(encoding="utf-8"))
        snapshot = parse_ups_body(body)[0]
        self.assertEqual(snapshot.status, OUT_FOR_DELIVERY)

    def test_ups_types(self):
        def status_for(kind, description):
            body = {
                "trackResponse": {
                    "shipment": [
                        {
                            "package": [
                                {
                                    "trackingNumber": "1Z999AA10123456784",
                                    "activity": [{"status": {"type": kind, "description": description, "code": kind}}],
                                }
                            ]
                        }
                    ]
                }
            }
            return parse_ups_body(body)[0].status

        self.assertEqual(status_for("M", "Shipper created a label"), PENDING)
        self.assertEqual(status_for("P", "Pickup"), SHIPPED)
        self.assertEqual(status_for("I", "Arrived at facility"), SHIPPED)
        self.assertEqual(status_for("X", "Weather delay"), DELAYED)
        self.assertEqual(status_for("D", "Delivered"), DELIVERED)

    def test_usps_categories(self):
        def status_for(category):
            body = {"trackingNumber": "9400111899223344556677", "statusCategory": category, "status": category}
            return parse_usps_body(body)[0].status

        self.assertEqual(status_for("Pre-Shipment"), PENDING)
        self.assertEqual(status_for("Accepted"), SHIPPED)
        self.assertEqual(status_for("Out for Delivery"), OUT_FOR_DELIVERY)
        self.assertEqual(status_for("Delivered"), DELIVERED)
        self.assertEqual(status_for("Alert"), DELAYED)

    def test_usps_weather_fixture(self):
        body = json.loads((FIXTURES / "usps" / "9400111899223344556677.json").read_text(encoding="utf-8"))
        self.assertEqual(parse_usps_body(body)[0].status, DELAYED)

    def test_webhook_envelope_and_raw_payload(self):
        envelope = snapshot_from_webhook(
            {
                "carrier": "FedEx",
                "tracking_number": "877000000010",
                "status_code": "OD",
                "status_description": "Out for delivery",
                "event_time": "2026-10-09T16:00:00Z",
            }
        )
        self.assertEqual(envelope.carrier, "fedex")
        self.assertEqual(envelope.status, OUT_FOR_DELIVERY)
        raw = snapshot_from_webhook(
            {
                "carrier": "ups",
                "tracking_number": "1Z999AA10123456784",
                "carrier_payload": json.loads((FIXTURES / "ups" / "1Z999AA10123456784.json").read_text(encoding="utf-8")),
            }
        )
        self.assertEqual(raw.status, OUT_FOR_DELIVERY)
        self.assertEqual(raw.source, "webhook")


if __name__ == "__main__":
    unittest.main()
