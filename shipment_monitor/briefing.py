"""Concise shipment text for the 6:00 AM, noon, and 4:00 PM briefings."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from shipment_monitor.models import DELAYED, DELIVERED, OUT_FOR_DELIVERY, PENDING, SHIPPED, Shipment, parse_time

EASTERN = ZoneInfo("America/New_York")

SLOT_LABEL = {
    "morning": "6:00 AM",
    "noon": "12:00 PM",
    "afternoon": "4:00 PM",
}
SLOT_TITLE = {
    "morning": "Morning",
    "noon": "Noon",
    "afternoon": "Afternoon",
}
PREVIOUS_LABEL = {
    "morning": "4:00 PM yesterday",
    "noon": "6:00 AM",
    "afternoon": "12:00 PM",
}
LIST_CAP = 15


def resolve_slot(now: datetime) -> str:
    """Map a clock time onto the briefing that is in force.

    12:00 through 3:59 PM is noon. 4:00 PM through 11:59 PM is afternoon.
    Everything earlier in the day is the morning briefing, including a job
    that runs at 6:00 AM.
    """
    local = now.astimezone(EASTERN)
    minutes = local.hour * 60 + local.minute
    if minutes >= 16 * 60:
        return "afternoon"
    if minutes >= 12 * 60:
        return "noon"
    return "morning"


def window_start(now: datetime, slot: str) -> datetime:
    local = now.astimezone(EASTERN)
    day = local.date()
    if slot == "noon":
        start = datetime.combine(day, time(6, 0), tzinfo=EASTERN)
    elif slot == "afternoon":
        start = datetime.combine(day, time(12, 0), tzinfo=EASTERN)
    else:
        start = datetime.combine(day - timedelta(days=1), time(16, 0), tzinfo=EASTERN)
    return start


def shipment_summary(shipments: list[Shipment], now: datetime, slot: str | None = None) -> str:
    """Return the briefing text. `slot` is morning, noon, afternoon, or omitted."""
    chosen = slot or resolve_slot(now)
    if chosen not in SLOT_LABEL:
        raise ValueError("slot must be morning, noon, or afternoon")
    local = now.astimezone(EASTERN)
    start = window_start(now, chosen)
    title = SLOT_TITLE[chosen]
    date_label = f"{local.strftime('%A')}, {local.strftime('%B')} {local.day}"
    header = f"{title} shipment briefing, {date_label}."

    open_lines = [item for item in shipments if not item.order_closed and item.status != DELIVERED]
    delayed = _sorted([item for item in open_lines if item.status == DELAYED])
    out_for_delivery = _sorted([item for item in open_lines if item.status == OUT_FOR_DELIVERY])
    in_transit = [item for item in open_lines if item.status == SHIPPED]
    waiting = [item for item in open_lines if item.status in {PENDING, "unknown"}]
    delivered = _sorted(
        [
            item
            for item in shipments
            if item.status == DELIVERED and _in_window(item, start, now)
        ]
    )
    shipped_new = _sorted(
        [item for item in in_transit if _in_window(item, start, now)]
    )

    counts = (
        f"{len(open_lines)} active. "
        f"{len(out_for_delivery)} out for delivery. "
        f"{len(delayed)} delayed. "
        f"{len(in_transit)} in transit. "
        f"{len(waiting)} waiting to ship. "
        f"{len(delivered)} delivered since {PREVIOUS_LABEL[chosen]}."
    )

    if not shipments or (not open_lines and not delivered):
        return f"{header}\nNo open phone shipments.\n"

    if not delayed and not out_for_delivery and not delivered and not shipped_new:
        quiet = (
            f"{header}\n{counts}\n"
            "No delays, none out for delivery, no new deliveries.\n"
        )
        return quiet

    lines = [header, counts]
    _append_section(lines, "Out for delivery", [_line(item) for item in out_for_delivery])
    _append_section(lines, "Delayed", [_line(item) for item in delayed])
    _append_section(lines, f"Delivered since {PREVIOUS_LABEL[chosen]}", [_line(item) for item in delivered])
    _append_section(lines, f"Shipped since {PREVIOUS_LABEL[chosen]}", [_line(item) for item in shipped_new])
    return "\n".join(lines) + "\n"


def _in_window(shipment: Shipment, start: datetime, now: datetime) -> bool:
    changed = parse_time(shipment.status_changed_at)
    if changed is None:
        return False
    return start <= changed.astimezone(EASTERN) <= now.astimezone(EASTERN)


def _sorted(items: list[Shipment]) -> list[Shipment]:
    return sorted(items, key=lambda item: (item.company.casefold(), item.order_id, item.line, item.shipment_id))


def _line(shipment: Shipment) -> str:
    carrier = shipment.carrier.upper()
    tracking = shipment.tracking_number or "no tracking number"
    return f"{shipment.company}, {shipment.device}, line {shipment.line}, {carrier} {tracking}."


def _append_section(lines: list[str], title: str, entries: list[str]) -> None:
    if not entries:
        return
    lines.append("")
    lines.append(f"{title}:")
    visible = entries[:LIST_CAP]
    lines.extend(visible)
    extra = len(entries) - len(visible)
    if extra:
        lines.append(f"and {extra} more.")
