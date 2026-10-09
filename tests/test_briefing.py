import unittest
from datetime import datetime, timezone

from shipment_monitor.briefing import resolve_slot, shipment_summary
from shipment_monitor.models import DELAYED, DELIVERED, OUT_FOR_DELIVERY, SHIPPED, Shipment

NOON = datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)
MORNING = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
AFTERNOON = datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc)


def line(shipment_id, company, status, changed, **overrides):
    payload = dict(
        shipment_id=shipment_id,
        order_id="99-1",
        customer_name="Riley Chen",
        customer_email="riley@example.com",
        company=company,
        line="6165550101",
        device="iPhone 17",
        carrier="fedex",
        tracking_number=f"8770000000{shipment_id[-2:]}",
        status=status,
        status_changed_at=changed,
    )
    payload.update(overrides)
    return Shipment(**payload)


class BriefingTests(unittest.TestCase):
    def test_slots_match_the_briefing_clock(self):
        self.assertEqual(resolve_slot(MORNING), "morning")
        self.assertEqual(resolve_slot(NOON), "noon")
        self.assertEqual(resolve_slot(AFTERNOON), "afternoon")
        self.assertEqual(resolve_slot(datetime(2026, 10, 9, 14, 30, tzinfo=timezone.utc)), "morning")
        self.assertEqual(resolve_slot(datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc)), "noon")

    def test_summary_is_concise_and_stable(self):
        shipments = [
            line("b2", "North Pier Dental", OUT_FOR_DELIVERY, "2026-10-09T15:00:00+00:00", line="6165550102", tracking_number="877000000202"),
            line("a1", "Harbor Books", DELAYED, "2026-10-09T15:10:00+00:00", carrier="ups", tracking_number="1Z999AA10123456784"),
            line("c3", "Harbor Books", DELIVERED, "2026-10-09T15:30:00+00:00", line="6165550103", tracking_number="877000000303"),
            line("d4", "North Pier Dental", SHIPPED, "2026-10-09T14:00:00+00:00", line="6165550104", tracking_number="877000000404"),
            line("e5", "Old News", DELIVERED, "2026-10-08T12:00:00+00:00", tracking_number="877000000505"),
            line("f6", "Closed Example Co", SHIPPED, "2026-10-09T15:00:00+00:00", order_closed=True, tracking_number="877000000606"),
        ]
        text = shipment_summary(shipments, NOON, "noon")
        again = shipment_summary(list(reversed(shipments)), NOON, "noon")
        self.assertEqual(text, again)
        self.assertTrue(text.startswith("Noon shipment briefing, Friday, October 9.\n"))
        self.assertIn("1 out for delivery", text)
        self.assertIn("1 delayed", text)
        self.assertIn("1 delivered since 6:00 AM", text)
        self.assertLess(text.index("Out for delivery:"), text.index("Delayed:"))
        self.assertLess(text.index("Delayed:"), text.index("Delivered since"))
        self.assertIn("UPS 1Z999AA10123456784", text)
        self.assertNotIn("Old News", text)
        self.assertNotIn("Closed Example Co", text)
        self.assertIn("Shipped since 6:00 AM", text)

    def test_each_slot_names_its_window(self):
        shipments = [line("a1", "Harbor Books", SHIPPED, "2026-10-09T15:00:00+00:00", tracking_number="877000000101")]
        self.assertIn("Morning shipment briefing", shipment_summary(shipments, MORNING, "morning"))
        self.assertIn("since 4:00 PM yesterday", shipment_summary(shipments, MORNING, "morning"))
        self.assertIn("Afternoon shipment briefing", shipment_summary(shipments, AFTERNOON, "afternoon"))
        self.assertIn("since 12:00 PM", shipment_summary(shipments, AFTERNOON, "afternoon"))

    def test_quiet_and_empty(self):
        self.assertIn("No open phone shipments.", shipment_summary([], NOON, "noon"))
        quiet = line("a1", "Harbor Books", SHIPPED, "2026-10-01T15:00:00+00:00", tracking_number="877000000101")
        text = shipment_summary([quiet], NOON, "noon")
        self.assertIn("No delays, none out for delivery, no new deliveries.", text)

    def test_long_lists_are_capped(self):
        shipments = [
            line(
                f"id{index:02d}",
                "Harbor Books",
                DELAYED,
                "2026-10-09T15:00:00+00:00",
                line=f"6165550{index:03d}",
                tracking_number=f"87700000{index:04d}",
            )
            for index in range(16)
        ]
        text = shipment_summary(shipments, NOON, "noon")
        self.assertIn("and 1 more.", text)
        self.assertEqual(text.count("Harbor Books"), 15)


if __name__ == "__main__":
    unittest.main()
