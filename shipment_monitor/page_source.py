"""Read public tracking pages from saved text files.

No carrier developer credentials are used. The carrier sites need
JavaScript, so a browser step saves each page's visible text as
<tracking_number>.txt. This client turns those files into the same
TrackingSnapshot values the API clients return.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from shipment_monitor.carriers import CarrierClient, valid_tracking_number
from shipment_monitor.models import DELIVERED, Shipment, TrackingSnapshot, normalize_carrier
from shipment_monitor.page_text import parse_tracking_page

TRACKING_PAGE_URLS = {
    "ups": "https://www.ups.com/track?tracknum={number}",
    "fedex": "https://www.fedex.com/fedextrack/?trknbr={number}",
    "usps": "https://tools.usps.com/go/TrackConfirmAction?tLabels={number}",
}


def tracking_page_url(carrier: str, tracking_number: str) -> str:
    name = normalize_carrier(carrier)
    number = quote(tracking_number.strip(), safe="")
    return TRACKING_PAGE_URLS[name].format(number=number)


def active_page_targets(shipments: list[Shipment]) -> list[tuple[str, str]]:
    """Unique carrier and tracking number pairs that still need a page check.

    Delivered lines and closed orders are skipped. Several phone lines that
    share one box produce one URL.
    """
    seen: set[tuple[str, str]] = set()
    targets: list[tuple[str, str]] = []
    for shipment in shipments:
        if shipment.order_closed or shipment.ever_delivered or shipment.status == DELIVERED:
            continue
        number = shipment.tracking_number.strip()
        if not valid_tracking_number(number):
            continue
        key = (shipment.carrier, number)
        if key in seen:
            continue
        seen.add(key)
        targets.append(key)
    targets.sort()
    return targets


class WebPageClient(CarrierClient):
    """TrackingSnapshot source backed by saved public tracking-page text."""

    def __init__(self, carrier: str, directory: Path, clock=None) -> None:
        self.carrier = normalize_carrier(carrier)
        self.directory = directory
        self.clock = clock

    def track_many(self, numbers: list[str]) -> list[TrackingSnapshot]:
        root = self.directory.resolve()
        now = self.clock.now() if self.clock is not None else None
        snapshots: list[TrackingSnapshot] = []
        for number in numbers:
            path = (self.directory / f"{number}.txt").resolve()
            if path.parent != root or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if not text.strip():
                continue
            saved_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            snapshots.append(parse_tracking_page(self.carrier, text, number, now=now, saved_at=saved_at))
        return snapshots


def web_page_clients(directory: Path, clock=None) -> dict[str, WebPageClient]:
    return {name: WebPageClient(name, directory, clock) for name in ("fedex", "ups", "usps")}
