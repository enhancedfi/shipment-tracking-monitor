"""FedEx, UPS, and USPS official tracking API clients.

Calls go to the REST APIs. OAuth tokens are cached until shortly before
expiry. Track calls are spaced by a limiter and stopped on HTTP 429.
These clients are optional. Public tracking pages are read from saved text
by WebPageClient and do not use the credentials here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from urllib.parse import quote

from shipment_monitor.clock import Clock
from shipment_monitor.http_util import (
    CarrierAuthError,
    CarrierRateLimitError,
    CarrierTemporaryError,
    decode_json,
    raise_for_status,
    urllib_transport,
)
from shipment_monitor.models import UNKNOWN, TrackingSnapshot
from shipment_monitor.parsing import parse_fedex_body, parse_ups_body, parse_usps_body
from shipment_monitor.rate_limit import RequestBudget, SpacingLimiter

FEDEX_PRODUCTION = "https://apis.fedex.com"
UPS_PRODUCTION = "https://onlinetools.ups.com"
USPS_PRODUCTION = "https://apis.usps.com"
FEDEX_BATCH = 30


def valid_tracking_number(value: str) -> bool:
    cleaned = value.strip()
    return cleaned.isalnum() and 8 <= len(cleaned) <= 40


class CarrierClient:
    carrier = ""

    def track_many(self, numbers: list[str]) -> list[TrackingSnapshot]:
        raise NotImplementedError


class _OAuthClient(CarrierClient):
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str,
        clock: Clock,
        limiter: SpacingLimiter,
        budget: RequestBudget,
        transport=urllib_transport,
        timeout: float = 20.0,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.clock = clock
        self.limiter = limiter
        self.budget = budget
        self.transport = transport
        self.timeout = timeout
        self._token = ""
        self._token_until = None

    def _request(self, method: str, url: str, headers: dict[str, str], body: bytes | None, *, auth: bool = False) -> dict:
        if not self.budget.take():
            raise CarrierTemporaryError("track request budget for this run is spent")
        self.limiter.wait(self.carrier)
        status, raw = self.transport(method, url, headers, body, self.timeout)
        parsed = decode_json(status, raw)
        raise_for_status(status, parsed, auth=auth)
        return parsed

    def _token_fresh(self) -> bool:
        if not self._token or self._token_until is None:
            return False
        return self.clock.now() < self._token_until

    def access_token(self) -> str:
        if self._token_fresh():
            return self._token
        payload = self.fetch_token()
        token = str(payload.get("access_token") or "")
        if not token:
            raise CarrierAuthError("carrier token response did not include access_token")
        expires_in = int(payload.get("expires_in") or 3600)
        self._token = token
        self._token_until = self.clock.now() + timedelta(seconds=max(expires_in - 60, 30))
        return token

    def fetch_token(self) -> dict:
        raise NotImplementedError

    def _authorized_get(self, url: str, headers: dict[str, str]) -> dict:
        headers = dict(headers)
        headers["Authorization"] = f"Bearer {self.access_token()}"
        try:
            return self._request("GET", url, headers, None)
        except CarrierAuthError:
            self._token = ""
            self._token_until = None
            headers["Authorization"] = f"Bearer {self.access_token()}"
            return self._request("GET", url, headers, None)

    def _authorized_post(self, url: str, headers: dict[str, str], body: dict) -> dict:
        headers = dict(headers)
        headers["Authorization"] = f"Bearer {self.access_token()}"
        encoded = json.dumps(body).encode("utf-8")
        try:
            return self._request("POST", url, headers, encoded)
        except CarrierAuthError:
            self._token = ""
            self._token_until = None
            headers["Authorization"] = f"Bearer {self.access_token()}"
            return self._request("POST", url, headers, encoded)


class FedExClient(_OAuthClient):
    carrier = "fedex"

    def fetch_token(self) -> dict:
        body = (
            "grant_type=client_credentials"
            f"&client_id={quote(self.client_id, safe='')}"
            f"&client_secret={quote(self.client_secret, safe='')}"
        ).encode()
        return self._request(
            "POST",
            f"{self.base_url}/oauth/token",
            {"Content-Type": "application/x-www-form-urlencoded"},
            body,
            auth=True,
        )

    def track_many(self, numbers: list[str]) -> list[TrackingSnapshot]:
        snapshots: list[TrackingSnapshot] = []
        for start in range(0, len(numbers), FEDEX_BATCH):
            chunk = numbers[start : start + FEDEX_BATCH]
            body = {
                "includeDetailedScans": True,
                "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": number}} for number in chunk],
            }
            try:
                payload = self._authorized_post(
                    f"{self.base_url}/track/v1/trackingnumbers",
                    {"Content-Type": "application/json", "X-locale": "en_US"},
                    body,
                )
            except (CarrierRateLimitError, CarrierTemporaryError):
                if not snapshots:
                    raise
                break
            parsed = parse_fedex_body(payload)
            by_number = {item.tracking_number: item for item in parsed}
            for number in chunk:
                snapshots.append(
                    by_number.get(
                        number,
                        TrackingSnapshot(
                            carrier="fedex",
                            tracking_number=number,
                            status=UNKNOWN,
                            description="tracking number missing from FedEx response",
                        ),
                    )
                )
        return snapshots


class UPSClient(_OAuthClient):
    carrier = "ups"

    def fetch_token(self) -> dict:
        import base64

        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        return self._request(
            "POST",
            f"{self.base_url}/security/v1/oauth/token",
            {"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"},
            b"grant_type=client_credentials",
            auth=True,
        )

    def track_many(self, numbers: list[str]) -> list[TrackingSnapshot]:
        snapshots: list[TrackingSnapshot] = []
        for number in numbers:
            trans_id = hashlib.sha256(f"{number}:{self.clock.now().strftime('%Y%m%d%H')}".encode()).hexdigest()[:32]
            try:
                payload = self._authorized_get(
                    f"{self.base_url}/api/track/v1/details/{quote(number)}",
                    {"transId": trans_id, "transactionSrc": "shipment-tracking-monitor", "Accept": "application/json"},
                )
            except (CarrierRateLimitError, CarrierTemporaryError):
                if not snapshots:
                    raise
                break
            parsed = parse_ups_body(payload, tracking_hint=number)
            snapshots.append(parsed[0] if parsed else TrackingSnapshot(carrier="ups", tracking_number=number, status=UNKNOWN))
        return snapshots


class USPSClient(_OAuthClient):
    carrier = "usps"

    def fetch_token(self) -> dict:
        body = json.dumps(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode()
        return self._request(
            "POST",
            f"{self.base_url}/oauth2/v3/token",
            {"Content-Type": "application/json"},
            body,
            auth=True,
        )

    def track_many(self, numbers: list[str]) -> list[TrackingSnapshot]:
        snapshots: list[TrackingSnapshot] = []
        for number in numbers:
            try:
                payload = self._authorized_get(
                    f"{self.base_url}/tracking/v3/tracking/{quote(number)}?expand=DETAIL",
                    {"Accept": "application/json"},
                )
            except (CarrierRateLimitError, CarrierTemporaryError):
                if not snapshots:
                    raise
                break
            parsed = parse_usps_body(payload, tracking_hint=number)
            snapshots.append(parsed[0] if parsed else TrackingSnapshot(carrier="usps", tracking_number=number, status=UNKNOWN))
        return snapshots


class FixtureClient(CarrierClient):
    """Read saved carrier JSON from disk. Used for tests and safe local runs."""

    def __init__(self, carrier: str, by_number: dict[str, TrackingSnapshot]) -> None:
        self.carrier = carrier
        self.by_number = by_number
        self.calls: list[list[str]] = []

    def track_many(self, numbers: list[str]) -> list[TrackingSnapshot]:
        self.calls.append(list(numbers))
        snapshots = []
        for number in numbers:
            found = self.by_number.get(number)
            if found is None:
                snapshots.append(
                    TrackingSnapshot(
                        carrier=self.carrier,
                        tracking_number=number,
                        status=UNKNOWN,
                        description="no fixture for tracking number",
                    )
                )
            else:
                snapshots.append(found)
        return snapshots
