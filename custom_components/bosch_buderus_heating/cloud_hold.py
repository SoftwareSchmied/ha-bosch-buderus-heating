"""Hold the last values of cloud-dependent entities over a short cloud blip.

A cloud refresh that fails once or twice (a slow sign-in service, a gateway
timeout, a server error) used to turn every control entity and every
calculated value into ``unavailable`` for a minute, with a gap in the long-term
statistics and an "unavailable" trigger for automations, although nothing
changed at the plant (H-B3-2).

The coordinator therefore keeps this state: while at most ``GRACE_CYCLES``
refreshes in a row failed, and the outage began less than ``GRACE_MAX_SECONDS``
ago, entities that need a current cloud cycle show their last values instead.
Afterwards they are ``unavailable`` as before. The hold only concerns the
display. Every write still reads the setting again from the gateway and is
refused without a current reading (``coordinator.control_status`` and the write
transaction use the strict state, never this one).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Final

from .pointt.redaction import resource_path_template

# Failed refreshes in a row whose values are still shown: one more failed
# refresh ends the hold. With the one-minute refresh that is about three
# minutes; the cloud protection pause opens after the same three failures.
GRACE_CYCLES: Final = 3
# An outage never holds longer than this, however few refreshes were counted
# (a long-running refresh, a profile with fewer wake-ups). The same length as
# the grace before a lasting outage is logged as an error (#60).
GRACE_MAX_SECONDS: Final = 5 * 60.0
# Entries of the report of the last failed refresh.
LAST_FAILED_PATHS: Final = 10


class CloudHold:
    """The failed refreshes of the current outage, counted for the hold."""

    __slots__ = (
        "_cancelled",
        "_counted_expiry",
        "expired",
        "failures",
        "since",
        "started",
    )

    def __init__(self) -> None:
        self.failures = 0
        self.since: float | None = None
        self._cancelled = False
        self._counted_expiry = False
        # Holds begun, and holds that ended by their limit without recovery.
        self.started = 0
        self.expired = 0

    def failed(self, now: float) -> None:
        """Count one failed refresh (the cloud did not answer usably)."""
        if self.since is None:
            self.since = now
            self.started += 1
        self.failures += 1
        self._count_expiry(now)

    def cancel(self) -> None:
        """No hold for the rest of this outage (an unexpected error)."""
        self._cancelled = True

    def recovered(self) -> None:
        """A refresh answered: the next failure starts a new outage."""
        self.failures = 0
        self.since = None
        self._cancelled = False
        self._counted_expiry = False

    def holding(self, now: float) -> bool:
        """Whether the last values are shown for the current failed refresh."""
        if self.since is None or self._cancelled or self.failures < 1:
            return False
        if self.failures > GRACE_CYCLES or now - self.since > GRACE_MAX_SECONDS:
            self._count_expiry(now)
            return False
        return True

    def _count_expiry(self, now: float) -> None:
        if (
            not self._counted_expiry
            and self.since is not None
            and (self.failures > GRACE_CYCLES or now - self.since > GRACE_MAX_SECONDS)
        ):
            self._counted_expiry = True
            self.expired += 1

    def diagnostics(self, now: float) -> dict[str, object]:
        """Counts and fixed limits only."""
        return {
            "grace_cycles": GRACE_CYCLES,
            "grace_max_seconds": GRACE_MAX_SECONDS,
            "holding": self.holding(now),
            "failed_cycles_in_a_row": self.failures,
            "holds_started": self.started,
            "holds_expired": self.expired,
        }


class LastFailedRefresh:
    """Which readings failed in the last refresh that had failures (F).

    Only path templates (no identifiers), the HTTP status and a fixed category
    are kept, so a short unexplained outage can be traced afterwards.
    """

    __slots__ = ("_report",)

    def __init__(self) -> None:
        self._report: dict[str, object] | None = None

    def observe(
        self,
        failures: Iterable[tuple[str, int | None, str]],
        *,
        cycle_failed: bool,
        category: str | None,
        now: datetime | None = None,
    ) -> None:
        """Keep this refresh's failed readings; nothing when none failed."""
        counts: dict[tuple[str, int | None, str], int] = {}
        for path, status, result in failures:
            key = (resource_path_template(path), status, result)
            counts[key] = counts.get(key, 0) + 1
        if not counts:
            return
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0][0]))
        self._report = {
            "at": (now or datetime.now(UTC)).replace(microsecond=0).isoformat(),
            "cycle_failed": cycle_failed,
            "category": category,
            "failed_readings": sum(counts.values()),
            "paths": [
                {
                    "path": path,
                    "status": status,
                    "category": result,
                    "count": count,
                }
                for (path, status, result), count in ordered[:LAST_FAILED_PATHS]
            ],
            "paths_omitted": max(0, len(ordered) - LAST_FAILED_PATHS),
        }

    def diagnostics(self) -> dict[str, object] | None:
        return self._report
