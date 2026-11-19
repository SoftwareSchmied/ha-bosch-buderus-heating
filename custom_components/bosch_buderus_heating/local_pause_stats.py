"""Count the pauses of a local access, beside the short list of condition changes.

The diagnostics keep only the last condition changes of an access, so a series
of short pauses over hours drops out of that list. This counts every pause
that began (NX-03): in total, per fixed reason code and the last one. Only
fixed codes and times are kept, never values, paths or addresses.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Final

# Reasons kept apart; any further distinct code counts as "other", so a
# misbehaving source cannot grow the counter without bound.
MAX_REASONS: Final = 12
OTHER: Final = "other"
UNKNOWN: Final = "unknown"
_CODE = re.compile(r"[a-z0-9_.:-]{1,48}")


class PauseCounter:
    """Pauses that began since the access started."""

    __slots__ = ("_by_reason", "_last", "_total")

    def __init__(self) -> None:
        self._total = 0
        self._by_reason: dict[str, int] = {}
        self._last: dict[str, str | None] | None = None

    def record(self, at: datetime, reason: str | None) -> None:
        """Count one pause that began at ``at`` for the fixed code ``reason``."""
        if not isinstance(reason, str) or not reason:
            code = UNKNOWN
        else:
            code = reason if _CODE.fullmatch(reason) else OTHER
        if code not in self._by_reason and len(self._by_reason) >= MAX_REASONS:
            code = OTHER
        self._total += 1
        self._by_reason[code] = self._by_reason.get(code, 0) + 1
        self._last = {"at": at.isoformat(timespec="seconds"), "reason": code}

    def diagnostics(self) -> dict[str, object]:
        return {
            "pauses_total": self._total,
            "pauses_by_reason": dict(sorted(self._by_reason.items())),
            "last_pause": dict(self._last) if self._last is not None else None,
        }
