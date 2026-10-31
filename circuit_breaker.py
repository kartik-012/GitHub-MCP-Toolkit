"""
Circuit Breaker Pattern — GitHub MCP Toolkit
=============================================
Implements a per-endpoint Circuit Breaker with three states:

    CLOSED  → Normal operation. Failures are counted.
    OPEN    → Fast-fail mode. All calls rejected for `cooldown_seconds`.
    HALF_OPEN → One probe call allowed. Success → CLOSED; failure → OPEN.

Usage (wraps any callable):
    cb = CircuitBreaker(name="github_api", failure_threshold=3, cooldown_seconds=60)
    result = cb.call(lambda: gh.get_repo("owner/repo"))

This ensures the MCP server never cascades GitHub API errors into the LLM
and provides graceful degradation with a structured error response.
"""

import time
import logging
import threading
from enum import Enum
from typing import Callable, Any, Dict

logger = logging.getLogger("github_mcp_toolkit")


class CircuitState(Enum):
    CLOSED = "closed"        # Normal — calls pass through
    OPEN = "open"            # Tripped — fast-fail, no calls made
    HALF_OPEN = "half_open"  # Recovery probe — one call allowed


class CircuitBreakerOpenError(Exception):
    """Raised when a call is rejected because the circuit is open."""
    pass


class ChaosInjector:
    """
    Deterministic Chaos & Fault-Injection Resilience Harness.
    Allows tests and resilience drills to inject controlled upstream GitHub API faults
    (HTTP 502 Bad Gateway, HTTP 429 Secondary Rate Limit, HTTP 504 Gateway Timeout,
    and synthetic latency jitter) directly inside `CircuitBreaker.call()`.
    """

    _lock = threading.Lock()
    _enabled: bool = False
    _fault_type: str = "502_bad_gateway"
    _remaining_faults: int = 0
    _latency_ms: float = 0.0
    _injected_count: int = 0

    FAULT_MESSAGES = {
        "502_bad_gateway": "Chaos Fault Injection: HTTP 502 Bad Gateway from upstream GitHub API",
        "429_secondary_rate_limit": "Chaos Fault Injection: HTTP 429 Secondary Rate Limit Exceeded (Retry-After: 60s)",
        "504_gateway_timeout": "Chaos Fault Injection: HTTP 504 Gateway Timeout",
    }

    @classmethod
    def configure(
        cls,
        enabled: bool = True,
        fault_type: str = "502_bad_gateway",
        remaining_faults: int = 1,
        latency_ms: float = 0.0,
    ) -> None:
        with cls._lock:
            cls._enabled = enabled
            cls._fault_type = fault_type
            cls._remaining_faults = max(0, int(remaining_faults))
            cls._latency_ms = max(0.0, float(latency_ms))
            cls._injected_count = 0

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._enabled = False
            cls._remaining_faults = 0
            cls._latency_ms = 0.0
            cls._injected_count = 0

    @classmethod
    def status(cls) -> Dict[str, Any]:
        with cls._lock:
            return {
                "enabled": cls._enabled,
                "fault_type": cls._fault_type,
                "remaining_faults": cls._remaining_faults,
                "latency_ms": cls._latency_ms,
                "injected_count": cls._injected_count,
            }

    @classmethod
    def maybe_inject_fault(cls) -> None:
        delay_sec = 0.0
        should_raise = False
        fault_kind = "502_bad_gateway"
        with cls._lock:
            if not cls._enabled or cls._remaining_faults <= 0:
                return
            cls._remaining_faults -= 1
            cls._injected_count += 1
            delay_sec = cls._latency_ms / 1000.0
            should_raise = True
            fault_kind = cls._fault_type
            if cls._remaining_faults == 0:
                cls._enabled = False

        if delay_sec > 0:
            time.sleep(delay_sec)
        if should_raise:
            msg = cls.FAULT_MESSAGES.get(fault_kind, f"Chaos Fault Injection: {fault_kind}")
            if fault_kind == "504_gateway_timeout":
                raise TimeoutError(msg)
            raise RuntimeError(msg)


class CircuitBreaker:
    """
    Thread-safe (single-process) per-endpoint circuit breaker.

    Parameters
    ----------
    name : str
        Identifier for this breaker (used in logs and error messages).
    failure_threshold : int
        Number of consecutive failures before the circuit opens. Default: 3.
    cooldown_seconds : float
        Duration the circuit stays open before attempting a half-open probe. Default: 60.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds

        self._lock = threading.RLock()
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._last_opened_at: float = 0.0
        self._probe_in_flight: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """Return current state, automatically transitioning OPEN → HALF_OPEN when cooldown expires."""
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_opened_at >= self.cooldown_seconds:
                    logger.info(f"[CircuitBreaker:{self.name}] Cooldown elapsed — entering HALF_OPEN.")
                    self._state = CircuitState.HALF_OPEN
                    self._probe_in_flight = False
            return self._state

    @property
    def is_available(self) -> bool:
        """Return True if the circuit breaker can accept calls (CLOSED or HALF_OPEN)."""
        return self.state != CircuitState.OPEN

    def call(self, func: Callable[[], Any]) -> Any:
        """
        Execute `func` if the circuit is CLOSED or HALF_OPEN (single probe).
        Raises CircuitBreakerOpenError immediately if the circuit is OPEN or a HALF_OPEN probe is already in flight.
        """
        with self._lock:
            current_state = self.state

            if current_state == CircuitState.OPEN:
                remaining = max(0.0, self.cooldown_seconds - (time.monotonic() - self._last_opened_at))
                raise CircuitBreakerOpenError(
                    f"Circuit breaker '{self.name}' is OPEN. "
                    f"Fast-failing. Retry in {remaining:.0f}s."
                )

            if current_state == CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker '{self.name}' is HALF_OPEN with a recovery probe already in flight."
                    )
                self._probe_in_flight = True

        try:
            ChaosInjector.maybe_inject_fault()
            result = func()
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure(exc)
            raise

    def get_cooldown_remaining(self) -> float:
        """Return the seconds remaining in the open cooldown period, or 0.0 if not open."""
        with self._lock:
            if self._state != CircuitState.OPEN or self._last_opened_at == 0.0:
                return 0.0
            return max(0.0, round(self.cooldown_seconds - (time.monotonic() - self._last_opened_at), 2))

    def status(self) -> Dict[str, Any]:
        """Return a dict snapshot of this breaker's current state — useful for diagnostics."""
        with self._lock:
            return {
                "name": self.name,
                "state": self.state.value,
                "is_available": self.is_available,
                "failure_count": self._failure_count,
                "failure_threshold": self.failure_threshold,
                "cooldown_seconds": self.cooldown_seconds,
                "cooldown_remaining_seconds": self.get_cooldown_remaining(),
                "last_opened_at": self._last_opened_at or None,
            }

    def reset(self) -> None:
        """Manually reset the breaker to CLOSED (useful in tests or admin tools)."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_opened_at = 0.0
            self._probe_in_flight = False
            logger.info(f"[CircuitBreaker:{self.name}] Manually reset to CLOSED.")

    def __repr__(self) -> str:
        """String representation of the CircuitBreaker instance and current state."""
        return (
            f"<CircuitBreaker name={self.name!r} state={self.state.value} "
            f"failures={self._failure_count}/{self.failure_threshold}>"
        )

    # ------------------------------------------------------------------
    # Internal state transitions
    # ------------------------------------------------------------------

    def _on_success(self) -> None:
        """Handle a successful call execution by resetting failures and closing circuit."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.info(f"[CircuitBreaker:{self.name}] Probe succeeded — closing circuit.")
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._probe_in_flight = False

    def _on_failure(self, exc: Exception) -> None:
        """Record an operation failure and trip the circuit if threshold is reached."""
        with self._lock:
            self._failure_count += 1
            self._probe_in_flight = False
            logger.warning(
                f"[CircuitBreaker:{self.name}] Failure #{self._failure_count}/{self.failure_threshold}: {exc}"
            )

            if self._state == CircuitState.HALF_OPEN:
                # Probe failed — trip back to OPEN immediately
                self._trip()
            elif self._failure_count >= self.failure_threshold:
                self._trip()

    def _trip(self) -> None:
        """Transition circuit breaker state to OPEN and initiate cooldown timer."""
        with self._lock:
            self._state = CircuitState.OPEN
            self._last_opened_at = time.monotonic()
            self._probe_in_flight = False
            logger.error(
                f"[CircuitBreaker:{self.name}] TRIPPED → OPEN. "
                f"Will attempt half-open probe in {self.cooldown_seconds}s."
            )
