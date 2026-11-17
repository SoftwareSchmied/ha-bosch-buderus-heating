"""The setpoint of a zone follows its mode; find it again after a mode change.

A zone controller offers only the setpoint that belongs to the zone's current
mode: ``manualRoomSetpoint`` in ``manual`` and ``temporaryRoomSetpoint`` in
``auto``. Discovery sees the one the mode had at that time; the other is
answered with "not found" until the mode changes (in the app or here). After
a change of the mode the coordinator therefore reads the setpoint of the new
mode once more (a short, bounded series, as for a heating circuit's manual
setpoint) and from then on polls it. Nothing is written for this.

This module holds what is specific to zones: which paths belong together and
the budget that keeps the extra reads small. ``off`` has no setpoint.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Final

_SIDE: Final = r"/zones/zone[0-9]+/(?:heating|cooling)"
_MODE: Final = re.compile(rf"^({_SIDE})/operationMode$")
_TARGET: Final = re.compile(rf"^({_SIDE})/(manualRoomSetpoint|temporaryRoomSetpoint)$")

MANUAL_TARGET: Final = "manualRoomSetpoint"
TEMPORARY_TARGET: Final = "temporaryRoomSetpoint"
# The mode whose setpoint each target is.
_MODE_OF: Final = {MANUAL_TARGET: "manual", TEMPORARY_TARGET: "auto"}
_TARGET_OF: Final = {mode: leaf for leaf, mode in _MODE_OF.items()}

# At most this many series per target and per hour, and this many in all.
EPISODES_PER_TARGET: Final = 3
EPISODES_TOTAL: Final = 12
WINDOW_SECONDS: Final = 3600.0
# Between two series for the same setpoint, so a controller that is slow to
# offer it gets time and a refusing one is not asked in a burst.
MIN_GAP_SECONDS: Final = 600.0


def zone_mode_path(path: str) -> str | None:
    """The mode path of a zone side for its mode path or one of its targets."""
    match = _MODE.fullmatch(path) or _TARGET.fullmatch(path)
    return f"{match.group(1)}/operationMode" if match is not None else None


def is_zone_target(path: str) -> bool:
    return _TARGET.fullmatch(path) is not None


def required_mode(setpoint_path: str) -> str:
    """The mode in which this setpoint exists (a circuit's target: manual)."""
    match = _TARGET.fullmatch(setpoint_path)
    return _MODE_OF[match.group(2)] if match is not None else "manual"


def targets_for_mode(mode_path: str, mode: object) -> tuple[str | None, str | None]:
    """The setpoint of this mode and the one of the other mode.

    Both are ``None`` for ``off``, an unknown mode or a path that is no zone
    mode.
    """
    match = _MODE.fullmatch(mode_path)
    if match is None:
        return None, None
    wanted_leaf = _TARGET_OF.get(mode) if isinstance(mode, str) else None
    if wanted_leaf is None:
        return None, None  # off or unknown: no setpoint, nothing is reset
    root = match.group(1)
    other_leaf = next(leaf for leaf in _MODE_OF if leaf != wanted_leaf)
    return f"{root}/{wanted_leaf}", f"{root}/{other_leaf}"


class RecoveryBudget:
    """Bounds the reads that look for a setpoint after a mode change."""

    def __init__(self) -> None:
        self._started: deque[tuple[float, str]] = deque()
        self.episodes = 0
        self.limited = 0
        self.recovered = 0

    def allow(self, path: str, now: float) -> bool:
        """Count a new series for ``path``; ``False`` when the budget is spent."""
        while self._started and now - self._started[0][0] >= WINDOW_SECONDS:
            self._started.popleft()
        if any(
            item == path and now - at < MIN_GAP_SECONDS for at, item in self._started
        ):
            return False
        if len(self._started) >= EPISODES_TOTAL or (
            sum(1 for _, item in self._started if item == path) >= EPISODES_PER_TARGET
        ):
            self.limited += 1
            return False
        self._started.append((now, path))
        self.episodes += 1
        return True

    def diagnostics(self) -> dict[str, int]:
        """Counts only; no path, name or value."""
        return {
            "series_started": self.episodes,
            "setpoints_found": self.recovered,
            "series_refused_by_budget": self.limited,
        }
