"""Why each access and each source started, for the diagnostics.

Only fixed reason codes, the time and the class of a failure: never a
message, value or identifier. The history belongs to the connection runtime,
so it survives the source objects it describes.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Collection
from enum import StrEnum

from homeassistant.util import dt as dt_util

# The most recent starts kept per access and per source.
RECENT_STARTS = 10


class StartReason(StrEnum):
    """Why an access or a source (re)started."""

    # The first start while the entry runs.
    START = "start"
    # The options or the stored configuration changed it, or it was switched
    # off and on again.
    CONFIGURATION = "configuration_changed"
    # The previous attempt failed; ``failure`` names the error class.
    FAILURE = "after_failure"
    # A source ended on its own while it was still current.
    ENDED = "source_ended"
    # The access of the source started again, and the source with it.
    ACCESS = "access_restarted"
    # The owner confirmed it in a repair.
    REQUESTED = "requested"
    # A start no known cause explains; kept rather than guessed.
    OTHER = "other"


class StartHistory:
    """The recent starts of each key, and the reason waiting for the next."""

    def __init__(self) -> None:
        self._pending: dict[str, StartReason] = {}
        self._recent: dict[str, deque[dict[str, object]]] = {}
        self._totals: dict[str, int] = {}

    def note(self, key: str, reason: StartReason) -> None:
        """Remember why ``key`` will start next; the first cause counts."""
        self._pending.setdefault(key, reason)

    def started(
        self,
        key: str,
        reason: StartReason | None = None,
        *,
        failure: str | None = None,
    ) -> None:
        """Record a start: ``reason`` if given, else the noted cause."""
        pending = self._pending.pop(key, None)
        if reason is None:
            reason = pending or (
                StartReason.OTHER if key in self._totals else StartReason.START
            )
        recent = self._recent.setdefault(key, deque(maxlen=RECENT_STARTS))
        recent.append(
            {
                "at": dt_util.utcnow().isoformat(timespec="seconds"),
                "reason": reason.value,
                "failure": failure,
            }
        )
        self._totals[key] = self._totals.get(key, 0) + 1

    def keep(self, keys: Collection[str]) -> None:
        """Removed accesses or sources: their history ends."""
        self._pending = {k: v for k, v in self._pending.items() if k in keys}
        self._recent = {k: v for k, v in self._recent.items() if k in keys}
        self._totals = {k: v for k, v in self._totals.items() if k in keys}

    def diagnostics(self, key: str) -> dict[str, object]:
        return {
            "total": self._totals.get(key, 0),
            "recent": list(self._recent.get(key, ())),
        }
