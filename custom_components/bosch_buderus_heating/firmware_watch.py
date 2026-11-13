"""Notice a firmware change while running and check the capabilities again (P22).

Discovery runs once per load. A gateway or appliance can update its firmware
while Home Assistant keeps running; new or changed capabilities would then stay
unnoticed until the next restart. The firmware versions are read with the slow
poll group, and a change leads to one bounded discovery after the next working
cycle. Parts missing afterwards are handled by the search from P46, changed
shapes by the capability check and the entities' own contract checks.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from homeassistant.util import dt as dt_util

from .pointt import Resource
from .pointt.redaction import resource_path_template
from .resource_catalog import FIRMWARE_PATHS

_LOGGER = logging.getLogger(__name__)
_MAX_VERSION_LENGTH = 64


def firmware_values(resources: Mapping[str, Resource]) -> dict[str, str]:
    """The reported firmware versions, only well-formed short strings."""
    values: dict[str, str] = {}
    for path in FIRMWARE_PATHS:
        resource = resources.get(path)
        if resource is None or not resource.has_value:
            continue
        value = resource.value
        if (
            isinstance(value, str)
            and 0 < len(value) <= _MAX_VERSION_LENGTH
            and value.isprintable()
        ):
            values[path] = value
    return values


@dataclass(slots=True)
class FirmwareWatch:
    """Last seen versions and the discovery a change triggers."""

    seen: dict[str, str] = field(default_factory=dict)
    changes: int = 0
    changed_since_last_start: bool = False
    changed_at: datetime | None = None
    due: float | None = None
    attempts: int = 0
    failed_since: float | None = None
    postponed: int = 0
    compared_with_last_start: bool = False
    result: str | None = None

    def compare_with_last_start(
        self, stored: Mapping[str, str], current: Mapping[str, str]
    ) -> None:
        """At start-up: the saved inventory shows the versions of the last start.

        Runs once, at the first discovery, even when a poll after a start with
        the saved inventory has already read the current versions.
        """
        if self.compared_with_last_start:
            self.seen.update(current)
            return
        self.compared_with_last_start = True
        changed = sorted(
            path for path, value in current.items() if stored.get(path, value) != value
        )
        self.seen.update(current)
        if changed:
            self.changed_since_last_start = True
            _LOGGER.info(
                "PointT firmware changed since the last start (%s); the start-up "
                "discovery checked the capabilities",
                ", ".join(resource_path_template(path) for path in changed),
            )

    def observe(self, current: Mapping[str, str], now: float) -> bool:
        """Remember versions; return True when a running gateway changed one.

        A discovery updates the seen versions itself, so its own cycle never
        counts as a change.
        """
        changed = sorted(
            path
            for path, value in current.items()
            if path in self.seen and self.seen[path] != value
        )
        self.seen.update(current)
        if not changed:
            return False
        self.changes += 1
        self.changed_at = dt_util.utcnow()
        self.due = now
        self.attempts = 0
        self.failed_since = None
        self.result = None
        _LOGGER.info(
            "PointT firmware changed while running (%s); checking the capabilities "
            "again with one discovery",
            ", ".join(resource_path_template(path) for path in changed),
        )
        return True

    def failed(self, now: float, schedule: tuple[int, ...]) -> None:
        """Plan the next attempt; the schedule counts from the first failure."""
        if self.failed_since is None:
            self.failed_since = now
        if self.attempts >= len(schedule):
            self.due = None
            self.result = "given_up"
        else:
            self.due = max(now, self.failed_since + schedule[self.attempts - 1])
            self.result = "failed"

    def diagnostics(self, now: float) -> dict[str, object]:
        return {
            "changes_while_running": self.changes,
            "changed_since_last_start": self.changed_since_last_start,
            "last_change_age_seconds": (
                max(0, int((dt_util.utcnow() - self.changed_at).total_seconds()))
                if self.changed_at is not None
                else None
            ),
            "discovery_attempts": self.attempts,
            "discovery_postponed": self.postponed,
            "discovery_result": self.result,
            "next_discovery_in_seconds": (
                max(0, math.ceil(self.due - now)) if self.due is not None else None
            ),
        }
