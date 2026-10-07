"""Server clock used for sessions, nonces, and login limits."""

from __future__ import annotations

import time


class SystemClock:
    """Wall clock of the service process."""

    def now(self) -> float:
        return time.time()


class ManualClock:
    """Deterministic clock for tests. Runtime code uses SystemClock."""

    def __init__(self, now: float) -> None:
        self._now = now

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds
