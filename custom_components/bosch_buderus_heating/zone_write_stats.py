"""Counters of the changes made through the climate of each zone.

The diagnostics show, per zone, how many changes (mode, target, ending a
temporary setpoint) were refused, sent and confirmed by the read-back, stayed
unconfirmed or failed. They count steps, not requests of the user: a mode with
a temperature is two steps. No value, name or path is kept; only the number
of the zone (already part of the report) keys the counters.

* ``writes_rejected``: refused before anything was sent (our own checks, a
  setting that is locked, unused or changed, or a busy gateway).
* ``writes_confirmed``: sent, and the read-back showed the value.
* ``writes_unconfirmed``: sent, but the read-back did not show it.
* ``writes_failed``: the request failed; whether it was sent is not known.
* ``writes_sent``: confirmed plus unconfirmed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final
from weakref import WeakKeyDictionary

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

COUNTERS: Final = (
    "writes_rejected",
    "writes_sent",
    "writes_confirmed",
    "writes_unconfirmed",
    "writes_failed",
)
_NOT_SENT_KEYS: Final = frozenset({"write_busy"})
_UNCONFIRMED_KEY: Final = "write_not_confirmed"
# Whatever the reason, only these fixed tokens are ever reported.
_REASONS: Final = frozenset(
    {
        "control_unused",
        "zone_climate_command",
        "write_validation_failed",
        "write_busy",
        "zone_temporary_setpoint_not_active",
    }
)


class ZoneWriteStats:
    """Counters per zone root (``/zones/zone5``)."""

    def __init__(self) -> None:
        self._counts: dict[str, Counter[str]] = {}
        self._reasons: dict[str, Counter[str]] = {}
        self._last: dict[str, tuple[str, str]] = {}

    def record(
        self, zone: str, kind: str, outcome: str, reason: str | None = None
    ) -> None:
        counts = self._counts.setdefault(zone, Counter())
        counts[f"writes_{outcome}"] += 1
        if outcome in ("confirmed", "unconfirmed"):
            counts["writes_sent"] += 1
        if outcome == "rejected" and reason in _REASONS:
            self._reasons.setdefault(zone, Counter())[str(reason)] += 1
        self._last[zone] = (kind, outcome)

    def snapshot(self, zone: str) -> dict[str, object]:
        counts = self._counts.get(zone, Counter())
        last = self._last.get(zone)
        reasons = self._reasons.get(zone, Counter())
        return {
            **{counter: counts[counter] for counter in COUNTERS},
            "last_step": (
                {"kind": last[0], "result": last[1]} if last is not None else None
            ),
            "rejected_reasons": dict(sorted(reasons.items())),
        }


_STATS: WeakKeyDictionary[object, ZoneWriteStats] = WeakKeyDictionary()


def zone_write_stats(coordinator: object) -> ZoneWriteStats:
    stats = _STATS.get(coordinator)
    if stats is None:
        stats = _STATS[coordinator] = ZoneWriteStats()
    return stats


@contextmanager
def counted_write(coordinator: object, zone: str, kind: str) -> Iterator[None]:
    """Count the outcome of the write step run inside the block."""
    stats = zone_write_stats(coordinator)
    try:
        yield
    except ServiceValidationError as err:
        stats.record(zone, kind, "rejected", err.translation_key)
        raise
    except HomeAssistantError as err:
        key = err.translation_key
        if key == _UNCONFIRMED_KEY:
            stats.record(zone, kind, "unconfirmed")
        elif key in _NOT_SENT_KEYS:
            stats.record(zone, kind, "rejected", key)
        else:
            stats.record(zone, kind, "failed")
        raise
    else:
        stats.record(zone, kind, "confirmed")
