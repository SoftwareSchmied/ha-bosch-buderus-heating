"""Energy estimated from the reported current power of a heat source.

Only for heat sources without a native consumption or energy counter. The
value integrates ``/heatSources/<hs>/actualPower`` over time; whether that
power is the fuel input or the delivered heat is not confirmed, so the entity
is named and described as an estimate, never as gas or electricity use.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Mapping
from datetime import timedelta
from decimal import Decimal
from math import isfinite
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    SnapshotSource,
)
from .derived import (
    PowerSample,
    estimated_energy_kwh,
    estimation_gap_limit,
    power_kilowatts,
)
from .device import device_info_for_resource, grouped_entity_name
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource

TRANSLATION_KEY = "estimated_energy"
UNIQUE_SUFFIX = "estimated_energy"
_NAMES = {
    "de": "Energie (geschätzt aus aktueller Leistung)",
    "en": "Energy (estimated from current power)",
}
_POWER_PATH = re.compile(r"^/heatSources/(?P<source>hs[^/]+)/actualPower$")
# A native counter of the plant covers every heat source; a counter of one
# heat source covers that source. Either one replaces the estimate.
_PLANT_COUNTER = re.compile(r"^/heatSources/(?:[A-Za-z]+Consumption|emon/[^/]+)$")
_SOURCE_COUNTER = r"^/heatSources/{source}/(?:[A-Za-z]+Consumption|emon/[^/]+)$"
_POWER_UNITS = frozenset({"kW", "W"})
_LOGGER = logging.getLogger(__name__)
# The running totals are saved in the integration's own store, at most once a
# minute and on shutdown: the recorder's restore data is written only every
# 15 minutes, and a lower total after a crash would count as a meter reset.
STORAGE_KEY = f"{DOMAIN}.estimated_energy"
STORAGE_VERSION = 1
SAVE_DELAY_SECONDS = 60
MAX_TOTALS = 256
_STORE: HassKey[EstimatedEnergyStore] = HassKey(f"{DOMAIN}_estimated_energy")


class EstimatedEnergyStore:
    """HA-wide running totals of the estimates, by unique ID."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self._totals: dict[str, float] = {}
        self._loaded = False
        self._pending = False

    async def async_total(self, unique_id: str) -> float | None:
        """The saved total; loaded once, a damaged file starts empty."""
        if not self._loaded:
            try:
                stored = await self._store.async_load()
            except Exception:
                _LOGGER.warning("Saved estimated energy totals could not be read")
                stored = None
            if not self._loaded:
                totals = stored.get("totals") if isinstance(stored, dict) else None
                if isinstance(totals, dict):
                    for key, value in list(totals.items())[:MAX_TOTALS]:
                        number = _restored_total(value)
                        if isinstance(key, str) and number is not None:
                            self._totals[key] = number
                self._loaded = True
        return self._totals.get(unique_id)

    def set(self, unique_id: str, total: float) -> None:
        """Remember a higher total; saved with a delay and on shutdown."""
        if total <= self._totals.get(unique_id, -1.0):
            return
        if unique_id not in self._totals and len(self._totals) >= MAX_TOTALS:
            return
        self._totals[unique_id] = total
        if not self._pending:
            # Not postponed by later totals: at most a minute old.
            self._pending = True
            self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    async def async_forget_gateways(self, gateway_ids: Collection[str]) -> None:
        """Drop the totals of gateways no entry uses any more (PRIV-1)."""
        await self.async_total("")
        prefixes = tuple(f"{gateway_id}:" for gateway_id in gateway_ids)
        forgotten = [key for key in self._totals if key.startswith(prefixes)]
        if not prefixes or not forgotten:
            return
        for key in forgotten:
            del self._totals[key]
        self._pending = False
        await self._store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        self._pending = False
        return {"totals": dict(self._totals)}


def estimated_energy_store(hass: HomeAssistant) -> EstimatedEnergyStore:
    store = hass.data.get(_STORE)
    if store is None:
        store = hass.data[_STORE] = EstimatedEnergyStore(hass)
    return store


def estimated_energy_paths(
    resources: Mapping[str, Resource], known_paths: Collection[str] = ()
) -> tuple[str, ...]:
    """``actualPower`` paths of heat sources without a native counter.

    ``known_paths`` adds paths whose entities exist from an earlier start but
    were not read yet, so a counter missing in one start never brings the
    estimate in.
    """
    paths = {*resources, *known_paths}
    if any(_PLANT_COUNTER.fullmatch(path) for path in paths):
        return ()
    result: list[str] = []
    for path in sorted(resources):
        match = _POWER_PATH.fullmatch(path)
        if match is None or resources[path].metadata.unit not in _POWER_UNITS:
            continue
        counter = re.compile(
            _SOURCE_COUNTER.format(source=re.escape(match.group("source")))
        )
        if not any(counter.fullmatch(item) for item in paths):
            result.append(path)
    return tuple(result)


def estimated_energy_unique_id(gateway_id: str, power_path: str) -> str:
    """``<gateway>:heatSources:<hs>:actualPower:estimated_energy``."""
    return f"{gateway_id}:{power_path.strip('/').replace('/', ':')}:{UNIQUE_SUFFIX}"


def track_estimated_energy_sensors(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one estimate per eligible heat source, each exactly once."""
    seen: set[str] = set()
    checked: list[frozenset[str]] = []

    @callback
    def discover() -> None:
        paths = frozenset(coordinator.resources)
        if checked and checked[-1] == paths:
            return  # nothing new since the last check
        checked[:] = [paths]
        additions = []
        for path in estimated_energy_paths(
            coordinator.resources, coordinator.startup_data.registered_paths()
        ):
            if path not in seen:
                seen.add(path)
                additions.append(BoschBuderusEstimatedEnergySensor(coordinator, path))
        if additions:
            async_add_entities(additions)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


class BoschBuderusEstimatedEnergySensor(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], RestoreSensor
):
    """Integrate the reported current power into a non-decreasing kWh total.

    Disabled by default; it counts only while enabled. The total survives a
    restart (the higher of the own saved total and the restored state); the
    first reading afterwards only sets the starting point. Within a run the
    shown total never decreases.
    """

    _attr_has_entity_name = True
    _attr_translation_key = TRANSLATION_KEY
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = frozenset(
        {
            "estimate",
            "method",
            "sampling_basis",
            "sampling_interval_minutes",
            "max_gap_minutes",
        }
    )

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, power_path: str
    ) -> None:
        super().__init__(coordinator)
        self._power_path = power_path
        german = coordinator.hass.config.language.casefold().startswith("de")
        self._attr_name = grouped_entity_name(
            coordinator, power_path, _NAMES["de" if german else "en"]
        )
        self._attr_unique_id = estimated_energy_unique_id(
            coordinator.gateway.gateway_id, power_path
        )
        self._total: float | None = None
        self._previous: PowerSample | None = None
        self._last_reading: tuple[str, object] | None = None
        self._totals_store = estimated_energy_store(coordinator.hass)
        # Counting starts once the saved totals are read.
        self._ready = False

    @property
    def available(self) -> bool:
        # The total is held across gaps and outages; it simply stops growing.
        return self._total is not None

    @property
    def native_value(self) -> float | None:
        return None if self._total is None else round(self._total, 4)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attributes: dict[str, object] = {
            "estimate": True,
            "method": "integration of the reported current power",
            "sampling_basis": "successive fresh readings; longer gaps not counted",
        }
        interval = self._interval()
        if interval is not None:
            attributes["sampling_interval_minutes"] = round(
                interval.total_seconds() / 60, 2
            )
        attributes["max_gap_minutes"] = round(
            estimation_gap_limit(interval).total_seconds() / 60, 2
        )
        return attributes

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self._power_path)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        restored = await self.async_get_last_sensor_data()
        candidates = [await self._totals_store.async_total(self._unique_id())]
        if (
            restored is not None
            and restored.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
        ):
            candidates.append(_restored_total(restored.native_value))
        known = [value for value in candidates if value is not None]
        if known:
            self._raise_total(max(known))
        self._ready = True
        self._sample()

    @callback
    def _handle_coordinator_update(self) -> None:
        if not self._ready:
            return  # the saved totals are still being read
        self._sample()
        super()._handle_coordinator_update()

    def _unique_id(self) -> str:
        return str(self._attr_unique_id)

    def _raise_total(self, total: float) -> None:
        """Only ever upwards; saved once the entity counts."""
        if self._total is not None and total <= self._total:
            return
        self._total = total
        if self._ready:
            self._totals_store.set(self._unique_id(), total)

    def _interval(self) -> timedelta | None:
        return self.coordinator.scheduled_interval(self._power_path)[1]

    def _sample(self) -> None:
        """Take one new reading; integrate it against the previous one."""
        snapshot = self.coordinator.display_snapshot(self._power_path)
        if (
            snapshot is None
            or not snapshot.available
            or snapshot.freshness is not Freshness.FRESH
        ):
            # Unavailable, expired or unconfirmed: never bridged.
            self._previous = None
            return
        origin = "local" if snapshot.source is SnapshotSource.LOCAL else "cloud"
        reading = (origin, snapshot.last_success)
        if reading == self._last_reading:
            return  # the same reading again, for example a kept value
        self._last_reading = reading
        resource = snapshot.resource
        power = power_kilowatts(
            resource.value,
            resource.metadata.unit,
            resource.metadata.numeric_error_values,
        )
        if power is None:
            self._previous = None
            return
        sample = PowerSample(snapshot.last_success, power, origin)
        previous = self._previous
        if previous is not None and sample.observed_at <= previous.observed_at:
            # Never step back to an older reading, also of the other access.
            return
        increment = estimated_energy_kwh(
            previous, sample, estimation_gap_limit(self._interval())
        )
        self._previous = sample
        if self._total is None:
            self._total = 0.0
        self._raise_total(self._total + max(0.0, increment))


def _restored_total(value: object) -> float | None:
    """A finite, non-negative stored total, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return None
    number = float(value)
    return number if isfinite(number) and number >= 0.0 else None
