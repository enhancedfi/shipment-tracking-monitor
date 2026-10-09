import unittest
from datetime import datetime, timedelta, timezone

from shipment_monitor.carriers import FixtureClient
from shipment_monitor.clock import FakeClock
from shipment_monitor.email_trigger import EmailTrigger
from shipment_monitor.models import DELAYED, DELIVERED, OUT_FOR_DELIVERY, SHIPPED, Shipment, TrackingSnapshot
from shipment_monitor.monitor import run_check
from shipment_monitor.monitor import apply_webhook
from shipment_monitor.store import Store

NOW = datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)


def make_store(shipments) -> Store:
    store = Store(path=__import__("pathlib").Path("/tmp/unused-shipments.json"))
    store.shipments = shipments
    return store


def line(shipment_id, email, carrier, tracking, **overrides) -> Shipment:
    payload = dict(
        shipment_id=shipment_id,
        order_id="99-1",
        customer_name="Riley Chen",
        customer_email=email,
        company="North Pier Dental",
        line="6165550101",
        device="iPhone 17",
        carrier=carrier,
        tracking_number=tracking,
        status="pending",
    )
    payload.update(overrides)
    return Shipment(**payload)


def snap(carrier, tracking, status) -> TrackingSnapshot:
    return TrackingSnapshot(
        carrier=carrier,
        tracking_number=tracking,
        status=status,
        description=status,
        event_time="2026-10-09T16:00:00Z",
        source="fixture",
    )


class MonitorTests(unittest.TestCase):
    def test_status_changes_queue_one_email_each_and_dry_run_does_not_call_sender(self):
        shipments = [
            line("a", "riley@example.com", "fedex", "877000000001"),
            line("b", "jordan@example.com", "ups", "1Z999AA10123456784", status=SHIPPED, notified_events=[SHIPPED]),
        ]
        carriers = {
            "fedex": FixtureClient("fedex", {"877000000001": snap("fedex", "877000000001", SHIPPED)}),
            "ups": FixtureClient("ups", {"1Z999AA10123456784": snap("ups", "1Z999AA10123456784", OUT_FOR_DELIVERY)}),
        }
        script = __import__("pathlib").Path("/workspace/customer-comms/send-shipment-updates.py")
        email = EmailTrigger(script, mode="dry-run", runner=lambda argv: self.fail("sender ran"))
        results = run_check(make_store(shipments), carriers, FakeClock(NOW), email, persist=False)
        by_id = {item.shipment_id: item for item in results}
        self.assertEqual(by_id["a"].action, "would_send")
        self.assertEqual(by_id["a"].event, SHIPPED)
        self.assertEqual(by_id["b"].action, "would_send")
        self.assertEqual(by_id["b"].event, OUT_FOR_DELIVERY)
        self.assertEqual(shipments[0].notified_events, [])
        self.assertEqual(shipments[0].pending_event, SHIPPED)
        self.assertIn("--event", by_id["a"].argv)
        self.assertIn("send-shipment-updates.py", " ".join(by_id["a"].argv))

    def test_one_email_per_six_hours_except_shipped_and_delivered(self):
        first = line("a", "riley@example.com", "fedex", "877000000001", status=SHIPPED, notified_events=[SHIPPED])
        second = line(
            "b",
            "riley@example.com",
            "ups",
            "1Z999AA10123456784",
            status=SHIPPED,
            notified_events=[SHIPPED],
            line="6165550102",
        )
        third = line(
            "c",
            "riley@example.com",
            "usps",
            "9400111899223344556677",
            status="pending",
            line="6165550103",
        )
        store = make_store([first, second, third])
        store.email_log.append(
            __import__("shipment_monitor.models", fromlist=["EmailLogEntry"]).EmailLogEntry(
                "riley@example.com",
                "older",
                DELAYED,
                (NOW - timedelta(hours=1)).isoformat(),
            )
        )
        carriers = {
            "fedex": FixtureClient("fedex", {"877000000001": snap("fedex", "877000000001", DELAYED)}),
            "ups": FixtureClient("ups", {"1Z999AA10123456784": snap("ups", "1Z999AA10123456784", DELIVERED)}),
            "usps": FixtureClient("usps", {"9400111899223344556677": snap("usps", "9400111899223344556677", SHIPPED)}),
        }
        calls = []
        script = self._script()
        email = EmailTrigger(script, mode="execute", runner=lambda argv: calls.append(argv) or 0)
        results = run_check(store, carriers, FakeClock(NOW), email, persist=False)
        by_id = {item.shipment_id: item for item in results}
        self.assertEqual(by_id["a"].action, "defer")
        self.assertEqual(by_id["a"].reason, "rate_limit_6h")
        self.assertEqual(by_id["b"].action, "send")
        self.assertEqual(by_id["b"].event, DELIVERED)
        self.assertEqual(by_id["c"].action, "send")
        self.assertEqual(by_id["c"].event, SHIPPED)
        events = [call[call.index("--event") + 1] for call in calls]
        self.assertEqual(events, [DELIVERED, SHIPPED])
        self.assertNotIn(DELAYED, events)

    def test_delivered_history_sends_only_delivered(self):
        shipment = line("a", "riley@example.com", "fedex", "877000000001")
        snapshot = snap("fedex", "877000000001", DELIVERED)
        carriers = {"fedex": FixtureClient("fedex", {"877000000001": snapshot})}
        calls = []
        email = EmailTrigger(self._script(), mode="execute", runner=lambda argv: calls.append(argv) or 0)
        run_check(make_store([shipment]), carriers, FakeClock(NOW), email, persist=False)
        events = [call[call.index("--event") + 1] for call in calls]
        self.assertEqual(events, [DELIVERED])
        self.assertTrue(shipment.ever_delivered)

    def test_closed_order_never_calls_the_sender(self):
        shipment = line("a", "riley@example.com", "fedex", "877000000001", order_closed=True, pending_event=SHIPPED)
        carriers = {"fedex": FixtureClient("fedex", {"877000000001": snap("fedex", "877000000001", SHIPPED)})}
        calls = []
        email = EmailTrigger(self._script(), mode="execute", runner=lambda argv: calls.append(argv) or 0)
        polled = run_check(make_store([shipment]), carriers, FakeClock(NOW), email, persist=False)
        self.assertEqual(polled[0].reason, "closed_order_guard")
        self.assertEqual(carriers["fedex"].calls, [])
        self.assertEqual(calls, [])
        webhook = apply_webhook(
            make_store([line("a", "riley@example.com", "fedex", "877000000001", order_closed=True)]),
            snap("fedex", "877000000001", SHIPPED),
            FakeClock(NOW),
            email,
            persist=False,
        )
        self.assertEqual(webhook[0].reason, "closed_order_guard")
        self.assertEqual(calls, [])

    def test_webhook_shipped_after_delivery_is_blocked(self):
        shipment = line(
            "a",
            "riley@example.com",
            "fedex",
            "877000000001",
            status=DELIVERED,
            ever_delivered=True,
            notified_events=[SHIPPED, DELIVERED],
        )
        calls = []
        email = EmailTrigger(self._script(), mode="execute", runner=lambda argv: calls.append(argv) or 0)
        results = apply_webhook(
            make_store([shipment]),
            snap("fedex", "877000000001", SHIPPED),
            FakeClock(NOW),
            email,
            persist=False,
        )
        self.assertEqual(results[0].reason, "delivered_line_guard")
        self.assertEqual(calls, [])
        self.assertEqual(shipment.status, DELIVERED)

    def test_cooldown_skips_a_second_poll_and_flush_sends_without_polling(self):
        shipment = line("a", "riley@example.com", "ups", "1Z999AA10123456784")
        client = FixtureClient("ups", {"1Z999AA10123456784": snap("ups", "1Z999AA10123456784", DELAYED)})
        clock = FakeClock(NOW)
        store = make_store([shipment])
        email = EmailTrigger(self._script(), mode="dry-run")
        run_check(store, {"ups": client}, clock, email, persist=False)
        self.assertEqual(len(client.calls), 1)
        run_check(store, {"ups": client}, clock, email, persist=False)
        self.assertEqual(len(client.calls), 1)
        later_moment = NOW + timedelta(hours=7)
        shipment.pending_event = DELAYED
        shipment.status = DELAYED
        shipment.last_checked_at = later_moment.isoformat()
        calls = []
        execute = EmailTrigger(self._script(), mode="execute", runner=lambda argv: calls.append(argv) or 0)
        later = FakeClock(later_moment)
        results = run_check(store, {"ups": client}, later, execute, persist=False)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(results[0].action, "send")
        self.assertEqual(results[0].event, DELAYED)

    def test_sender_failure_stays_queued(self):
        shipment = line("a", "riley@example.com", "fedex", "877000000001")
        carriers = {"fedex": FixtureClient("fedex", {"877000000001": snap("fedex", "877000000001", SHIPPED)})}
        email = EmailTrigger(self._script(), mode="execute", runner=lambda argv: 1)
        results = run_check(make_store([shipment]), carriers, FakeClock(NOW), email, persist=False)
        self.assertEqual(results[0].action, "retry")
        self.assertEqual(shipment.pending_event, SHIPPED)
        self.assertNotIn(SHIPPED, shipment.notified_events)

    def test_same_box_updates_every_line(self):
        shared = "877000000001"
        shipments = [
            line("a", "riley@example.com", "fedex", shared, line="6165550101"),
            line("b", "riley@example.com", "fedex", shared, line="6165550102"),
        ]
        carriers = {"fedex": FixtureClient("fedex", {shared: snap("fedex", shared, SHIPPED)})}
        calls = []
        email = EmailTrigger(self._script(), mode="execute", runner=lambda argv: calls.append(argv) or 0)
        run_check(make_store(shipments), carriers, FakeClock(NOW), email, persist=False)
        self.assertEqual(len(calls), 2)
        self.assertEqual(carriers["fedex"].calls, [[shared]])

    def _script(self):
        path = __import__("pathlib").Path("/tmp/send-shipment-updates-test.py")
        path.write_text("raise SystemExit(0)\n", encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
