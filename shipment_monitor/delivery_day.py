"""Shipments that should be polled on the 30-minute delivery-day cadence.

A line is delivery-day when its tracking page ETA is today in
America/New_York, when that ETA has already passed and the package is
not delivered, or when the carrier already says out_for_delivery.
A standard in-transit line whose ETA is a later day is not.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from shipment_monitor.carriers import valid_tracking_number
from shipment_monitor.models import DELIVERED, OUT_FOR_DELIVERY, Shipment
from shipment_monitor.page_text import parse_estimated_delivery
from shipment_monitor.schedule import EASTERN


def eastern_day(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("delivery-day clock must include a timezone")
    return now.astimezone(ZoneInfo(EASTERN)).date().isoformat()


def is_delivery_day(shipment: Shipment, now: datetime, eta: str | None = None) -> bool:
    if shipment.order_closed or shipment.ever_delivered or shipment.status == DELIVERED:
        return False
    if not valid_tracking_number(shipment.tracking_number):
        return False
    if shipment.status == OUT_FOR_DELIVERY:
        return True
    resolved = eta or shipment.estimated_delivery
    if not resolved:
        return False
    # Today, or a late package whose promised day has passed.
    return resolved <= eastern_day(now)


def delivery_day_shipments(
    shipments: list[Shipment],
    now: datetime,
    pages: Path | None = None,
) -> list[Shipment]:
    chosen: list[Shipment] = []
    for shipment in shipments:
        eta = _page_eta(shipment, pages, now)
        if is_delivery_day(shipment, now, eta):
            chosen.append(shipment)
    return chosen


def _page_eta(shipment: Shipment, pages: Path | None, now: datetime) -> str | None:
    if pages is None:
        return None
    path = pages / f"{shipment.tracking_number}.txt"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    saved_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return parse_estimated_delivery(text, now, saved_at=saved_at)
