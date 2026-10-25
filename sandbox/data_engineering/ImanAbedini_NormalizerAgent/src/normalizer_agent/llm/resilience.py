"""Circuit breaker (per provider) and token-bucket rate limiter."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Closed -> (N consecutive failures) -> open -> (cooldown) -> half-open.

    A success while half-open closes the circuit; a failure re-opens it.
    Permanent errors keep the circuit open for ``permanent_cooldown_s``.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_s: float = 60.0,
        permanent_cooldown_s: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.permanent_cooldown_s = permanent_cooldown_s
        self._clock = clock
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self._opened_at: float | None = None
        self._cooldown: float = cooldown_s

    def allow(self) -> bool:
        if self.state is CircuitState.CLOSED:
            return True
        if self.state is CircuitState.OPEN:
            assert self._opened_at is not None
            if self._clock() - self._opened_at >= self._cooldown:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True  # half_open lets one probe through

    def record_success(self) -> None:
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self._opened_at = None

    def record_failure(self, *, permanent: bool = False) -> None:
        self.consecutive_failures += 1
        if self.state is CircuitState.HALF_OPEN or permanent:
            self._trip(self.permanent_cooldown_s if permanent else self.cooldown_s)
            return
        if self.consecutive_failures >= self.failure_threshold:
            self._trip(self.cooldown_s)

    def _trip(self, cooldown: float) -> None:
        self.state = CircuitState.OPEN
        self._cooldown = cooldown
        self._opened_at = self._clock()

    def describe(self) -> str:
        return f"{self.state.value}(failures={self.consecutive_failures})"


class RateLimiter:
    """Simple sliding-window limiter: at most ``requests_per_minute`` starts."""

    def __init__(
        self,
        requests_per_minute: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rpm = requests_per_minute
        self.window = 60.0
        self._starts: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._clock = clock

    async def acquire(self) -> None:
        if self.rpm <= 0:
            return
        while True:
            async with self._lock:
                now = self._clock()
                while self._starts and now - self._starts[0] >= self.window:
                    self._starts.popleft()
                if len(self._starts) < self.rpm:
                    self._starts.append(now)
                    return
                wait = self.window - (now - self._starts[0])
            await asyncio.sleep(max(wait, 0.01))
