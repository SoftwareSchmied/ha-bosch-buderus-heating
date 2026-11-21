"""The failed bulk items of one gateway, as far as they may be kept (#146).

A bulk answer that fails a path carries a status and sometimes a few error
fields. This keeps, for the debug log and the diagnostics, how many items of
each status failed and the last few failures with their redacted fields. The
paths are path templates and the fields come from
``pointt.redaction.safe_error_details``; no value of a reading is kept.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime

from .pointt import BatchItemResult
from .pointt.redaction import diagnostic_resource_path

MAX_RECENT_ITEM_ERRORS = 10
# Statuses a controller gives as a matter of course for paths it does not
# offer: counted, but they fill no place of the recent list without details.
_ROUTINE_STATUSES = frozenset({403, 404, 406})


class ItemErrorLog:
    """Counts per status and a bounded list of the latest failed items."""

    __slots__ = ("_recent", "counts")

    def __init__(self) -> None:
        self.counts: Counter[int] = Counter()
        # key -> entry; a repeat of the same failure counts up its entry.
        self._recent: dict[str, dict[str, object]] = {}

    def record(self, results: Iterable[BatchItemResult]) -> None:
        """Take the failed items of one bulk answer."""
        for result in results:
            status = result.server_status
            if result.resource is not None or status is None or status == 200:
                continue
            self.counts[status] += 1
            details = dict(result.error_details) if result.error_details else None
            if status in _ROUTINE_STATUSES and details is None:
                continue
            entry: dict[str, object] = {
                "status": status,
                "path": diagnostic_resource_path(result.path),
                "error": type(result.error).__name__ if result.error else None,
                "details": details,
            }
            key = json.dumps(entry, sort_keys=True, default=str)
            seen = self._recent.pop(key, None)
            previous = seen["count"] if seen is not None else 0
            entry["count"] = (previous if isinstance(previous, int) else 0) + 1
            entry["last_seen"] = datetime.now(UTC).isoformat(timespec="seconds")
            self._recent[key] = entry
            while len(self._recent) > MAX_RECENT_ITEM_ERRORS:
                del self._recent[next(iter(self._recent))]

    def diagnostics(self) -> dict[str, object]:
        """The counts per status and the latest failures, oldest first."""
        return {
            "status_counts": {
                str(status): count for status, count in sorted(self.counts.items())
            },
            "recent": [dict(entry) for entry in self._recent.values()],
        }
