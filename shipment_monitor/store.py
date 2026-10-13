"""JSON store for shipments and the customer email log."""

from __future__ import annotations

import json
import os
from pathlib import Path

from shipment_monitor.models import EmailLogEntry, Shipment


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.shipments: list[Shipment] = []
        self.email_log: list[EmailLogEntry] = []

    def load(self) -> Store:
        if not self.path.exists():
            self.shipments = []
            self.email_log = []
            return self
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.shipments = [Shipment.from_dict(item) for item in raw.get("shipments", [])]
        self.email_log = [EmailLogEntry.from_dict(item) for item in raw.get("email_log", [])]
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "shipments": [item.to_dict() for item in self.shipments],
            "email_log": [item.to_dict() for item in self.email_log],
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def upsert(self, incoming: list[Shipment]) -> None:
        """Insert new lines and refresh descriptive fields on existing ones.

        A line that has already been delivered stays delivered. A closed order
        stays closed. Importing a spreadsheet refresh must not clear those guards.
        """
        by_id = {item.shipment_id: item for item in self.shipments}
        for item in incoming:
            current = by_id.get(item.shipment_id)
            if current is None:
                self.shipments.append(item)
                by_id[item.shipment_id] = item
                continue
            current.order_id = item.order_id
            current.customer_name = item.customer_name
            current.customer_email = item.customer_email
            current.company = item.company
            current.line = item.line
            current.device = item.device
            current.carrier = item.carrier
            current.tracking_number = item.tracking_number
            if item.lookup_email:
                current.lookup_email = item.lookup_email
            if item.lookup_zip:
                current.lookup_zip = item.lookup_zip
            if item.estimated_delivery:
                current.estimated_delivery = item.estimated_delivery
            if item.order_closed:
                current.order_closed = True
        self.shipments.sort(key=lambda shipment: shipment.shipment_id)
