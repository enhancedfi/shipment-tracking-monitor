"""Map FedEx, UPS, and USPS payloads onto one status vocabulary.

Delivered wins over every earlier scan in the same payload. A history that
contains both a pickup and a delivery is a delivered package, so the pickup
cannot become a shipped email later.
"""

from __future__ import annotations

from shipment_monitor.models import (
    DELAYED,
    DELIVERED,
    OUT_FOR_DELIVERY,
    PENDING,
    SHIPPED,
    UNKNOWN,
    TrackingSnapshot,
    normalize_carrier,
)

FEDEX_CODES = {
    "OC": PENDING,
    "PU": SHIPPED,
    "PX": SHIPPED,
    "AR": SHIPPED,
    "DP": SHIPPED,
    "IT": SHIPPED,
    "IX": SHIPPED,
    "AF": SHIPPED,
    "OD": OUT_FOR_DELIVERY,
    "DL": DELIVERED,
    "DE": DELAYED,
    "SE": DELAYED,
    "DY": DELAYED,
    "CD": DELAYED,
    "HL": DELAYED,
    "CA": DELAYED,
    "RS": DELAYED,
}

UPS_TYPES = {
    "M": PENDING,
    "MV": PENDING,
    "P": SHIPPED,
    "I": SHIPPED,
    "X": DELAYED,
    "D": DELIVERED,
}

USPS_CATEGORIES = {
    "pre-shipment": PENDING,
    "accepted": SHIPPED,
    "in transit": SHIPPED,
    "in-transit": SHIPPED,
    "out for delivery": OUT_FOR_DELIVERY,
    "available for pickup": OUT_FOR_DELIVERY,
    "delivered": DELIVERED,
    "alert": DELAYED,
    "delivery attempt": DELAYED,
}


def classify_text(code: str, description: str) -> str:
    """Keyword fallback used when a carrier sends an unfamiliar code."""
    folded = f"{code} {description}".lower()
    if any(word in folded for word in ("delivered", "delivery complete")):
        return DELIVERED
    if "out for delivery" in folded or "on vehicle for delivery" in folded:
        return OUT_FOR_DELIVERY
    if any(word in folded for word in ("delay", "delayed", "exception", "weather", "hold", "unable")):
        return DELAYED
    if any(word in folded for word in ("picked up", "in transit", "accepted", "departed", "arrived", "on the way")):
        return SHIPPED
    if any(word in folded for word in ("label created", "shipment information sent", "pre-shipment", "manifest")):
        return PENDING
    return UNKNOWN


def coerce_status(latest: str, seen: list[str]) -> str:
    """If any scan says delivered, the package is delivered.

    Carrier histories list the original pickup beside the delivery. Treating
    the pickup as the current status is what sends a shipped email for a
    phone that already arrived.
    """
    if latest == DELIVERED or DELIVERED in seen:
        return DELIVERED
    return latest


def _snapshot(
    carrier: str,
    tracking_number: str,
    status: str,
    raw_code: str,
    description: str,
    event_time: str | None,
    source: str,
    seen: list[str],
) -> TrackingSnapshot:
    return TrackingSnapshot(
        carrier=carrier,
        tracking_number=tracking_number or "",
        status=coerce_status(status, seen),
        raw_code=raw_code,
        description=description,
        event_time=event_time,
        source=source,
    )


def parse_fedex_body(body: dict, source: str = "api") -> list[TrackingSnapshot]:
    results = body.get("output", body).get("completeTrackResults", [])
    snapshots: list[TrackingSnapshot] = []
    for result in results:
        number = str(result.get("trackingNumber") or "")
        for track in result.get("trackResults") or [{}]:
            if track.get("error"):
                message = track["error"].get("message") or track["error"].get("code") or "not found"
                snapshots.append(
                    TrackingSnapshot(
                        carrier="fedex",
                        tracking_number=number,
                        status=UNKNOWN,
                        raw_code=str(track["error"].get("code") or ""),
                        description=str(message),
                        source=source,
                    )
                )
                continue
            latest = track.get("latestStatusDetail") or {}
            scans = track.get("scanEvents") or []
            seen = [FEDEX_CODES.get(str(scan.get("eventType") or scan.get("code") or ""), UNKNOWN) for scan in scans]
            code = str(latest.get("code") or latest.get("eventType") or "")
            description = str(latest.get("statusByLocale") or latest.get("description") or "")
            status = FEDEX_CODES.get(code) or classify_text(code, description)
            if not code and scans:
                first = scans[0]
                code = str(first.get("eventType") or "")
                description = str(first.get("eventDescription") or first.get("description") or description)
                status = FEDEX_CODES.get(code) or classify_text(code, description)
            event_time = _fedex_time(latest) or (_fedex_time(scans[0]) if scans else None)
            info = track.get("trackingNumberInfo") or {}
            tracking = str(info.get("trackingNumber") or number)
            snapshots.append(_snapshot("fedex", tracking, status, code, description, event_time, source, seen))
    return snapshots


def _fedex_time(node: dict) -> str | None:
    date = node.get("date") or node.get("scanDate") or ""
    clock = node.get("time") or node.get("scanTime") or ""
    if date and clock:
        return f"{date}T{clock}"
    return date or None


def parse_ups_body(body: dict, tracking_hint: str = "", source: str = "api") -> list[TrackingSnapshot]:
    shipments = (body.get("trackResponse") or {}).get("shipment") or []
    snapshots: list[TrackingSnapshot] = []
    if not shipments:
        snapshots.append(
            TrackingSnapshot(
                carrier="ups",
                tracking_number=tracking_hint,
                status=UNKNOWN,
                description="no shipment in UPS response",
                source=source,
            )
        )
        return snapshots
    for shipment in shipments:
        packages = shipment.get("package") or [shipment]
        for package in packages:
            number = str(package.get("trackingNumber") or shipment.get("inquiryNumber") or tracking_hint)
            warnings = package.get("warnings") or shipment.get("warnings") or []
            activities = package.get("activity") or []
            if warnings and not activities:
                message = warnings[0].get("message") or warnings[0].get("code") or "not found"
                snapshots.append(
                    TrackingSnapshot(
                        carrier="ups",
                        tracking_number=number,
                        status=UNKNOWN,
                        raw_code=str(warnings[0].get("code") or ""),
                        description=str(message),
                        source=source,
                    )
                )
                continue
            seen: list[str] = []
            for activity in activities:
                status_node = activity.get("status") or {}
                seen.append(_ups_status(status_node))
            newest = activities[0] if activities else {}
            status_node = newest.get("status") or {}
            status = _ups_status(status_node) if activities else UNKNOWN
            description = str(status_node.get("description") or "")
            code = str(status_node.get("code") or status_node.get("type") or "")
            event_time = None
            if newest.get("gmtDate") and newest.get("gmtTime"):
                event_time = f"{newest['gmtDate']}T{newest['gmtTime']}"
            elif newest.get("date"):
                event_time = str(newest.get("date"))
            snapshots.append(_snapshot("ups", number, status, code, description, event_time, source, seen))
    return snapshots


def _ups_status(status_node: dict) -> str:
    description = str(status_node.get("description") or "")
    code = str(status_node.get("code") or "")
    kind = str(status_node.get("type") or "")
    text = classify_text(code, description)
    if text in {DELIVERED, OUT_FOR_DELIVERY, DELAYED}:
        return text
    return UPS_TYPES.get(kind, text)


def parse_usps_body(body: dict, tracking_hint: str = "", source: str = "api") -> list[TrackingSnapshot]:
    number = str(body.get("trackingNumber") or tracking_hint)
    if body.get("error") or body.get("apiVersion") and body.get("error"):
        message = (body.get("error") or {}).get("message") or "usps error"
        return [
            TrackingSnapshot(
                carrier="usps",
                tracking_number=number,
                status=UNKNOWN,
                description=str(message),
                source=source,
            )
        ]
    category = str(body.get("statusCategory") or "")
    description = str(body.get("status") or body.get("statusSummary") or category)
    status = USPS_CATEGORIES.get(category.strip().lower()) or classify_text(category, description)
    events = body.get("trackingEvents") or []
    seen = []
    for event in events:
        event_type = str(event.get("eventType") or "")
        seen.append(USPS_CATEGORIES.get(event_type.strip().lower()) or classify_text(event_type, event_type))
    event_time = None
    if events:
        event_time = events[0].get("eventTimestamp")
    return [_snapshot("usps", number, status, category, description, event_time, source, seen)]


def parse_carrier_body(carrier: str, body: dict, tracking_hint: str = "", source: str = "api") -> list[TrackingSnapshot]:
    name = normalize_carrier(carrier)
    if name == "fedex":
        return parse_fedex_body(body, source=source)
    if name == "ups":
        return parse_ups_body(body, tracking_hint=tracking_hint, source=source)
    return parse_usps_body(body, tracking_hint=tracking_hint, source=source)


def snapshot_from_webhook(payload: dict) -> TrackingSnapshot:
    """Accept either a carrier body or the simplified webhook envelope.

    Simplified envelope:
    {"carrier", "tracking_number", "status_code", "status_description", "event_time"}
    Carrier body:
    {"carrier", "tracking_number", "carrier_payload": { ...official JSON... }}
    """
    if "carrier_payload" in payload:
        parsed = parse_carrier_body(
            str(payload["carrier"]),
            payload["carrier_payload"],
            tracking_hint=str(payload.get("tracking_number") or ""),
            source="webhook",
        )
        if not parsed:
            raise ValueError("carrier payload did not contain a tracking result")
        return parsed[0]
    carrier = normalize_carrier(str(payload["carrier"]))
    code = str(payload.get("status_code") or "")
    description = str(payload.get("status_description") or payload.get("status") or "")
    if carrier == "fedex":
        status = FEDEX_CODES.get(code) or classify_text(code, description)
    elif carrier == "ups":
        status = _ups_status({"type": code, "code": code, "description": description})
    else:
        status = USPS_CATEGORIES.get(code.strip().lower()) or USPS_CATEGORIES.get(description.strip().lower()) or classify_text(code, description)
    seen = [status]
    return _snapshot(
        carrier,
        str(payload.get("tracking_number") or ""),
        status,
        code,
        description,
        payload.get("event_time"),
        "webhook",
        seen,
    )
