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

        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._last_opened_at: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """Return current state, automatically transitioning OPEN → HALF_OPEN when cooldown expires."""
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_opened_at >= self.cooldown_seconds:
                logger.info(f"[CircuitBreaker:{self.name}] Cooldown elapsed — entering HALF_OPEN.")
                self._state = CircuitState.HALF_OPEN
        return self._state

    def call(self, func: Callable[[], Any]) -> Any:
        """
        Execute `func` if the circuit is CLOSED or HALF_OPEN.
        Raises CircuitBreakerOpenError immediately if the circuit is OPEN.
        """
        current_state = self.state

        if current_state == CircuitState.OPEN:
            remaining = self.cooldown_seconds - (time.monotonic() - self._last_opened_at)
            raise CircuitBreakerOpenError(
                f"Circuit breaker '{self.name}' is OPEN. "
                f"Fast-failing. Retry in {remaining:.0f}s."
            )

        try:
            result = func()
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure(exc)
            raise

    def status(self) -> Dict[str, Any]:
        """Return a dict snapshot of this breaker's current state — useful for diagnostics."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "last_opened_at": self._last_opened_at or None,
        }

    def reset(self) -> None:
        """Manually reset the breaker to CLOSED (useful in tests or admin tools)."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_opened_at = 0.0
        logger.info(f"[CircuitBreaker:{self.name}] Manually reset to CLOSED.")

    # ------------------------------------------------------------------
    # Internal state transitions
    # ------------------------------------------------------------------

    def _on_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            logger.info(f"[CircuitBreaker:{self.name}] Probe succeeded — closing circuit.")
        self._state = CircuitState.CLOSED
        self._failure_count = 0

    def _on_failure(self, exc: Exception) -> None:
        self._failure_count += 1
        logger.warning(
            f"[CircuitBreaker:{self.name}] Failure #{self._failure_count}/{self.failure_threshold}: {exc}"
        )

        if self._state == CircuitState.HALF_OPEN:
            # Probe failed — trip back to OPEN immediately
            self._trip()
        elif self._failure_count >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._last_opened_at = time.monotonic()
        logger.error(
            f"[CircuitBreaker:{self.name}] TRIPPED → OPEN. "
            f"Will attempt half-open probe in {self.cooldown_seconds}s."
        )
