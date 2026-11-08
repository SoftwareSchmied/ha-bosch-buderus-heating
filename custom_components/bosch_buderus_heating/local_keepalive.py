"""Connection reuse to the local gateway with a conservative fallback (G4).

A read normally reuses a kept, already pinned connection. When reused
connections misbehave (closed or reset before an answer, no answer in time,
a broken answer), the access falls back to a new connection per read, the
behaviour before keep-alive, for a while and then tries again.

The counters also measure whether two parallel reads are still needed (O56).
Everything here is a fixed code, a count or a duration: no addresses, paths,
values or credentials.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, deque
from collections.abc import Callable
from enum import StrEnum
from time import monotonic

# This many failures on reused connections within the window switch to a new
# connection per read. A healthy keep-alive has none; a gateway restart costs
# at most one per kept connection (two), so it alone never switches.
REUSE_FAILURE_LIMIT = 3
REUSE_FAILURE_WINDOW = 900.0
# How long one fallback lasts; a repeated fallback lasts longer, up to a day.
FALLBACK_SECONDS = (6 * 3600.0, 12 * 3600.0, 24 * 3600.0)
# Response times per connection kind kept for the diagnostics.
CONNECTION_SAMPLES = 256

_LOGGER = logging.getLogger(__name__)


class ReuseFailure(StrEnum):
    CLOSED = "closed"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"


class ConnectionKind(StrEnum):
    NEW = "new"
    REUSED = "reused"


def _times(samples: deque[float]) -> dict[str, object]:
    ordered = sorted(samples)
    result: dict[str, object] = {"samples": len(ordered)}
    if ordered:
        result |= {
            "median_ms": round(ordered[(len(ordered) - 1) // 2], 1),
            "p95_ms": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 1),
            "maximum_ms": round(ordered[-1], 1),
        }
    return result


class ConnectionReuse:
    """Decide per read whether kept connections may be used, and count."""

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self._recent: deque[float] = deque()
        self._fallback_until: float | None = None
        self._fallback_reason: ReuseFailure | None = None
        self._fallbacks = 0
        self._logged = False
        self._connections: Counter[ConnectionKind] = Counter()
        self._failures: Counter[ReuseFailure] = Counter()
        self._closed_by_gateway = 0
        self._overlapping = 0
        self._times: dict[ConnectionKind, deque[float]] = {
            kind: deque(maxlen=CONNECTION_SAMPLES) for kind in ConnectionKind
        }

    def _fallback_active(self, now: float) -> bool:
        return self._fallback_until is not None and now < self._fallback_until

    @property
    def keep_alive(self) -> bool:
        """Whether the next read may use (and keep) an open connection."""
        if self._fallback_until is None:
            return True
        if self._fallback_active(self._clock()):
            return False
        self._fallback_until = None
        _LOGGER.debug("Local readings keep connections open again")
        return True

    def connected(self, kind: ConnectionKind) -> None:
        self._connections[kind] += 1

    def overlapping(self) -> None:
        """A read started while another one was still running."""
        self._overlapping += 1

    def answered(self, *, closes: bool) -> None:
        """A kept-alive response; ``closes`` when the gateway ends the connection."""
        if closes:
            self._closed_by_gateway += 1

    def timed(self, kind: ConnectionKind, milliseconds: float) -> None:
        self._times[kind].append(milliseconds)

    def failed(self, failure: ReuseFailure) -> None:
        """A reused connection failed; switch after repeated failures."""
        now = self._clock()
        self._failures[failure] += 1
        if self._fallback_active(now):
            return  # A read that was already running when the fallback began.
        while self._recent and now - self._recent[0] > REUSE_FAILURE_WINDOW:
            self._recent.popleft()
        self._recent.append(now)
        if len(self._recent) < REUSE_FAILURE_LIMIT:
            return
        self._recent.clear()
        duration = FALLBACK_SECONDS[min(self._fallbacks, len(FALLBACK_SECONDS) - 1)]
        self._fallbacks += 1
        self._fallback_until = now + duration
        self._fallback_reason = failure
        log = _LOGGER.debug if self._logged else _LOGGER.warning
        self._logged = True
        log(
            "The gateway does not keep local connections open reliably (%s); "
            "local readings use a new connection per read for %d h",
            failure.value,
            round(duration / 3600),
        )

    def diagnostics(self, tls_inspections: int) -> dict[str, object]:
        """Counts since the access started, for the keep-alive practice test."""
        now = self._clock()
        measured = max(0.0, now - self._started)
        handshakes = tls_inspections + self._connections[ConnectionKind.NEW]
        active = self._fallback_active(now)
        return {
            "mode": "keep_alive" if not active else "new_connection_per_read",
            "measured_seconds": round(measured),
            "tls_handshakes": handshakes,
            "tls_handshakes_per_hour": round(handshakes * 3600 / measured, 1)
            if measured >= 60
            else None,
            "new_connections": self._connections[ConnectionKind.NEW],
            "reused_connections": self._connections[ConnectionKind.REUSED],
            "closed_by_gateway": self._closed_by_gateway,
            "reuse_failures": {
                failure.value: self._failures[failure]
                for failure in ReuseFailure
                if self._failures[failure]
            },
            "fallback_active": active,
            "fallback_reason": self._fallback_reason.value
            if self._fallback_reason is not None
            else None,
            "fallback_remaining_seconds": round(self._fallback_until - now)
            if active and self._fallback_until is not None
            else 0,
            "fallbacks": self._fallbacks,
            "overlapping_reads": self._overlapping,
            "response_times_by_connection": {
                kind.value: _times(self._times[kind]) for kind in ConnectionKind
            },
        }
