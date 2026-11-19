"""The start-up phase of the compressor, for the current COP.

In the first minutes after the compressor starts, the heat output rises
more slowly than the electrical power (readings of the heat meter lag the
compressor), so a COP worked out from them is wrong: it swings far too high
or reads zero. The COP stays unknown, with ``reason: starting``, until the
compressor has run for ``STARTUP_SECONDS``.

A compressor that is already running when the integration first sees it
counts as running for good: its start is not known, and the COP of a run in
progress is not held back after a restart of Home Assistant.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from homeassistant.util import dt as dt_util

STARTUP_SECONDS: Final = 300.0
STARTING: Final = "starting"
_SETTLED: Final = timedelta(seconds=STARTUP_SECONDS)


class StartPhase:
    """Follows the compressor between readings of one heat pump."""

    def __init__(self) -> None:
        self._started: datetime | None = None
        self._stopped_seen = False

    def observe(self, running: bool | None, now: datetime | None = None) -> bool:
        """Take the compressor's state; ``True`` while it is still starting.

        ``None`` (no reading) changes nothing.
        """
        if running is None:
            return self.starting(now)
        now = now or dt_util.utcnow()
        if running is False:
            self._started = None
            self._stopped_seen = True
            return False
        if self._started is None:
            # A start seen after a stop; a run found in progress has no
            # known start.
            self._started = now if self._stopped_seen else now - _SETTLED
        return self.starting(now)

    def starting(self, now: datetime | None = None) -> bool:
        if self._started is None:
            return False
        now = now or dt_util.utcnow()
        return (now - self._started).total_seconds() < STARTUP_SECONDS
