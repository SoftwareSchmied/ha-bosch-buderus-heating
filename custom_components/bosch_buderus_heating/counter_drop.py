"""A single large drop of an energy counter is held until it is confirmed.

The shown counter entities hold such a drop already (``counter_selection``):
a single low reading (for example a whole collection read as 0) would
otherwise count the whole meter again. The calculated values that read the
raw counters (net heat, defrost energy, performance factors) use the same
rule: the first reading with a large drop is ignored, and a later reading of
the same counter that still drops confirms it as a replaced or reset
counter. A reading without a drop ends a hold.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

# Within the shown counters, a drop by more than this share is held.
DROP_FRACTION: Final = 0.1


def large_drop(value: float, reference: float, *, at_least: float = 0.0) -> bool:
    """``value`` is more than ``DROP_FRACTION`` (and more than ``at_least``)
    below ``reference``."""
    return value < reference * (1 - DROP_FRACTION) and value < reference - at_least


class DropHold:
    """Holds the first reading of a counter with a large drop."""

    __slots__ = ("_held",)

    def __init__(self) -> None:
        self._held: dict[str, datetime | None] = {}

    def held(self, key: str, dropped: bool, observed_at: datetime | None) -> bool:
        """Whether to ignore this reading of ``key``.

        True for the first reading with a drop; a later reading (a newer
        reading time, or one without a time) that still drops is let through
        as a real reset.
        """
        if not dropped:
            self._held.pop(key, None)
            return False
        if key not in self._held:
            self._held[key] = observed_at
            return True
        since = self._held[key]
        if observed_at is None or since is None or observed_at > since:
            del self._held[key]
            return False
        return True  # the same reading again

    def clear(self) -> None:
        self._held.clear()
