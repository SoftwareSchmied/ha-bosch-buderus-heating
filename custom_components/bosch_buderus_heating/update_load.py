"""Measure entity update passes and garbage-collection pauses.

Home Assistant warns when a single entity state update takes longer than 0.4 s.
These process-wide numbers show whether such a pause came from Python's
garbage collector or from the integration's own update work. Only counts and
whole milliseconds are kept: no paths, values or identifiers.
"""

from __future__ import annotations

import gc
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_CLOSE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

# A collection this long is worth counting; Home Assistant warns from 0.4 s.
SLOW_COLLECTION_SECONDS = 0.1
HA_SLOW_UPDATE_SECONDS = 0.4
_GENERATIONS = 3

_KEY: HassKey[UpdateLoad] = HassKey(f"{DOMAIN}.update_load")


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


class UpdateLoad:
    """Counters for update passes and the garbage collections around them."""

    def __init__(self) -> None:
        self._collection_started: float | None = None
        self._pass_depth = 0
        self.collections = [0] * _GENERATIONS
        self.collection_total = 0.0
        self.collection_maximum = 0.0
        self.slow_collections = 0
        self.slow_collections_during_updates = 0
        self.collection_maximum_during_updates = 0.0
        self.passes = 0
        self.pass_total = 0.0
        self.pass_maximum = 0.0
        self.slow_passes = 0
        self.pass_maximum_entities = 0

    def gc_callback(self, phase: str, info: dict[str, Any]) -> None:
        """Time one collection; it stops every thread, the event loop included."""
        if phase == "start":
            self._collection_started = time.perf_counter()
            return
        started, self._collection_started = self._collection_started, None
        if started is None:
            return
        duration = time.perf_counter() - started
        generation = info.get("generation")
        if isinstance(generation, int) and 0 <= generation < _GENERATIONS:
            self.collections[generation] += 1
        self.collection_total += duration
        self.collection_maximum = max(self.collection_maximum, duration)
        during_update = self._pass_depth > 0
        if during_update:
            self.collection_maximum_during_updates = max(
                self.collection_maximum_during_updates, duration
            )
        if duration >= SLOW_COLLECTION_SECONDS:
            self.slow_collections += 1
            if during_update:
                self.slow_collections_during_updates += 1

    @contextmanager
    def measure_pass(self, entities: int) -> Iterator[None]:
        """Measure one synchronous pass that updates the given number of entities."""
        self._pass_depth += 1
        started = time.perf_counter()
        try:
            yield
        finally:
            self._pass_depth -= 1
            if self._pass_depth == 0:
                duration = time.perf_counter() - started
                self.passes += 1
                self.pass_total += duration
                self.pass_maximum = max(self.pass_maximum, duration)
                self.pass_maximum_entities = max(self.pass_maximum_entities, entities)
                if duration >= HA_SLOW_UPDATE_SECONDS:
                    self.slow_passes += 1

    def diagnostics(self) -> dict[str, object]:
        """Counts and whole milliseconds since Home Assistant started."""
        return {
            "measured": True,
            "garbage_collections": {
                f"generation_{number}": count
                for number, count in enumerate(self.collections)
            },
            "garbage_collection_total_ms": _ms(self.collection_total),
            "garbage_collection_maximum_ms": _ms(self.collection_maximum),
            "garbage_collections_over_100_ms": self.slow_collections,
            "garbage_collections_over_100_ms_during_updates": (
                self.slow_collections_during_updates
            ),
            "garbage_collection_maximum_during_updates_ms": _ms(
                self.collection_maximum_during_updates
            ),
            "update_passes": self.passes,
            "update_pass_average_ms": (
                _ms(self.pass_total / self.passes) if self.passes else None
            ),
            "update_pass_maximum_ms": _ms(self.pass_maximum),
            "update_passes_over_400_ms": self.slow_passes,
            "update_pass_maximum_entities": self.pass_maximum_entities,
        }


def update_load(hass: HomeAssistant) -> UpdateLoad | None:
    """Return the running measurement, if installed."""
    return hass.data.get(_KEY)


@callback
def install_update_load(hass: HomeAssistant) -> UpdateLoad:
    """Start measuring once per Home Assistant instance; stop when it closes."""
    if (load := hass.data.get(_KEY)) is not None:
        return load
    load = hass.data[_KEY] = UpdateLoad()
    gc.callbacks.append(load.gc_callback)

    @callback
    def _remove(_event: Event | None = None) -> None:
        with suppress(ValueError):
            gc.callbacks.remove(load.gc_callback)
        if hass.data.get(_KEY) is load:
            hass.data.pop(_KEY)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_CLOSE, _remove)
    return load


def update_load_diagnostics(hass: HomeAssistant) -> dict[str, object]:
    load = update_load(hass)
    return load.diagnostics() if load is not None else {"measured": False}
