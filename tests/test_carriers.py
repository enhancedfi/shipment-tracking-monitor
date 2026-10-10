import json
import unittest
from datetime import datetime, timezone

from shipment_monitor.carriers import FEDEX_BATCH, FedExClient, UPSClient, USPSClient, valid_tracking_number
from shipment_monitor.clock import FakeClock
from shipment_monitor.http_util import CarrierRateLimitError
from shipment_monitor.models import DELIVERED, OUT_FOR_DELIVERY, SHIPPED
from shipment_monitor.rate_limit import RequestBudget, SpacingLimiter

NOW = datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body, "at": None})
        for index, (predicate, status, payload) in enumerate(self.routes):
            if predicate(method, url):
                self.routes.pop(index)
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected {method} {url}")


def token():
    return {"access_token": "token-1", "expires_in": 3600}


def fedex_track(code):
    return {
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


class CarrierHttpTests(unittest.TestCase):
    def test_fedex_reuses_a_token_and_batches(self):
        clock = FakeClock(NOW)
        transport = FakeTransport(
            [
                (lambda method, url: url.endswith("/oauth/token"), 200, token()),
                (lambda method, url: url.endswith("/track/v1/trackingnumbers"), 200, fedex_track("OD")),
                (lambda method, url: url.endswith("/track/v1/trackingnumbers"), 200, fedex_track("DL")),
            ]
        )
        client = FedExClient("id", "secret", "https://apis.fedex.com", clock, SpacingLimiter(0, clock), RequestBudget(10), transport)
        first = client.track_many(["877000000010"])
        second = client.track_many(["877000000010"])
        self.assertEqual(first[0].status, OUT_FOR_DELIVERY)
        self.assertEqual(second[0].status, DELIVERED)
        oauth_calls = [call for call in transport.calls if call["url"].endswith("/oauth/token")]
        self.assertEqual(len(oauth_calls), 1)
        self.assertNotIn("secret", oauth_calls[0]["url"])
        self.assertIn(b"client_secret=secret", oauth_calls[0]["body"])

    def test_token_refresh_after_unauthorized_track(self):
        clock = FakeClock(NOW)
        def token_route(method, url):
            return method == "POST" and url.endswith("/oauth2/v3/token")

        def track_route(method, url):
            return method == "GET" and "/tracking/" in url

        transport = FakeTransport(
            [
                (token_route, 200, {"access_token": "one", "expires_in": 3600}),
                (track_route, 401, {"error": "expired"}),
                (token_route, 200, {"access_token": "two", "expires_in": 3600}),
                (
                    track_route,
                    200,
                    {"trackingNumber": "9400111899223344556677", "statusCategory": "Delivered", "status": "Delivered"},
                ),
            ]
        )
        client = USPSClient("id", "secret", "https://apis.usps.com", clock, SpacingLimiter(0, clock), RequestBudget(10), transport)
        snapshot = client.track_many(["9400111899223344556677"])[0]
        self.assertEqual(snapshot.status, DELIVERED)
        self.assertTrue(transport.calls[2]["url"].endswith("/oauth2/v3/token"))
        self.assertIn("Bearer two", transport.calls[3]["headers"]["Authorization"])

    def test_ups_spaces_calls_and_stops_on_429(self):
        clock = FakeClock(NOW)
        track_times = []

        def in_transit(number):
            return {
                "trackResponse": {
                    "shipment": [
                        {
                            "package": [
                                {
                                    "trackingNumber": number,
                                    "activity": [{"status": {"type": "I", "description": "In transit", "code": "I"}}],
                                }
                            ]
                        }
                    ]
                }
            }

        def transport(method, url, headers, body, timeout):
            if url.endswith("/oauth/token"):
                return 200, json.dumps(token()).encode()
            track_times.append(clock.now())
            number = url.rstrip("/").split("/")[-1]
            return 200, json.dumps(in_transit(number)).encode()

        client = UPSClient(
            "id",
            "secret",
            "https://onlinetools.ups.com",
            clock,
            SpacingLimiter(10, clock),
            RequestBudget(10),
            transport,
        )
        snapshots = client.track_many(["1Z999AA10123456784", "1Z999AA10999999990"])
        self.assertEqual([item.status for item in snapshots], [SHIPPED, SHIPPED])
        self.assertGreaterEqual((track_times[1] - track_times[0]).total_seconds(), 10)

        clock2 = FakeClock(NOW)
        calls = {"tracks": 0}

        def transport2(method, url, headers, body, timeout):
            if url.endswith("/oauth/token"):
                return 200, json.dumps(token()).encode()
            calls["tracks"] += 1
            if calls["tracks"] == 1:
                return 200, json.dumps(in_transit("1Z999AA10123456784")).encode()
            return 429, b"{}"

        client2 = UPSClient(
            "id",
            "secret",
            "https://onlinetools.ups.com",
            clock2,
            SpacingLimiter(0, clock2),
            RequestBudget(10),
            transport2,
        )
        partial = client2.track_many(["1Z999AA10123456784", "1Z999AA10999999999"])
        self.assertEqual(len(partial), 1)
        self.assertEqual(partial[0].status, SHIPPED)

        def transport3(method, url, headers, body, timeout):
            if url.endswith("/oauth/token"):
                return 200, json.dumps(token()).encode()
            return 429, b"{}"

        client3 = UPSClient(
            "id",
            "secret",
            "https://onlinetools.ups.com",
            FakeClock(NOW),
            SpacingLimiter(0, FakeClock(NOW)),
            RequestBudget(10),
            transport3,
        )
        with self.assertRaises(CarrierRateLimitError):
            client3.track_many(["1Z999AA10123456784"])

    def test_budget_stops_before_a_second_track(self):
        clock = FakeClock(NOW)
        transport = FakeTransport(
            [
                (lambda method, url: url.endswith("/oauth/token"), 200, token()),
                (lambda method, url: url.endswith("/track/v1/trackingnumbers"), 200, fedex_track("IT")),
            ]
        )
        client = FedExClient("id", "secret", "https://apis.fedex.com", clock, SpacingLimiter(0, clock), RequestBudget(2), transport)
        numbers = ["877000000010"] * (FEDEX_BATCH + 1)
        snapshots = client.track_many(numbers)
        self.assertEqual(len(snapshots), FEDEX_BATCH)
        self.assertEqual(snapshots[0].status, SHIPPED)

    def test_tracking_numbers_are_constrained(self):
        self.assertTrue(valid_tracking_number("877000000001"))
        self.assertFalse(valid_tracking_number("12"))
        self.assertFalse(valid_tracking_number("8770 0000"))
        self.assertFalse(valid_tracking_number("../etc/passwd"))


if __name__ == "__main__":
    unittest.main()
