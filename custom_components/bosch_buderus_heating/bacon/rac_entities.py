"""Timers and lifetime energy of the additional-cloud air conditioners."""

from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfEnergy, UnitOfTime
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from ..const import DOMAIN
from ..platform_scope import DiscoveryLifecycle
from . import rac, rac_energy, rac_timers
from .access import BaconAccess
from .entity import BaconEntity
from .models import BaconError, Device

_LOGGER = logging.getLogger(__package__)


def _invalid_command() -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key="bacon_unsupported_command"
    )


class BaconRacTimer(BaconEntity, NumberEntity):
    """Minutes until the unit switches on or off; 0 stops the timer."""

    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 0
    _attr_native_max_value = rac_timers.MAX_MINUTES
    _attr_native_step = 1
    _attr_entity_registry_enabled_default = False

    def __init__(self, account: BaconAccess, serial: str, which: str) -> None:
        super().__init__(account, serial, "rac_timer_" + which)
        self.field = rac_timers.FIELDS[which]
        self._attr_translation_key = "bacon_rac_timer_" + which

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # The remaining time runs down without a message from the device.
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._tick, timedelta(seconds=rac_timers.STEP_SECONDS)
            )
        )

    @callback
    def _tick(self, _now: object) -> None:
        self.async_write_ha_state()

    @property
    def controls_current(self) -> bool:
        return (
            rac.supported(self.device) and self.metadata_current and self.state_current
        )

    @property
    def available(self) -> bool:
        return self.controls_current and rac_timers.declared(self.device, self.field)

    @property
    def native_value(self) -> int | None:
        return (
            rac_timers.remaining_minutes(self.device, self.field)
            if self.available
            else None
        )

    async def async_set_native_value(self, value: float) -> None:
        model = self.device.model
        if (
            model is None
            or type(value) not in (int, float)
            or not math.isfinite(value)
            or value != int(value)
            or not 0 <= value <= rac_timers.MAX_MINUTES
            or not self.available
        ):
            raise _invalid_command()
        minutes = int(value)
        # One time for the whole write, so the check and the command agree.
        at = rac_timers.now()
        desired = {
            self.field: rac_timers.target_time(minutes, at) if minutes else 0,
        }

        def validate(device: Device, patch: dict[str, Any]) -> bool:
            return self.controls_current and rac_timers.command_valid(
                device, patch, model, at
            )

        if not validate(self.device, desired):
            raise _invalid_command()
        try:
            await self.account.write_state(self.serial, desired, validate)
        except BaconError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="bacon_command_failed"
            ) from err


class BaconRacEnergy(BaconEntity, SensorEntity):
    """A lifetime total in kWh. No state class: its reset range is not known."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 1
    _attr_entity_registry_enabled_default = False

    def __init__(self, account: BaconAccess, serial: str, key: str) -> None:
        super().__init__(account, serial, "rac_energy_" + key)
        self.key = key
        self._attr_translation_key = "bacon_rac_energy_" + key
        self.totals = rac_energy.feed(account, serial)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        def start(coroutine: Any) -> Any:
            return self.hass.async_create_background_task(
                coroutine, "home_comfort_rac_energy"
            )

        def log(category: str) -> None:
            _LOGGER.info(
                "The history service does not offer the air conditioner "
                "energy totals (%s); they stay unavailable",
                category,
            )

        self.async_on_remove(
            self.totals.subscribe(self.async_write_ha_state, start, log)
        )

    @property
    def available(self) -> bool:
        return rac.supported(self.device) and self.native_value is not None

    @property
    def native_value(self) -> float | None:
        return self.totals.current(self.key) if rac.supported(self.device) else None


def track_entities(
    entry: DiscoveryLifecycle,
    account: BaconAccess,
    async_add_entities: AddConfigEntryEntitiesCallback,
    platform: str,
) -> None:
    seen: set[tuple[str, str]] = set()

    @callback
    def discover() -> None:
        additions: list[BaconEntity] = []
        for serial, device in account.devices.items():
            if not rac.supported(device):
                continue
            candidates: list[tuple[str, BaconEntity]] = []
            if platform == "sensor":
                candidates.extend(
                    (key, BaconRacEnergy(account, serial, key))
                    for key in rac_energy.TOTALS
                )
            elif device.metadata_generation == account.generation:
                candidates.extend(
                    (which, BaconRacTimer(account, serial, which))
                    for which, field in rac_timers.FIELDS.items()
                    if rac_timers.declared(device, field)
                )
            for key, entity in candidates:
                if (serial, key) not in seen:
                    seen.add((serial, key))
                    additions.append(entity)
        if additions:
            async_add_entities(additions)

    entry.async_on_unload(account.add_listener(discover))
    discover()
