"""Clocks so checks and rate limits stay deterministic in tests."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock(Clock):
    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware datetime")
        self._now = now

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now = self._now + timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)
