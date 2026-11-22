"""Sensors of the periods of the plant's recordings.

One sensor per measure, period and method. Its value is the last one the
manager published: a past period (yesterday, previous month, previous year)
changes only once the new period is final, so for a while after midnight or
the first of a month the attributes ``period_start``/``period_end`` still
name the period before. The values are no counters (no state class): the
hourly values go into the statistics through the optional import.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import (
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ..device import device_info_for_resource, grouped_entity_name
from ..platform_scope import DiscoveryLifecycle
from .gating import heat_pump_source
from .manager import RecordingsManager, manager_for
from .measures import EntitySpec, entity_name
from .series import Quantity

if TYPE_CHECKING:
    from ..coordinator import BoschBuderusDataUpdateCoordinator

_KINDS: dict[Quantity, tuple[SensorDeviceClass | None, str | None, int]] = {
    Quantity.ENERGY: (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, 2),
    Quantity.DURATION: (SensorDeviceClass.DURATION, UnitOfTime.HOURS, 1),
    Quantity.TEMPERATURE: (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS, 1),
    Quantity.VOLUME: (SensorDeviceClass.WATER, UnitOfVolume.LITERS, 0),
    Quantity.COUNT: (None, None, 0),
}


def unique_id(gateway_id: str, spec: EntitySpec) -> str:
    """``<gateway>:recordings:<path>:<period>[:calculated]``."""
    return f"{gateway_id}:recordings:{spec.key}"


def track_recording_sensors(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the period sensors of every series that answered, each once."""
    manager = manager_for(coordinator)
    if manager is None:
        return
    seen: set[str] = set()

    @callback
    def discover() -> None:
        if not manager.loaded:
            return
        additions = []
        for spec in manager.specs():
            if spec.key not in seen:
                seen.add(spec.key)
                additions.append(RecordingPeriodSensor(coordinator, manager, spec))
        if additions:
            async_add_entities(additions)

    discover()
    entry.async_on_unload(manager.add_listener(discover))
    entry.async_on_unload(partial(manager.detach, coordinator))


class RecordingPeriodSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _unrecorded_attributes = frozenset(
        {
            "period_start",
            "period_end",
            "source",
            "resolution",
            "derived_from",
            "method",
            "counter_history_start",
            "heat_generator",
        }
    )

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        manager: RecordingsManager,
        spec: EntitySpec,
    ) -> None:
        self._manager, self._spec = manager, spec
        measure = spec.measure
        language = coordinator.hass.config.language
        self._attr_name = grouped_entity_name(
            coordinator, measure.anchor, entity_name(language, spec)
        )
        self._attr_unique_id = unique_id(coordinator.gateway.gateway_id, spec)
        self._attr_device_info = device_info_for_resource(coordinator, measure.anchor)
        device_class, unit, precision = _KINDS[measure.quantity]
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_suggested_display_precision = precision
        self._attr_entity_registry_enabled_default = spec.enabled_default
        self._release: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._release = self._manager.request(self._spec.key)
        self.async_on_remove(self._manager.add_listener(self._updated))

    async def async_will_remove_from_hass(self) -> None:
        if self._release is not None:
            self._release()
            self._release = None
        await super().async_will_remove_from_hass()

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # H-B3-7: a value that does not exist (yet) is "unknown"; "unavailable"
        # only when the source cannot be reached: no value shown and no source
        # attempt attached to the recordings.
        return (
            self._manager.shown(self._spec) is not None
            or self._manager.coordinator() is not None
        )

    @property
    def native_value(self) -> float | None:
        shown = self._manager.shown(self._spec)
        return shown.value if shown is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        shown = self._manager.shown(self._spec)
        measure = self._spec.measure
        attributes: dict[str, Any] = {"method": self._spec.method}
        if (
            measure.kind == "heat"
            and measure.mode in ("total", "ch", "dhw")
            and heat_pump_source(self._manager.coordinator()) is not None
        ):
            # A hybrid system: the gateway's heat is the boiler's; the heat
            # pump's own heat is recorded under its heat source.
            attributes["heat_generator"] = "boiler"
        if shown is None:
            return attributes
        if shown.source == "counters":
            counter = self._manager.counter_entities.get(measure.key)
            derived = [counter] if counter else []
        else:
            derived = measure.derived_from()
        attributes.update(
            {
                "period_start": shown.start.isoformat(),
                # The last day of the period.
                "period_end": shown.end.fromordinal(
                    shown.end.toordinal() - 1
                ).isoformat(),
                "source": shown.source,
                "resolution": shown.resolution,
                "derived_from": derived,
            }
        )
        if shown.history_start is not None:
            # A value from the counters covers the period only from here on.
            attributes["counter_history_start"] = shown.history_start.isoformat()
        return attributes
