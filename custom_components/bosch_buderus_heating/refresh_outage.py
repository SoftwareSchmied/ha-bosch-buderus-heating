"""Report a failed cloud refresh as an error only once the outage lasts (#60).

Home Assistant logs the first failed refresh of a coordinator as an error at
once, so a cloud that does not answer for one or two cycles already leaves an
error in the log although nothing needs attention. The coordinator therefore
reports the failures of one outage itself: debug messages while it is short,
one error once it lasts, and debug messages again for the rest of it. Home
Assistant's info line ("Fetching ... data recovered") still reports the end.
"""

from __future__ import annotations

import logging
from time import monotonic

# An outage is an error once it has lasted this long and at least this many
# refreshes in a row failed. With the one-minute cycle that is the sixth failed
# refresh: three failed cycles open the circuit breaker (one warning), its
# five-minute pause follows.
ERROR_GRACE_SECONDS = 5 * 60
ERROR_MIN_FAILURES = 3


class RefreshOutageLog:
    """The failed refreshes of the current outage of one coordinator."""

    __slots__ = ("_logger", "_name", "failures", "reported", "since")

    def __init__(self, logger: logging.Logger, name: str) -> None:
        self._logger = logger
        self._name = name
        self.failures = 0
        self.since: float | None = None
        self.reported = False

    def failed(self, error: Exception, now: float | None = None) -> None:
        """Count one failed refresh; log the error once the outage lasts."""
        now = monotonic() if now is None else now
        if self.since is None:
            self.since = now
        self.failures += 1
        lasted = max(0.0, now - self.since)
        if self.reported:
            self._logger.debug(
                "Error fetching %s data again (%d failed refreshes in a row): %s",
                self._name,
                self.failures,
                error,
            )
            return
        if self.failures >= ERROR_MIN_FAILURES and lasted >= ERROR_GRACE_SECONDS:
            self.reported = True
            self._logger.error(
                "Error fetching %s data: %s. No usable cloud data for %.0f "
                "minutes (%d failed refreshes in a row); further failures of "
                "this outage are logged at debug level",
                self._name,
                error,
                lasted / 60,
                self.failures,
            )
            return
        self._logger.debug(
            "Error fetching %s data: %s. Logged as an error if it lasts %d minutes",
            self._name,
            error,
            ERROR_GRACE_SECONDS // 60,
        )

    def recovered(self) -> None:
        """A refresh succeeded: the next failure starts a new outage."""
        self.failures = 0
        self.since = None
        self.reported = False
