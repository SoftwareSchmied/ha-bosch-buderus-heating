"""Counters from both connections without ever counting backwards.

With local values enabled, paired energy, start and runtime counters may be
shown from the local connection. A counter only changes its source when every
number the entities show from it (each part and the derived energy values) is
at least the last number shown. Otherwise the previous source stays while its
reading is valid, and the counter becomes unavailable after that: a gap, never
an artificial consumption. Within one source a small decrease (a net heat
counter during a defrost) is shown as reported. A drop of more than
``DROP_FRACTION`` is held until the next reading of that source confirms it,
so a single outlier never counts as a meter reset, while a real device reset
stays visible one reading later. The last shown numbers and source are stored,
so the rule also holds after a restart.

When the source shown last is gone for good (its access removed or switched
off), the other source may take over once it is not lower for the parts both
report. A part the shown source no longer reports becomes unconfirmed. A
stored part that no source has reported for ``STALE_PART_SECONDS`` since it
became unconfirmed no longer binds anything and is dropped; a part that is
reported stays bound until it reaches its stored number.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .energy_contracts import assess_energy, is_energy_collection
from .log_helpers import log_failure
from .read_contracts import assess_runtime, is_runtime_path
from .start_counters import assess_start_counter, is_start_counter_path

if TYPE_CHECKING:
    from .coordinator import ResourceSnapshot
    from .pointt import Resource

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.counter_sources"
# O69: shown counters change slowly; a source switch is still written at once
# and a pending save is written when Home Assistant stops.
SAVE_DELAY_SECONDS = 60
MAX_ROWS = 2048
SOURCES = frozenset({"local", "cloud"})
_ID = re.compile(r"[^|\x00-\x1f\x7f]{1,256}")
# Without stored history only the cloud may set the first count: it is what the
# counter showed before local values existed.
BASELINE_SOURCE = "cloud"
# A part no source reports any more stops binding after this time.
STALE_PART_SECONDS = 7 * 24 * 3600.0
# SEL-4: a decrease of more than this share within one source is what Home
# Assistant takes as a meter reset; it is held until a second reading shows it.
DROP_FRACTION = 0.1


def _now() -> float:
    """Wall-clock seconds; the waiting time must survive a restart."""
    return time.time()


_STORES: HassKey[CounterSourceStore] = HassKey(f"{DOMAIN}.counter_source_store")


@dataclass(frozen=True, slots=True)
class ShownCounter:
    """Last numbers shown for one counter and the source they came from."""

    source: str
    values: Mapping[str, float]
    # Parts the current source itself has shown; None: all of them (also rows
    # of older versions). A part taken over from the previous source is not
    # confirmed until this source shows it at least as high (P49b).
    confirmed: frozenset[str] | None = None
    # Wall-clock time from which the unconfirmed parts wait; None: not known
    # yet (the wait starts with the next decision).
    since: float | None = None

    def unconfirmed(self) -> frozenset[str]:
        if self.confirmed is None:
            return frozenset()
        return frozenset(self.values) - self.confirmed


def counter_vector(resource: Resource) -> dict[str, float] | None:
    """Every number entities may show from a counter, including derived ones."""
    readings: dict[str, float] = {}
    if is_energy_collection(resource):
        for key, reading in assess_energy(resource).readings:
            if reading.value is not None:
                readings[key] = float(reading.value)
        if not readings:
            return None
        if "electricity" in readings:
            readings["=total_electricity"] = readings["electricity"]
        elif "compressor" in readings and "eheater" in readings:
            readings["=total_electricity"] = math.fsum(
                (readings["compressor"], readings["eheater"])
            )
        if {"outputProduced", "compressor", "eheater"} <= readings.keys():
            readings["=environmental_energy"] = math.fsum(
                (
                    readings["outputProduced"],
                    -readings["compressor"],
                    -readings["eheater"],
                )
            )
        return readings
    assessment: Any = None
    if is_start_counter_path(resource.path):
        assessment = assess_start_counter(resource)
    elif is_runtime_path(resource.path):
        assessment = assess_runtime(resource)
    if assessment is not None:
        for key, reading in assessment.readings:
            if reading.value is not None:
                readings[key or "value"] = float(reading.value)
        return readings or None
    value = resource.value
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return {"value": float(value)} if math.isfinite(value) else None


def not_lower(
    candidate: Mapping[str, float],
    last: Mapping[str, float],
    *,
    partial: bool = False,
) -> bool:
    """Every previously shown number is present and not lower.

    With ``partial``, a reading that lacks some parts (for example a local
    energy collection without the consumption parts) passes if it has at
    least one shown part and none of them is lower. Its missing parts and
    the values derived from them then show nothing; they never jump.
    """
    if partial:
        shared = [key for key in last if key in candidate]
        return bool(shared) and all(candidate[key] >= last[key] for key in shared)
    return all(
        key in candidate and candidate[key] >= value for key, value in last.items()
    )


class CounterSourceStore:
    """HA-wide record of the last shown counter numbers per entry and target."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self._rows: dict[str, ShownCounter] = {}
        self._loaded = False
        self._pending = False

    @property
    def store(self) -> Store[dict[str, Any]]:
        return self._store

    async def async_initialize(self) -> None:
        """Load once; a damaged file starts empty and is not fatal."""
        if self._loaded:
            return
        try:
            stored = await self._store.async_load()
        except Exception as err:
            log_failure(
                _LOGGER,
                logging.WARNING,
                "Saved counter sources could not be read",
                error=err,
            )
            stored = None
        if not self._loaded:
            self._rows = dict(_rows(stored))
            self._loaded = True

    def get(self, key: str) -> ShownCounter | None:
        return self._rows.get(key)

    def set(self, key: str, shown: ShownCounter, *, immediate: bool = False) -> None:
        """Remember the shown counter; a source change is written at once."""
        if self._rows.get(key) == shown:
            return
        if key not in self._rows and len(self._rows) >= MAX_ROWS:
            return  # without a row the counter keeps its cloud baseline
        self._rows[key] = shown
        self._schedule_save(immediate=immediate)

    def discard_owner(self, owner_id: str) -> None:
        prefix = owner_id + "|"
        removed = [key for key in self._rows if key.startswith(prefix)]
        for key in removed:
            del self._rows[key]
        if removed:
            self._schedule_save()

    def _schedule_save(self, *, immediate: bool = False) -> None:
        """Save at once, or at most ``SAVE_DELAY_SECONDS`` after the first change.

        PERF-4: Home Assistant moves a delayed save to the last call plus the
        delay, so frequent changes would keep postponing it; a pending save is
        therefore not scheduled again. An immediate save still goes first.
        """
        if immediate:
            self._pending = True
            self._store.async_delay_save(self._data, 0)
        elif not self._pending:
            self._pending = True
            self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    async def async_discard_target(self, owner_id: str, target_id: str) -> None:
        """Forget the shown counters of one target, saved at once (P50).

        After a gateway exchange the last shown source says nothing about the
        new gateway's counters; the exchange's own counter rule decides.
        """
        await self.async_initialize()  # never save over rows not read yet
        prefix = f"{owner_id}|{target_id}|"
        removed = [key for key in self._rows if key.startswith(prefix)]
        for key in removed:
            del self._rows[key]
        if removed:
            await self._store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        self._pending = False
        return {"counters": {key: _row_data(row) for key, row in self._rows.items()}}


def _row_data(row: ShownCounter) -> dict[str, Any]:
    data: dict[str, Any] = {"source": row.source, "values": dict(row.values)}
    if row.confirmed is not None:
        data["confirmed"] = sorted(row.confirmed)
        if row.since is not None:
            data["since"] = row.since
    return data


def _rows(stored: object) -> Iterable[tuple[str, ShownCounter]]:
    if not isinstance(stored, Mapping):
        return
    counters = stored.get("counters")
    if not isinstance(counters, Mapping):
        return
    for key, row in list(counters.items())[:MAX_ROWS]:
        if not isinstance(key, str) or not all(
            _ID.fullmatch(p) for p in key.split("|")
        ):
            continue
        if not isinstance(row, Mapping) or row.get("source") not in SOURCES:
            continue
        values = row.get("values")
        if not isinstance(values, Mapping) or not values:
            continue
        clean = {
            name: float(number)
            for name, number in values.items()
            if isinstance(name, str)
            and not isinstance(number, bool)
            and isinstance(number, int | float)
            and math.isfinite(number)
        }
        if len(clean) != len(values):
            continue
        if "confirmed" not in row:
            # Older versions: every stored part counts as confirmed.
            yield key, ShownCounter(row["source"], clean)
            continue
        confirmed = row["confirmed"]
        if not isinstance(confirmed, list):
            confirmed = []  # damaged: nothing counts as confirmed
        since = row.get("since")
        if (
            isinstance(since, bool)
            or not isinstance(since, int | float)
            or not math.isfinite(since)
        ):
            since = None  # missing or damaged: the wait starts again
        yield (
            key,
            ShownCounter(
                row["source"],
                clean,
                frozenset(name for name in confirmed if name in clean),
                float(since) if since is not None else None,
            ),
        )


def counter_source_store(hass: HomeAssistant) -> CounterSourceStore:
    """Return the HA-wide store, creating it without I/O."""
    if (store := hass.data.get(_STORES)) is None:
        store = hass.data[_STORES] = CounterSourceStore(hass)
    return store


class CounterGuard:
    """Choose the shown counter per target, never counting backwards."""

    def __init__(self, store: CounterSourceStore, owner_id: str, target_id: str):
        self._store = store
        self._prefix = f"{owner_id}|{target_id}|"
        self._blocked_now: set[str] = set()
        self.switches: dict[str, int] = {}
        self.blocked: dict[str, int] = {}
        self.shown: dict[str, str] = {}
        self._decided: set[str] = set()
        # SEL-4: per counter, the source and reading time of a held drop.
        self._drops: dict[str, tuple[str, datetime]] = {}

    def decide(
        self,
        path: str,
        candidates: Iterable[tuple[str, ResourceSnapshot | None]],
        *,
        partial: bool = False,
        gone: frozenset[str] = frozenset(),
    ) -> ResourceSnapshot | None:
        """Return the first valid candidate the rule allows, in preference order.

        Without history only the cloud sets the first count. After a restart the
        source shown last goes first. Parts missing in a new reading keep their
        last shown number, so they still bind a later source change, but they
        become unconfirmed and stop binding after ``STALE_PART_SECONDS``.

        Within one source, a part more than ``DROP_FRACTION`` below its shown
        number is held until a later reading of that source shows the drop
        again; a reading in between that does not drop ends the hold.

        With ``partial`` (only the local plant view, its local access being
        the sole source), a reading lacking some parts may take over. The
        parts it lacks stay unconfirmed: when the same source shows one of
        them later, it is held back while it is below its stored number.

        ``gone`` names sources whose access was removed or switched off. When
        the stored source is one of them, another source may take over once
        it is not lower for the parts both report.
        """
        key = self._prefix + path
        last = self._store.get(key)
        ordered = [
            (source, snapshot, counter_vector(snapshot.resource))
            for source, snapshot in candidates
            if snapshot is not None and snapshot.available
        ]
        if last is not None:
            last = self._drop_stale(key, last, ordered)
        if last is not None and path not in self._decided:
            first = last.source
            ordered.sort(key=lambda item: item[0] != first)
        blocked = False
        without_numbers: ResourceSnapshot | None = None
        for source, snapshot, vector in ordered:
            if vector is None:
                # No count yet (e.g. a registered counter awaiting its first
                # value): nothing can go backwards, show it as before.
                if source == BASELINE_SOURCE:
                    without_numbers = snapshot
                continue
            if last is None and source != BASELINE_SOURCE:
                blocked = True
                continue
            switch = last is not None and last.source != source
            if (
                switch
                and last is not None
                and not not_lower(
                    vector, last.values, partial=partial or last.source in gone
                )
            ):
                blocked = True
                continue
            if (
                not switch
                and last is not None
                and any(
                    name in vector and vector[name] < last.values[name]
                    for name in last.unconfirmed()
                )
            ):
                # A part this source had not shown yet is below the number
                # shown before: held back until it reaches it.
                blocked = True
                continue
            if (
                not switch
                and last is not None
                and self._held_drop(path, source, snapshot, vector, last)
            ):
                blocked = True
                continue
            if switch:
                self.switches[path] = self.switches.get(path, 0) + 1
                from .observations import pair_for_path

                pair = pair_for_path(path)
                _LOGGER.debug(
                    "Counter %s now shown from the %s access",
                    pair.measurement if pair is not None else "unknown",
                    source,
                )
            values = {**last.values, **vector} if last is not None else vector
            confirmed: frozenset[str] | None = None
            since: float | None = None
            if last is not None:
                # NRG-1: confirmed are the parts this source reports now; a
                # part it no longer reports waits like one never shown, so a
                # part that is gone for good cannot block a switch forever.
                confirmed = frozenset(vector)
                if confirmed >= frozenset(values):
                    confirmed = None  # every part shown by this source
            if confirmed is not None and last is not None:
                # The parts this source has not shown wait from the switch on,
                # or from the reading that first lacked them.
                newly = frozenset(values) - confirmed - last.unconfirmed()
                since = _now() if switch or newly else last.since
            self._store.set(
                key, ShownCounter(source, values, confirmed, since), immediate=switch
            )
            self._decided.add(path)
            self._drops.pop(path, None)  # a held drop must be seen twice in a row
            self.shown[path] = source
            self._note_blocked(path, blocked)
            return snapshot
        self._note_blocked(path, blocked)
        if without_numbers is not None and (last is None or not blocked):
            self.shown[path] = BASELINE_SOURCE
            return without_numbers
        self.shown[path] = "unavailable"
        return None

    def _drop_stale(
        self,
        key: str,
        last: ShownCounter,
        ordered: list[tuple[str, ResourceSnapshot, dict[str, float] | None]],
    ) -> ShownCounter:
        """Drop waiting parts that no source has reported for a long time.

        Only unconfirmed parts that no current reading reports: a reported
        part keeps binding until it reaches its stored number, and a part
        nobody reports shows nothing, so no shown number goes backwards.
        """
        waiting = last.unconfirmed()
        if not waiting:
            return last
        now = _now()
        if last.since is None:
            row = replace(last, since=now)
            self._store.set(key, row)
            return row
        if now - last.since < STALE_PART_SECONDS:
            return last  # also with a clock set back: not stale
        if not any(vector for *_, vector in ordered):
            return last  # no current reading: no evidence that a part ended
        reported = {name for *_, vector in ordered if vector for name in vector}
        drop = waiting - reported
        values = {
            name: value for name, value in last.values.items() if name not in drop
        }
        if not drop or not values:
            return last
        confirmed = (last.confirmed or frozenset()) & values.keys()
        row = (
            ShownCounter(last.source, values)
            if confirmed >= values.keys()
            else ShownCounter(last.source, values, frozenset(confirmed), last.since)
        )
        self._store.set(key, row, immediate=True)
        return row

    def _held_drop(
        self,
        path: str,
        source: str,
        snapshot: ResourceSnapshot,
        vector: Mapping[str, float],
        last: ShownCounter,
    ) -> bool:
        """SEL-4: whether a large drop within the shown source is held.

        The first reading with a drop is held; a later reading of the same
        source that still drops shows it (a real reset). Small decreases and
        readings that do not drop pass and end a hold.
        """
        # Reported parts only: a derived difference (the environmental energy)
        # moves by a larger share in a defrost, and an outlier always shows in
        # a reported part.
        dropped = any(
            not name.startswith("=")
            and name in vector
            and last.values[name] > 0
            and vector[name] < last.values[name] * (1 - DROP_FRACTION)
            for name in last.values
        )
        if not dropped:
            self._drops.pop(path, None)
            return False
        held = self._drops.get(path)
        if held is not None and held[0] == source and snapshot.last_success > held[1]:
            del self._drops[path]
            _LOGGER.debug("Counter reading dropped twice in a row; shown as a reset")
            return False
        if held is None or held[0] != source:
            self._drops[path] = (source, snapshot.last_success)
        return True

    def forget_shown(self) -> None:
        """A stopped source shows nothing; keep the stored history."""
        self.shown.clear()
        self._blocked_now.clear()
        self._decided.clear()
        self._drops.clear()

    def summary(self) -> dict[str, dict[str, Any]]:
        """Per catalog measurement: shown source, switches and blocked switches."""
        from .observations import pair_for_path

        result: dict[str, dict[str, Any]] = {}
        for path, shown in self.shown.items():
            pair = pair_for_path(path)
            if pair is None:
                continue
            entry = result.setdefault(
                pair.measurement, {"shown": {}, "switches": 0, "blocked": 0}
            )
            entry["shown"][shown] = entry["shown"].get(shown, 0) + 1
            entry["switches"] += self.switches.get(path, 0)
            entry["blocked"] += self.blocked.get(path, 0)
        return dict(sorted(result.items()))

    def _note_blocked(self, path: str, blocked: bool) -> None:
        if blocked and path not in self._blocked_now:
            self.blocked[path] = self.blocked.get(path, 0) + 1
            self._blocked_now.add(path)
        elif not blocked:
            self._blocked_now.discard(path)
