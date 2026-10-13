"""JSON store for shipments and the customer email log."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from shipment_monitor.models import EmailLogEntry, Shipment

# Customer names, emails, and phone lines. Created with this mode; not chmod after.
PRIVATE_MODE = 0o600
# A second run waits, then skips. Long enough for one full page cycle.
LOCK_TIMEOUT_SECONDS = 20 * 60

# In-process re-entry only. A child process does not inherit this set, and an
# environment variable cannot add to it.
_held_locks: set[str] = set()


class StoreLockTimeout(Exception):
    """The store lock was still held when the wait ended. The run should skip."""


def lockfile_for(store_path: Path) -> Path:
    """Exclusive lock sits beside the store, not on the file ``save`` replaces."""
    return store_path.with_name(store_path.name + ".lock")


def write_private_text(path: Path, text: str) -> None:
    """Create ``path`` as a new 0600 file and publish it with ``os.replace``.

    The mode is the ``os.open`` mode. Nothing chmods the file afterward.
    ``O_NOFOLLOW`` refuses to write through a symlink.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(temporary, flags, PRIVATE_MODE)
    try:
        payload = text.encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(f"could not write {temporary}")
            view = view[written:]
    except Exception:
        os.close(fd)
        temporary.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(temporary, path)


@contextmanager
def exclusive_store_lock(store_path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold an exclusive flock for one store command.

    Load the store after entering and save it before leaving, so the throttle
    check and the store write cannot interleave with another run. A second run
    waits up to ``timeout`` seconds (20 minutes by default). If the lock is
    still held, this logs and raises ``StoreLockTimeout`` so the caller skips.
    Re-entry in this process uses ``_held_locks``. An environment variable
    cannot grant that.
    """
    key = str(Path(store_path).resolve())
    if key in _held_locks:
        yield
        return
    path = lockfile_for(store_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, PRIVATE_MODE)
    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    message = f"ALERT: skipping run; lock still held: {path} after {timeout:.0f}s"
                    print(message, file=sys.stderr)
                    raise StoreLockTimeout(message)
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        _held_locks.add(key)
        try:
            yield
        finally:
            _held_locks.discard(key)
    finally:
        if acquired:
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
        payload = {
            "shipments": [item.to_dict() for item in self.shipments],
            "email_log": [item.to_dict() for item in self.email_log],
        }
        write_private_text(self.path, json.dumps(payload, indent=2) + "\n")

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
