"""Load carrier fixtures so a check can run with no network and no API keys."""

from __future__ import annotations

import json
from pathlib import Path

from shipment_monitor.carriers import FixtureClient
from shipment_monitor.models import TrackingSnapshot, normalize_carrier
from shipment_monitor.parsing import parse_carrier_body


def load_fixture_clients(directory: Path) -> dict[str, FixtureClient]:
    clients: dict[str, FixtureClient] = {}
    if not directory.is_dir():
        raise FileNotFoundError(f"fixture directory not found: {directory}")
    for carrier_dir in sorted(path for path in directory.iterdir() if path.is_dir()):
        carrier = normalize_carrier(carrier_dir.name)
        by_number: dict[str, TrackingSnapshot] = {}
        for path in sorted(carrier_dir.glob("*.json")):
            body = json.loads(path.read_text(encoding="utf-8"))
            number = path.stem
            snapshot = _snapshot_from_fixture(carrier, number, body)
            by_number[snapshot.tracking_number] = snapshot
        clients[carrier] = FixtureClient(carrier, by_number)
    if not clients:
        raise FileNotFoundError(f"no carrier fixtures in {directory}")
    return clients


def _snapshot_from_fixture(carrier: str, number: str, body: dict) -> TrackingSnapshot:
    if "status" in body and "output" not in body and "trackResponse" not in body and "statusCategory" not in body:
        return TrackingSnapshot(
            carrier=carrier,
            tracking_number=str(body.get("tracking_number") or number),
            status=str(body["status"]),
            raw_code=str(body.get("raw_code") or ""),
            description=str(body.get("description") or ""),
            event_time=body.get("event_time"),
            source="fixture",
        )
    parsed = parse_carrier_body(carrier, body, tracking_hint=number, source="fixture")
    if not parsed:
        raise ValueError(f"fixture {carrier}/{number} did not contain a tracking result")
    return parsed[0]
