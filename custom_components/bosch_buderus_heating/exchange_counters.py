"""P50 (4B): counters keep clean statistics across a gateway exchange.

After an exchange it is unknown whether a counter lives in the heat source (it
continues) or in the gateway or the cloud (it starts again). Each counter
compares its first values with the last value recorded before the exchange:

* not lower: the counter continued, nothing changes;
* lower by more than HA's reset threshold: it started again. A
  ``total_increasing`` counter is a meter reset for HA; a ``total`` counter
  reports the exchange time as ``last_reset`` from then on;
* slightly lower: the entity shows no value until the counter reaches the
  recorded value again, a gap instead of a negative consumption.

The decision is stored per entity, so a settled counter never asks the
recorder again and ``last_reset`` stays identical across restarts.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from math import isfinite

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .gateway_exchange import CounterRecord, ModuleFingerprints, module_fingerprints

_LOGGER = logging.getLogger(__name__)

# Only an exchange within this period is guarded.
GUARD_PERIOD = timedelta(days=30)
# HA's own limit: a total_increasing value below 90 % is a meter reset.
RESET_RATIO = 0.9
COUNTER_STATE_CLASSES = frozenset(
    {SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING}
)


@dataclass(frozen=True, slots=True)
class RecordedValue:
    """The last statistics state of an entity, in its statistics unit."""

    value: float
    unit: str | None


# Rows read per statistics table when looking for the value before an exchange.
_ROWS = 24


async def async_last_recorded_value(
    hass: HomeAssistant, statistic_id: str, before: datetime
) -> RecordedValue | None:
    """The last recorded statistics state from before ``before``.

    None without a loaded recorder or without such a value. Short-term
    statistics come first; the hourly ones cover a longer outage.
    """
    from homeassistant.helpers.recorder import DATA_INSTANCE

    if DATA_INSTANCE not in hass.data:
        return None
    from homeassistant.components.recorder import (  # type: ignore[attr-defined,unused-ignore]
        get_instance,
    )
    from homeassistant.components.recorder.statistics import (
        get_last_short_term_statistics,
        get_last_statistics,
        get_metadata,
    )

    instance = get_instance(hass)
    # Shielded: a cancelled entity must never cancel the recorder's future.
    if not await asyncio.shield(instance.async_db_ready):
        return None
    limit = before.timestamp()

    def read() -> RecordedValue | None:
        metadata = get_metadata(hass, statistic_ids={statistic_id})
        if statistic_id not in metadata:
            return None
        unit = metadata[statistic_id][1].get("unit_of_measurement")
        for fetch in (get_last_short_term_statistics, get_last_statistics):
            # Several rows: the newest may already be from after the exchange.
            rows = fetch(hass, _ROWS, statistic_id, False, {"state"}).get(statistic_id)
            for row in sorted(rows or (), key=lambda item: item["start"], reverse=True):
                state = row.get("state")
                # A row's state is the value at its end, so the whole period
                # must lie before the exchange (an hourly row starting before
                # it may still hold a later value).
                end = row.get("end")
                if (
                    isinstance(end, int | float)
                    and end <= limit
                    and isinstance(state, int | float)
                    and isfinite(state)
                ):
                    return RecordedValue(float(state), unit)
        return None

    try:
        return await instance.async_add_executor_job(read)
    except Exception:  # statistics are optional evidence
        _LOGGER.debug("No recorded counter value for %s", statistic_id)
        return None


def _converted(
    recorded: RecordedValue, unit: str | None, device_class: SensorDeviceClass | None
) -> float | None:
    if recorded.unit == unit:
        return recorded.value
    from homeassistant.components.sensor.const import UNIT_CONVERTERS

    converter = UNIT_CONVERTERS.get(device_class)
    if (
        converter is None
        or recorded.unit not in converter.VALID_UNITS
        or unit not in converter.VALID_UNITS
    ):
        return None
    return converter.convert(recorded.value, recorded.unit, unit)


class _Phase(Enum):
    OFF = auto()  # values pass unchanged
    PENDING = auto()  # the recorded value is still being read: no value
    GUARDING = auto()  # values are compared with the recorded value


class ExchangeCounterGuard:
    """The 4B rule for one counter entity of a managed target."""

    # O25: one guard per counter entity; no per-instance dictionary.
    __slots__ = (
        "_at",
        "_baseline",
        "_exchanged",
        "_phase",
        "_store",
        "_target_id",
        "_total",
        "_unique_id",
    )

    def __init__(
        self,
        store: ModuleFingerprints,
        target_id: str,
        unique_id: str,
        state_class: SensorStateClass,
    ) -> None:
        self._store, self._target_id, self._unique_id = store, target_id, unique_id
        self._total = state_class == SensorStateClass.TOTAL
        self._phase = _Phase.OFF
        self._at: str | None = None
        self._exchanged: datetime | None = None
        self._baseline: float | None = None

    def start(self) -> datetime | None:
        """The exchange time when the recorder must be asked, otherwise None."""
        at = self._store.exchange(self._target_id)
        exchanged = dt_util.parse_datetime(at) if at is not None else None
        record = self._store.counter(self._unique_id)
        if (
            at is None
            or exchanged is None
            or exchanged.tzinfo is None
            or not self._recent(exchanged)
            or (record is not None and record.at == at and record.settled)
        ):
            return None
        self._at, self._exchanged, self._phase = at, exchanged, _Phase.PENDING
        return exchanged

    def recorded(
        self,
        recorded: RecordedValue | None,
        unit: str | None,
        device_class: SensorDeviceClass | None,
    ) -> None:
        """Without a usable recorded value nothing is guarded."""
        if self._phase is not _Phase.PENDING:
            return
        baseline = _converted(recorded, unit, device_class) if recorded else None
        if baseline is None:
            self._phase = _Phase.OFF
            return
        self._baseline, self._phase = baseline, _Phase.GUARDING

    @property
    def last_reset(self) -> datetime | None:
        """The stored new cycle of a ``total`` counter, identical on every start."""
        record = self._store.counter(self._unique_id)
        if not self._total or record is None or record.last_reset is None:
            return None
        return dt_util.parse_datetime(record.last_reset)

    def value(self, value: float) -> float | None:
        """The value to show for a fresh reading of the counter."""
        if self._phase is _Phase.PENDING:
            return None
        if self._phase is _Phase.OFF:
            return value
        baseline, exchanged = self._baseline, self._exchanged
        if baseline is None or exchanged is None or not self._recent(exchanged):
            self._phase = _Phase.OFF
            return value
        if value >= baseline:
            self._settle()  # continued
            return value
        if value < RESET_RATIO * baseline:
            # Started again. ``total`` also falls slightly, for example while
            # defrosting, so only the same clear drop counts as a new cycle.
            self._settle(last_reset=self._at if self._total else None)
            return value
        return None  # a gap until the counter reaches the recorded value again

    def _settle(self, *, last_reset: str | None = None) -> None:
        self._phase = _Phase.OFF
        if self._at is None:
            return
        previous = self._store.counter(self._unique_id)
        if last_reset is None and previous is not None:
            last_reset = previous.last_reset  # an earlier cycle stays
        self._store.set_counter(
            self._unique_id,
            CounterRecord(self._at, True, last_reset),
            immediate=last_reset is not None,
        )

    @staticmethod
    def _recent(exchanged: datetime) -> bool:
        return dt_util.utcnow() - exchanged <= GUARD_PERIOD


async def async_counter_guard(
    hass: HomeAssistant,
    entry_id: str,
    target_id: str,
    unique_id: str,
    state_class: SensorStateClass,
) -> ExchangeCounterGuard:
    store = module_fingerprints(hass, entry_id)
    await store.async_load()
    return ExchangeCounterGuard(store, target_id, unique_id, state_class)
