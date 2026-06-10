"""Shared circuit breaker.

Lifted from the voice pipeline router so the same breaker guards LLM providers,
store-API clients, and voice pipelines instead of three bespoke implementations.

States: CLOSED (normal) → OPEN (tripped, route elsewhere) → HALF_OPEN (probe).
Time source is time.monotonic() (immune to wall-clock changes).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 3
    recovery_timeout: float = 60.0

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failures: int = field(default=0, init=False)
    _last_fail_at: float = field(default=0.0, init=False)
    _total_success: int = field(default=0, init=False)
    _total_fail: int = field(default=0, init=False)

    @property
    def state(self) -> CircuitState:
        if (
            self._state == CircuitState.OPEN
            and time.monotonic() - self._last_fail_at >= self.recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            logger.info("Circuit [%s] → HALF_OPEN (testing recovery)", self.name)
        return self._state

    def is_available(self) -> bool:
        return self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def record_success(self) -> None:
        self._total_success += 1
        if self._state == CircuitState.HALF_OPEN:
            logger.info("Circuit [%s] → CLOSED (recovered)", self.name)
        self._failures = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._total_fail += 1
        self._failures += 1
        self._last_fail_at = time.monotonic()
        if self._failures >= self.failure_threshold:
            if self._state != CircuitState.OPEN:
                logger.warning(
                    "Circuit [%s] → OPEN after %d failures", self.name, self._failures
                )
            self._state = CircuitState.OPEN

    def health(self) -> dict:
        s = self.state
        return {
            "state": s,
            "failures": self._failures,
            "total_success": self._total_success,
            "total_fail": self._total_fail,
            "threshold": self.failure_threshold,
            "recovery_in": round(
                max(0.0, self.recovery_timeout - (time.monotonic() - self._last_fail_at)), 1
            )
            if s == CircuitState.OPEN
            else 0.0,
        }
