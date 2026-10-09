"""Shared status names and shipment records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

PENDING = "pending"
UNKNOWN = "unknown"
SHIPPED = "shipped"
DELAYED = "delayed"
OUT_FOR_DELIVERY = "out_for_delivery"
DELIVERED = "delivered"

CUSTOMER_EVENTS = frozenset({SHIPPED, DELAYED, OUT_FOR_DELIVERY, DELIVERED})
IMMEDIATE_EVENTS = frozenset({SHIPPED, DELIVERED})
CARRIERS = frozenset({"fedex", "ups", "usps"})

# How far a package has progressed. Used only to reject stale regressions.
STATUS_RANK = {
    UNKNOWN: 0,
    PENDING: 1,
    SHIPPED: 2,
    DELAYED: 3,
    OUT_FOR_DELIVERY: 4,
    DELIVERED: 5,
}

# Observations that must not move a line backward.
# Delay can clear back to in-transit. Out for delivery can still become delayed.
BLOCKED_REGRESSIONS = {
    DELIVERED: frozenset({SHIPPED, DELAYED, OUT_FOR_DELIVERY, PENDING, UNKNOWN}),
    OUT_FOR_DELIVERY: frozenset({SHIPPED, PENDING, UNKNOWN}),
    SHIPPED: frozenset({PENDING, UNKNOWN}),
    DELAYED: frozenset({PENDING, UNKNOWN}),
}


def normalize_carrier(value: str) -> str:
    key = (value or "").strip().lower().replace(" ", "")
    aliases = {"federalexpress": "fedex", "fedexground": "fedex", "unitedstatespostalservice": "usps"}
    key = aliases.get(key, key)
    if key not in CARRIERS:
        raise ValueError(f"carrier must be one of {sorted(CARRIERS)}, got {value!r}")
    return key


@dataclass
class TrackingSnapshot:
    carrier: str
    tracking_number: str
    status: str
    raw_code: str = ""
    description: str = ""
    event_time: str | None = None
    source: str = "api"

    def __post_init__(self) -> None:
        self.carrier = normalize_carrier(self.carrier)
        self.tracking_number = self.tracking_number.strip()


@dataclass
class Shipment:
    shipment_id: str
    order_id: str
    customer_name: str
    customer_email: str
    company: str
    line: str
    device: str
    carrier: str
    tracking_number: str
    order_closed: bool = False
    ever_delivered: bool = False
    status: str = PENDING
    last_raw_code: str = ""
    last_description: str = ""
    last_checked_at: str | None = None
    last_event_at: str | None = None
    status_changed_at: str | None = None
    notified_events: list[str] = field(default_factory=list)
    pending_event: str | None = None
    pending_event_at: str | None = None

    def __post_init__(self) -> None:
        self.carrier = normalize_carrier(self.carrier) if self.carrier else self.carrier
        self.customer_email = self.customer_email.strip().lower()
        self.tracking_number = self.tracking_number.strip()

    def to_dict(self) -> dict:
        return {
            "shipment_id": self.shipment_id,
            "order_id": self.order_id,
            "customer_name": self.customer_name,
            "customer_email": self.customer_email,
            "company": self.company,
            "line": self.line,
            "device": self.device,
            "carrier": self.carrier,
            "tracking_number": self.tracking_number,
            "order_closed": self.order_closed,
            "ever_delivered": self.ever_delivered,
            "status": self.status,
            "last_raw_code": self.last_raw_code,
            "last_description": self.last_description,
            "last_checked_at": self.last_checked_at,
            "last_event_at": self.last_event_at,
            "status_changed_at": self.status_changed_at,
            "notified_events": list(self.notified_events),
            "pending_event": self.pending_event,
            "pending_event_at": self.pending_event_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Shipment:
        required = (
            "shipment_id",
            "order_id",
            "customer_name",
            "customer_email",
            "company",
            "line",
            "device",
            "carrier",
            "tracking_number",
        )
        missing = [key for key in required if not str(raw.get(key, "")).strip()]
        if missing:
            raise ValueError(f"shipment record missing {', '.join(missing)}")
        return cls(
            shipment_id=str(raw["shipment_id"]),
            order_id=str(raw["order_id"]),
            customer_name=str(raw["customer_name"]),
            customer_email=str(raw["customer_email"]),
            company=str(raw["company"]),
            line=str(raw["line"]),
            device=str(raw["device"]),
            carrier=str(raw["carrier"]),
            tracking_number=str(raw["tracking_number"]),
            order_closed=bool(raw.get("order_closed", False)),
            ever_delivered=bool(raw.get("ever_delivered", False)),
            status=str(raw.get("status") or PENDING),
            last_raw_code=str(raw.get("last_raw_code") or ""),
            last_description=str(raw.get("last_description") or ""),
            last_checked_at=raw.get("last_checked_at"),
            last_event_at=raw.get("last_event_at"),
            status_changed_at=raw.get("status_changed_at"),
            notified_events=list(raw.get("notified_events") or []),
            pending_event=raw.get("pending_event"),
            pending_event_at=raw.get("pending_event_at"),
        )


@dataclass
class EmailLogEntry:
    customer_email: str
    shipment_id: str
    event: str
    sent_at: str

    def to_dict(self) -> dict:
        return {
            "customer_email": self.customer_email,
            "shipment_id": self.shipment_id,
            "event": self.event,
            "sent_at": self.sent_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> EmailLogEntry:
        return cls(
            customer_email=str(raw["customer_email"]).strip().lower(),
            shipment_id=str(raw["shipment_id"]),
            event=str(raw["event"]),
            sent_at=str(raw["sent_at"]),
        )


@dataclass(frozen=True)
class Observation:
    """What changed, before the email admission rules run."""

    email_event: str | None
    reason: str


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def isoformat(moment: datetime) -> str:
    return moment.isoformat()
