"""Space carrier calls so a check cannot hammer FedEx, UPS, or USPS."""

from __future__ import annotations

from datetime import datetime, timedelta

from shipment_monitor.clock import Clock


class SpacingLimiter:
    def __init__(self, min_interval_seconds: float, clock: Clock) -> None:
        self.min_interval_seconds = min_interval_seconds
        self.clock = clock
        self._next_ok: dict[str, datetime] = {}

    def wait(self, key: str) -> None:
        if self.min_interval_seconds <= 0:
            return
        now = self.clock.now()
        nxt = self._next_ok.get(key)
        if nxt is not None and nxt > now:
            self.clock.sleep((nxt - now).total_seconds())
        self._next_ok[key] = self.clock.now() + timedelta(seconds=self.min_interval_seconds)


class RequestBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True
