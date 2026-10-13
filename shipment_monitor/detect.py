"""Decide whether a new carrier status should email the customer.

Three rules from the existing customer-comms sender are enforced here, before
that script is ever invoked:

1. A delivered line can never trigger a shipped email.
2. A closed order does not get another email.
3. At most one email per six hours per customer, except shipped and delivered,
   which always send immediately.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from shipment_monitor.models import (
    BLOCKED_REGRESSIONS,
    CUSTOMER_EVENTS,
    DELAYED,
    DELIVERED,
    IMMEDIATE_EVENTS,
    OUT_FOR_DELIVERY,
    SHIPPED,
    Observation,
    Shipment,
    TrackingSnapshot,
    isoformat,
)

EMAIL_WINDOW = timedelta(hours=6)


def remember(events: list[str], event: str) -> None:
    if event not in events:
        events.append(event)


def _remember_reading(shipment: Shipment, snapshot: TrackingSnapshot) -> None:
    shipment.last_raw_code = snapshot.raw_code
    shipment.last_description = snapshot.description
    if snapshot.event_time:
        shipment.last_event_at = snapshot.event_time
    if snapshot.estimated_delivery:
        shipment.estimated_delivery = snapshot.estimated_delivery


def _queue_note(shipment: Shipment, event: str, now: datetime) -> None:
    shipment.pending_event = event
    if not shipment.pending_event_at:
        shipment.pending_event_at = isoformat(now)


def apply_observation(
    shipment: Shipment,
    snapshot: TrackingSnapshot,
    now: datetime,
) -> Observation:
    """Update the line from one carrier reading and name the email, if any.

    The returned event is a candidate. Rate limiting happens later so one
    check can admit shipped and delivered immediately and hold the rest.
    """
    observed = snapshot.status
    previous = shipment.status
    shipment.last_checked_at = isoformat(now)

    if previous == DELIVERED or shipment.ever_delivered:
        shipment.ever_delivered = True
        shipment.status = DELIVERED
        if observed != DELIVERED:
            # A later pickup or in-transit scan is history, not a new shipment.
            # If the delivery email itself has not gone out yet, keep that
            # email queued. Never replace it with a shipped email.
            if shipment.order_closed:
                shipment.pending_event = None
                shipment.pending_event_at = None
                return Observation(None, "closed_order_guard")
            if DELIVERED not in shipment.notified_events:
                _queue_note(shipment, DELIVERED, now)
                return Observation(DELIVERED, "pending_due")
            return Observation(None, "delivered_line_guard")
        _remember_reading(shipment, snapshot)
        if DELIVERED in shipment.notified_events and shipment.pending_event != DELIVERED:
            return Observation(None, "unchanged")
        return _candidate(shipment, DELIVERED, "pending_due" if shipment.pending_event == DELIVERED else "status_change")

    blocked = BLOCKED_REGRESSIONS.get(previous, frozenset())
    if observed in blocked:
        return Observation(None, "status_regression")

    _remember_reading(shipment, snapshot)

    if observed != previous:
        if previous in {DELAYED, OUT_FOR_DELIVERY}:
            shipment.notified_events = [event for event in shipment.notified_events if event != previous]
        shipment.status = observed
        shipment.status_changed_at = isoformat(now)
        if shipment.pending_event and shipment.pending_event != observed:
            shipment.pending_event = None
            shipment.pending_event_at = None

    if observed == DELIVERED:
        shipment.ever_delivered = True
        # Poison shipped without sending it. The delivery is the email.
        remember(shipment.notified_events, SHIPPED)

    if shipment.order_closed:
        shipment.pending_event = None
        shipment.pending_event_at = None
        return Observation(None, "closed_order_guard")

    if observed == previous:
        if shipment.pending_event == observed and observed in CUSTOMER_EVENTS:
            return _candidate(shipment, observed, "pending_due")
        return Observation(None, "unchanged")

    if observed not in CUSTOMER_EVENTS:
        return Observation(None, "not_actionable")
    if observed == SHIPPED and (shipment.ever_delivered or SHIPPED in shipment.notified_events):
        return Observation(None, "delivered_line_guard" if shipment.ever_delivered else "already_notified")
    if observed == DELIVERED and DELIVERED in shipment.notified_events:
        return Observation(None, "already_notified")
    if observed in {DELAYED, OUT_FOR_DELIVERY} and observed in shipment.notified_events:
        return Observation(None, "already_notified")
    return _candidate(shipment, observed, "status_change")


def _candidate(shipment: Shipment, event: str, reason: str) -> Observation:
    if shipment.order_closed:
        shipment.pending_event = None
        shipment.pending_event_at = None
        return Observation(None, "closed_order_guard")
    if event == SHIPPED and shipment.ever_delivered:
        shipment.pending_event = None
        shipment.pending_event_at = None
        return Observation(None, "delivered_line_guard")
    return Observation(event, reason)


def pending_candidate(shipment: Shipment) -> Observation | None:
    """Revisit a queued email without calling the carrier again."""
    if not shipment.pending_event:
        return None
    if shipment.order_closed:
        shipment.pending_event = None
        shipment.pending_event_at = None
        return Observation(None, "closed_order_guard")
    if shipment.ever_delivered and shipment.pending_event == SHIPPED:
        shipment.pending_event = None
        shipment.pending_event_at = None
        return Observation(None, "delivered_line_guard")
    if shipment.pending_event != shipment.status:
        shipment.pending_event = None
        shipment.pending_event_at = None
        return Observation(None, "superseded")
    return Observation(shipment.pending_event, "pending_due")


def within_email_window(last_sent_at: datetime | None, now: datetime) -> bool:
    if last_sent_at is None:
        return False
    return now - last_sent_at < EMAIL_WINDOW


def admit(event: str, last_sent_at: datetime | None, now: datetime) -> str:
    """Return send or defer. Shipped and delivered are never deferred."""
    if event in IMMEDIATE_EVENTS:
        return "send"
    if within_email_window(last_sent_at, now):
        return "defer"
    return "send"
