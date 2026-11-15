"""The entities of the variable tariff of an iCom gateway (``variable_tariff``).

Diagnostic, disabled by default; they read what the regular cycle read and
never send a request.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .control import track_entity_once
from .coordinator import Freshness
from .device import device_info_for_resource
from .variable_tariff import (
    NO_TARIFF_ID,
    TARIFF_ENABLE_PATH,
    TARIFF_ID_PATH,
    tariff_enabled,
    tariff_id,
)

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .platform_scope import DiscoveryLifecycle
    from .pointt import Resource


def _unique_id(gateway_id: str, path: str) -> str:
    return f"{gateway_id}:{path.strip('/').replace('/', ':')}"


def _current(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> Resource | None:
    snapshot = (coordinator.data or {}).get(path)
    if (
        snapshot is None
        or not snapshot.available
        or snapshot.freshness is not Freshness.FRESH
        or snapshot.resource.path != path
    ):
        return None
    return snapshot.resource


class _TariffEntity(CoordinatorEntity["BoschBuderusDataUpdateCoordinator"]):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _path: str

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = _unique_id(coordinator.gateway.gateway_id, self._path)

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self._path)

    async def async_update(self) -> None:
        """Read with the regular cycle; never a request of its own."""


class VariableTariffActiveBinarySensor(_TariffEntity, BinarySensorEntity):
    """On while the gateway's tariff optimisation is switched on."""

    _attr_translation_key = "variable_tariff_active"
    _path = TARIFF_ENABLE_PATH

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None

    @property
    def is_on(self) -> bool | None:
        return tariff_enabled(_current(self.coordinator, self._path))


class TariffIdSensor(_TariffEntity, SensorEntity):
    """The number of the chosen tariff (no name, no prices)."""

    _attr_translation_key = "tariff_id"
    _path = TARIFF_ID_PATH

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> int | None:
        return tariff_id(_current(self.coordinator, self._path))

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        value = self.native_value
        if value is None:
            return None
        return {"tariff_selected": value != NO_TARIFF_ID}


def _track(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    domain: str,
    path: str,
    create: type[_TariffEntity],
) -> None:
    registry = er.async_get(coordinator.hass)
    unique_id = _unique_id(coordinator.gateway.gateway_id, path)

    def ready() -> bool:
        return (
            path in coordinator.resources
            or registry.async_get_entity_id(domain, DOMAIN, unique_id) is not None
        )

    track_entity_once(
        entry, coordinator, async_add_entities, ready, partial(create, coordinator)
    )


def track_tariff_binary_sensor(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the binary sensor once the gateway reported the path."""
    _track(
        entry,
        coordinator,
        async_add_entities,
        "binary_sensor",
        TARIFF_ENABLE_PATH,
        VariableTariffActiveBinarySensor,
    )


def track_tariff_sensor(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensor once the gateway reported the path."""
    _track(
        entry, coordinator, async_add_entities, "sensor", TARIFF_ID_PATH, TariffIdSensor
    )
