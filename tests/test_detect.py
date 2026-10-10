import unittest
from datetime import datetime, timedelta, timezone

from shipment_monitor.detect import admit, apply_observation, within_email_window
from shipment_monitor.models import (
    DELAYED,
    DELIVERED,
    OUT_FOR_DELIVERY,
    PENDING,
    SHIPPED,
    Shipment,
    TrackingSnapshot,
)

NOW = datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)


def line(**overrides) -> Shipment:
    payload = dict(
        shipment_id="line-1",
        order_id="99-1",
        customer_name="Riley Chen",
        customer_email="Riley@Example.com",
        company="North Pier Dental",
        line="6165550101",
        device="iPhone 17",
        carrier="fedex",
        tracking_number="877000000001",
        status=PENDING,
    )
    payload.update(overrides)
    return Shipment(**payload)


def snap(status, **overrides) -> TrackingSnapshot:
    payload = dict(
        carrier="fedex",
        tracking_number="877000000001",
        status=status,
        raw_code=status,
        description=status,
        event_time="2026-10-09T16:00:00Z",
    )
    payload.update(overrides)
    return TrackingSnapshot(**payload)


class DetectTests(unittest.TestCase):
    def test_pending_to_shipped_is_a_candidate(self):
        shipment = line()
        observed = apply_observation(shipment, snap(SHIPPED), NOW)
        self.assertEqual(observed.email_event, SHIPPED)
        self.assertEqual(shipment.status, SHIPPED)

    def test_delivered_snapshot_never_emits_shipped(self):
        shipment = line()
        observed = apply_observation(shipment, snap(DELIVERED, description="Delivered"), NOW)
        self.assertEqual(observed.email_event, DELIVERED)
        self.assertNotEqual(observed.email_event, SHIPPED)
        self.assertIn(SHIPPED, shipment.notified_events)
        self.assertNotIn(DELIVERED, shipment.notified_events)
        self.assertTrue(shipment.ever_delivered)
        shipment.notified_events.append(DELIVERED)
        replay = apply_observation(shipment, snap(SHIPPED, description="Picked up"), NOW)
        self.assertIsNone(replay.email_event)
        self.assertEqual(replay.reason, "delivered_line_guard")
        self.assertEqual(shipment.status, DELIVERED)
        self.assertEqual(shipment.last_description, "Delivered")

    def test_delivered_line_blocks_shipped_even_if_shipped_was_never_notified(self):
        shipment = line(status=DELIVERED, ever_delivered=True, notified_events=[DELIVERED])
        observed = apply_observation(shipment, snap(SHIPPED), NOW)
        self.assertIsNone(observed.email_event)
        self.assertEqual(observed.reason, "delivered_line_guard")

    def test_closed_order_does_not_email(self):
        shipment = line(order_closed=True)
        observed = apply_observation(shipment, snap(SHIPPED), NOW)
        self.assertIsNone(observed.email_event)
        self.assertEqual(observed.reason, "closed_order_guard")
        self.assertIsNone(shipment.pending_event)
        again = apply_observation(shipment, snap(DELIVERED), NOW)
        self.assertEqual(again.reason, "closed_order_guard")
        self.assertIsNone(again.email_event)
        self.assertTrue(shipment.ever_delivered)

    def test_unchanged_status_does_not_email(self):
        shipment = line(status=SHIPPED, notified_events=[SHIPPED])
        observed = apply_observation(shipment, snap(SHIPPED), NOW)
        self.assertIsNone(observed.email_event)
        self.assertEqual(observed.reason, "unchanged")

    def test_out_for_delivery_and_delay(self):
        shipment = line(status=SHIPPED, notified_events=[SHIPPED])
        delayed = apply_observation(shipment, snap(DELAYED), NOW)
        self.assertEqual(delayed.email_event, DELAYED)
        shipment.notified_events.append(DELAYED)
        regression = apply_observation(shipment, snap(SHIPPED), NOW)
        self.assertIsNone(regression.email_event)
        self.assertEqual(regression.reason, "already_notified")
        again = apply_observation(shipment, snap(DELAYED), NOW)
        self.assertEqual(again.email_event, DELAYED)

    def test_out_for_delivery_does_not_fall_back_to_shipped(self):
        shipment = line(status=OUT_FOR_DELIVERY, notified_events=[SHIPPED, OUT_FOR_DELIVERY])
        observed = apply_observation(shipment, snap(SHIPPED), NOW)
        self.assertEqual(observed.reason, "status_regression")
        self.assertEqual(shipment.status, OUT_FOR_DELIVERY)

    def test_rate_limit_window_and_immediate_bypass(self):
        sent = NOW - timedelta(hours=5, minutes=59)
        self.assertTrue(within_email_window(sent, NOW))
        self.assertFalse(within_email_window(NOW - timedelta(hours=6), NOW))
        self.assertEqual(admit(DELAYED, sent, NOW), "defer")
        self.assertEqual(admit(OUT_FOR_DELIVERY, sent, NOW), "defer")
        self.assertEqual(admit(SHIPPED, sent, NOW), "send")
        self.assertEqual(admit(DELIVERED, sent, NOW), "send")
        self.assertEqual(admit(DELAYED, NOW - timedelta(hours=6), NOW), "send")
        self.assertEqual(admit(DELAYED, None, NOW), "send")


if __name__ == "__main__":
    unittest.main()
