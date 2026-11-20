"""An energy total that only grows never shows a tiny step backwards.

A counter with the statistics class ``total_increasing`` is read from two
connections and rounded or truncated differently by each; after a restart,
a pause or a change of the connection shown, the first reading can be one
resolution step (0.01 kWh) below the last number published. Home Assistant
does not treat such a step as a reset, but long-term statistics and
dashboards that sum increases do count the whole meter again.

``MonotonicCounter`` therefore keeps the last published number (stored, so it
also holds after a restart) and shows that number again while a reading is
below it by no more than ``TOLERANCE_KWH``. A larger drop is a different
event (a replaced or reset meter, already confirmed by the counter guard) and
is shown as reported. Counters with the statistics class ``total`` fall by a
few hundredths of a kilowatt hour in every defrost by design; they are not
changed.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any, Final

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .log_helpers import log_failure

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.published_counters"
SAVE_DELAY_SECONDS = 60
MAX_ROWS = 4096
# A step of this size or smaller is rounding, never a meter reset.
TOLERANCE_KWH: Final = 0.05
_PER_KWH: Final[dict[str, float]] = {
    UnitOfEnergy.KILO_WATT_HOUR: 1.0,
    UnitOfEnergy.WATT_HOUR: 1000.0,
    UnitOfEnergy.MEGA_WATT_HOUR: 0.001,
}

_STORES: HassKey[PublishedCounterStore] = HassKey(f"{DOMAIN}.published_counters")


def tolerance_for(
    state_class: SensorStateClass | str | None,
    device_class: SensorDeviceClass | str | None,
    unit: str | None,
) -> float | None:
    """The tolerance in the entity's unit; None if the rule does not apply."""
    if state_class != SensorStateClass.TOTAL_INCREASING:
        return None
    if device_class != SensorDeviceClass.ENERGY or unit not in _PER_KWH:
        return None
    return TOLERANCE_KWH * _PER_KWH[unit]


class PublishedCounterStore:
    """HA-wide record of the last published number per counter entity."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self._rows: dict[str, float] = {}
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
                "Saved counter numbers could not be read",
                error=err,
            )
            stored = None
        if not self._loaded:
            self._rows = _rows(stored)
            self._loaded = True

    def get(self, unique_id: str) -> float | None:
        return self._rows.get(unique_id)

    def set(self, unique_id: str, value: float) -> None:
        if self._rows.get(unique_id) == value:
            return
        if unique_id not in self._rows and len(self._rows) >= MAX_ROWS:
            return  # without a row the counter simply is not held after a restart
        self._rows[unique_id] = value
        if not self._pending:  # a pending save is not postponed again
            self._pending = True
            self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    def _data(self) -> dict[str, Any]:
        self._pending = False
        return {"counters": dict(self._rows)}


def _rows(stored: object) -> dict[str, float]:
    if not isinstance(stored, Mapping):
        return {}
    counters = stored.get("counters")
    if not isinstance(counters, Mapping):
        return {}
    return {
        key: float(number)
        for key, number in list(counters.items())[:MAX_ROWS]
        if isinstance(key, str)
        and not isinstance(number, bool)
        and isinstance(number, int | float)
        and math.isfinite(number)
    }


def published_counter_store(hass: HomeAssistant) -> PublishedCounterStore:
    """Return the HA-wide store, creating it without I/O."""
    if (store := hass.data.get(_STORES)) is None:
        store = hass.data[_STORES] = PublishedCounterStore(hass)
    return store


class MonotonicCounter:
    """The last published number of one counter entity."""

    __slots__ = ("_last", "_store", "_tolerance", "_unique_id", "held")

    def __init__(
        self, store: PublishedCounterStore, unique_id: str, tolerance: float
    ) -> None:
        self._store, self._unique_id, self._tolerance = store, unique_id, tolerance
        self._last: float | None = store.get(unique_id)
        self.held = 0  # readings replaced by the last number (diagnostics, tests)

    def value(self, reading: float) -> float:
        """The number to publish for ``reading``; never a tiny step backwards."""
        last = self._last
        if (
            last is not None
            and reading < last
            and last - reading <= self._tolerance + 1e-9
        ):
            self.held += 1
            return last
        if reading != last:
            self._last = reading
            self._store.set(self._unique_id, reading)
        return reading
