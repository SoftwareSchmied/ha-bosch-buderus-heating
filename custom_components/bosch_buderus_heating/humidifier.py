"""Native dehumidifiers for identified Dry-family devices."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.humidifier import HumidifierEntity
from homeassistant.components.humidifier.const import (
    HumidifierAction,
    HumidifierEntityFeature,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BoschBuderusConfigEntry
from .bacon import dry
from .bacon.access import BaconAccess
from .bacon.dry_entities import DryEntity
from .bacon.metadata import metadata, writable
from .platform_scope import PlatformScope, setup_platform

if TYPE_CHECKING:
    from homeassistant.components.humidifier.const import HumidifierDeviceClass
else:
    from homeassistant.components.humidifier import HumidifierDeviceClass


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    setup_platform(entry, Platform.HUMIDIFIER, async_add_entities, _setup_entities)


def _setup_entities(
    entry: PlatformScope,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if (account := entry.bacon) is None:
        return
    seen: set[str] = set()

    @callback
    def discover() -> None:
        additions = []
        for serial, device in account.devices.items():
            if serial not in seen and dry.family(device) is not None:
                seen.add(serial)
                additions.append(BaconDehumidifier(account, serial))
        if additions:
            async_add_entities(additions)

    entry.async_on_unload(account.add_listener(discover))
    discover()


class BaconDehumidifier(DryEntity, HumidifierEntity):
    _attr_device_class = HumidifierDeviceClass.DEHUMIDIFIER

    def __init__(self, account: BaconAccess, serial: str) -> None:
        super().__init__(account, serial, "dehumidifier")

    @property
    def available(self) -> bool:
        return (
            self.controls_current
            and type(dry.reported(self.device, "powerEnabled")) is bool
        )

    @property
    def is_on(self) -> bool | None:
        return dry.reported(self.device, "powerEnabled") if self.available else None

    @property
    def current_humidity(self) -> float | None:
        return (
            dry.measurement(self.device, "relHumidity")
            if self.observations_current
            else None
        )

    @property
    def target_humidity(self) -> float | None:
        return dry.target(self.device) if self.controls_current else None

    @property
    def min_humidity(self) -> float:
        limits = dry.target_range(self.device) if self.controls_current else None
        return limits.minimum if limits else 0

    @property
    def max_humidity(self) -> float:
        limits = dry.target_range(self.device) if self.controls_current else None
        return limits.maximum if limits else 0

    @property
    def target_humidity_step(self) -> float | None:
        limits = dry.target_range(self.device) if self.controls_current else None
        return limits.step if limits else None

    @property
    def mode(self) -> str | None:
        value = dry.reported(self.device, "opMode")
        return value if self.controls_current and value in dry.MODES else None

    @property
    def available_modes(self) -> list[str]:
        return (
            dry.options(self.device)
            if self.controls_current
            and writable(metadata(self.device, "state", "opMode"))
            else []
        )

    @property
    def supported_features(self) -> HumidifierEntityFeature:
        return (
            HumidifierEntityFeature.MODES
            if self.available_modes
            else HumidifierEntityFeature(0)
        )

    @property
    def action(self) -> HumidifierAction | None:
        if self.is_on is False:
            return HumidifierAction.OFF
        return {"running": HumidifierAction.DRYING, "idle": HumidifierAction.IDLE}.get(
            self.operating_status or ""
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.write({"powerEnabled": True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.write({"powerEnabled": False})

    async def async_set_humidity(self, humidity: int) -> None:
        await self.write({"humiditySetpoint": humidity})

    async def async_set_mode(self, mode: str) -> None:
        await self.write({"opMode": mode})
