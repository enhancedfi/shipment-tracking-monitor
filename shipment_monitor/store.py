"""JSON store for shipments and the customer email log."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from shipment_monitor.models import EmailLogEntry, Shipment

# Set on the process that holds the flock so a child it spawns does not
# flock the same file and deadlock against its parent.
LOCK_HELD_ENV = "SHIPMENT_STORE_LOCK_HELD"


def lockfile_for(store_path: Path) -> Path:
    """Exclusive lock sits beside the store, not on the file ``save`` replaces."""
    return store_path.with_name(store_path.name + ".lock")


@contextmanager
def exclusive_store_lock(store_path: Path) -> Iterator[None]:
    """Hold an exclusive flock for one whole run.

    The second run waits until this one finishes. Load the store after
    entering and save it before leaving: the 6-hour throttle check and the
    store write then cannot interleave with another run. A child started
    while the lock is held sees ``SHIPMENT_STORE_LOCK_HELD`` and does not
    take the flock again.
    """
    if os.environ.get(LOCK_HELD_ENV) == "1":
        yield
        return
    path = lockfile_for(store_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.environ[LOCK_HELD_ENV] = "1"
        try:
            yield
        finally:
            os.environ.pop(LOCK_HELD_ENV, None)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
