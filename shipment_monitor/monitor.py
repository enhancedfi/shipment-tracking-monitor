"""One deterministic pass over active phone shipments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from shipment_monitor.carriers import valid_tracking_number
from shipment_monitor.clock import Clock
from shipment_monitor.detect import admit, apply_observation, pending_candidate
from shipment_monitor.email_trigger import EmailTrigger
from shipment_monitor.http_util import CarrierError, CarrierRateLimitError, CarrierTemporaryError
from shipment_monitor.models import DELIVERED, SHIPPED, EmailLogEntry, Shipment, TrackingSnapshot, isoformat, parse_time
from shipment_monitor.store import Store

EVENT_ORDER = {DELIVERED: 0, SHIPPED: 1, "out_for_delivery": 2, "delayed": 3}
DEFAULT_COOLDOWN = timedelta(hours=3)


@dataclass
class CheckResult:
    shipment_id: str
    action: str
    event: str | None
    reason: str
    argv: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "shipment_id": self.shipment_id,
            "action": self.action,
            "event": self.event,
            "reason": self.reason,
            "argv": list(self.argv),
        }


@dataclass
class _Candidate:
    shipment: Shipment
    event: str
    reason: str


def due_for_poll(shipment: Shipment, now: datetime, cooldown: timedelta, force: bool) -> bool:
    if not shipment.tracking_number or not valid_tracking_number(shipment.tracking_number):
        return False
    if shipment.order_closed or shipment.ever_delivered or shipment.status == DELIVERED:
        return False
    if force or not shipment.last_checked_at:
        return True
    last = parse_time(shipment.last_checked_at)
    if last is None:
        return True
    return now - last >= cooldown


def run_check(
    store: Store,
    carriers: dict,
    clock: Clock,
    email: EmailTrigger,
    cooldown: timedelta = DEFAULT_COOLDOWN,
    force: bool = False,
    persist: bool = True,
) -> list[CheckResult]:
    """Poll due tracking numbers, then admit emails under the standing guards."""
    now = clock.now()
    results: list[CheckResult] = []
    candidates: list[_Candidate] = []
    polled_ids: set[str] = set()

    grouped: dict[str, list[Shipment]] = {}
    for shipment in store.shipments:
        if not due_for_poll(shipment, now, cooldown, force):
            continue
        grouped.setdefault(shipment.carrier, []).append(shipment)

    for carrier, shipments in sorted(grouped.items()):
        client = carriers.get(carrier)
        numbers = _unique([item.tracking_number for item in shipments])
        if client is None:
            for shipment in shipments:
                results.append(CheckResult(shipment.shipment_id, "ignore", None, "carrier_not_configured"))
            continue
        try:
            snapshots = client.track_many(numbers)
        except (CarrierRateLimitError, CarrierTemporaryError, CarrierError) as error:
            reason = "carrier_rate_limited" if isinstance(error, CarrierRateLimitError) else "carrier_error"
            for shipment in shipments:
                results.append(CheckResult(shipment.shipment_id, "ignore", None, reason))
            continue
        by_number = {item.tracking_number: item for item in snapshots}
        for shipment in shipments:
            snapshot = by_number.get(shipment.tracking_number)
            if snapshot is None:
                results.append(CheckResult(shipment.shipment_id, "ignore", None, "missing_snapshot"))
                continue
            polled_ids.add(shipment.shipment_id)
            observation = apply_observation(shipment, snapshot, now)
            _collect(candidates, results, shipment, observation)

    for shipment in store.shipments:
        if shipment.shipment_id in polled_ids:
            continue
        observation = pending_candidate(shipment)
        if observation is None:
            continue
        _collect(candidates, results, shipment, observation)

    last_sent = _last_sent(store)
    candidates.sort(key=lambda item: (EVENT_ORDER.get(item.event, 9), item.shipment.shipment_id))
    for candidate in candidates:
        shipment = candidate.shipment
        if not shipment.customer_email:
            results.append(CheckResult(shipment.shipment_id, "ignore", candidate.event, "missing_customer_email"))
            continue
        decision = admit(candidate.event, last_sent.get(shipment.customer_email), now)
        if decision == "defer":
            shipment.pending_event = candidate.event
            if not shipment.pending_event_at:
                shipment.pending_event_at = isoformat(now)
            results.append(CheckResult(shipment.shipment_id, "defer", candidate.event, "rate_limit_6h"))
            continue
        description = shipment.last_description or candidate.event.replace("_", " ")
        event_time = shipment.last_event_at or isoformat(now)
        trigger = email.send(shipment, candidate.event, description, event_time)
        if email.mode == "execute" and trigger.ok and trigger.reason == "sent":
            _mark_sent(store, shipment, candidate.event, now)
            last_sent[shipment.customer_email] = now
            results.append(CheckResult(shipment.shipment_id, "send", candidate.event, "sent", trigger.argv))
            continue
        if email.mode != "execute" and trigger.ok:
            # Preview only. Leave the event queued so the next real execute still sends it.
            shipment.pending_event = candidate.event
            if not shipment.pending_event_at:
                shipment.pending_event_at = isoformat(now)
            last_sent[shipment.customer_email] = now
            results.append(CheckResult(shipment.shipment_id, "would_send", candidate.event, "dry_run", trigger.argv))
            continue
        shipment.pending_event = candidate.event
        if not shipment.pending_event_at:
            shipment.pending_event_at = isoformat(now)
        results.append(CheckResult(shipment.shipment_id, "retry", candidate.event, trigger.reason, trigger.argv))

    results.sort(key=lambda item: item.shipment_id)
    if persist:
        store.save()
    return results


def apply_webhook(
    store: Store,
    snapshot: TrackingSnapshot,
    clock: Clock,
    email: EmailTrigger,
    persist: bool = True,
) -> list[CheckResult]:
    """Apply one pushed carrier event through the same guards as a poll."""
    now = clock.now()
    matches = [
        item
        for item in store.shipments
        if item.carrier == snapshot.carrier and item.tracking_number == snapshot.tracking_number
    ]
    results: list[CheckResult] = []
    candidates: list[_Candidate] = []
    if not matches:
        results.append(CheckResult("", "ignore", None, "unmatched_tracking"))
        return results
    for shipment in matches:
        observation = apply_observation(shipment, snapshot, now)
        _collect(candidates, results, shipment, observation)
    last_sent = _last_sent(store)
    candidates.sort(key=lambda item: (EVENT_ORDER.get(item.event, 9), item.shipment.shipment_id))
    for candidate in candidates:
        shipment = candidate.shipment
        decision = admit(candidate.event, last_sent.get(shipment.customer_email), now)
        if decision == "defer":
            shipment.pending_event = candidate.event
            shipment.pending_event_at = shipment.pending_event_at or isoformat(now)
            results.append(CheckResult(shipment.shipment_id, "defer", candidate.event, "rate_limit_6h"))
            continue
        trigger = email.send(
            shipment,
            candidate.event,
            shipment.last_description or candidate.event,
            shipment.last_event_at or isoformat(now),
        )
        if email.mode == "execute" and trigger.ok and trigger.reason == "sent":
            _mark_sent(store, shipment, candidate.event, now)
            last_sent[shipment.customer_email] = now
            results.append(CheckResult(shipment.shipment_id, "send", candidate.event, "sent", trigger.argv))
        elif email.mode != "execute" and trigger.ok:
            shipment.pending_event = candidate.event
            shipment.pending_event_at = shipment.pending_event_at or isoformat(now)
            last_sent[shipment.customer_email] = now
            results.append(CheckResult(shipment.shipment_id, "would_send", candidate.event, "dry_run", trigger.argv))
        else:
            shipment.pending_event = candidate.event
            shipment.pending_event_at = shipment.pending_event_at or isoformat(now)
            results.append(CheckResult(shipment.shipment_id, "retry", candidate.event, trigger.reason, trigger.argv))
    if persist:
        store.save()
    return results


def _collect(candidates: list[_Candidate], results: list[CheckResult], shipment: Shipment, observation) -> None:
    if observation.email_event:
        candidates.append(_Candidate(shipment, observation.email_event, observation.reason))
        return
    results.append(CheckResult(shipment.shipment_id, "ignore", None, observation.reason))


def _mark_sent(store: Store, shipment: Shipment, event: str, now: datetime) -> None:
    if event not in shipment.notified_events:
        shipment.notified_events.append(event)
    if shipment.pending_event == event:
        shipment.pending_event = None
        shipment.pending_event_at = None
    store.email_log.append(
        EmailLogEntry(
            customer_email=shipment.customer_email,
            shipment_id=shipment.shipment_id,
            event=event,
            sent_at=isoformat(now),
        )
    )


def _last_sent(store: Store) -> dict[str, datetime]:
    latest: dict[str, datetime] = {}
    for entry in store.email_log:
        moment = parse_time(entry.sent_at)
        if moment is None:
            continue
        current = latest.get(entry.customer_email)
        if current is None or moment > current:
            latest[entry.customer_email] = moment
    return latest


def _unique(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen
