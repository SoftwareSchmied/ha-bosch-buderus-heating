"""Native air purifier power and presets, without a fabricated speed percentage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.fan import FanEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BoschBuderusConfigEntry
from .bacon import purifier
from .bacon.purifier_entities import PurifierEntity, invalid_command
from .bacon.session import BaconAccount

if TYPE_CHECKING:
    from homeassistant.components.fan.const import FanEntityFeature as Feature
else:
    from homeassistant.components.fan import FanEntityFeature as Feature


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if (account := entry.runtime_data.bacon) is None:
        return
    seen: set[str] = set()

    @callback
    def discover() -> None:
        additions = []
        for serial, device in account.devices.items():
            if serial not in seen and purifier.family(device) is not None:
                seen.add(serial)
                additions.append(BaconPurifierFan(account, serial))
        if additions:
            async_add_entities(additions)

    entry.async_on_unload(account.add_listener(discover))
    discover()


class BaconPurifierFan(PurifierEntity, FanEntity):
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, account: BaconAccount, serial: str) -> None:
        super().__init__(account, serial, "fan")

    @property
    def available(self) -> bool:
        return (
            self.controls_current
            and type(purifier.reported(self.device, "powerEnabled")) is bool
        )

    @property
    def is_on(self) -> bool | None:
        return (
            purifier.reported(self.device, "powerEnabled") if self.available else None
        )

    @property
    def preset_modes(self) -> list[str]:
        return (
            purifier.options(self.device, "opMode", purifier.MODES)
            if self.controls_current
            and purifier.control_available(self.device, "opMode")
            else []
        )

    @property
    def preset_mode(self) -> str | None:
        value = purifier.reported(self.device, "opMode")
        return value if self.controls_current and value in purifier.MODES else None

    @property
    def supported_features(self) -> Feature:
        result = Feature(0)
        if self.controls_current and purifier.boolean_control(
            self.device, "powerEnabled"
        ):
            result |= Feature.TURN_OFF
            if purifier.reported(self.device, "opMode") in purifier.options(
                self.device, "opMode", purifier.MODES
            ):
                result |= Feature.TURN_ON
        if self.preset_modes:
            result |= Feature.PRESET_MODE
        return result

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        if percentage is not None or kwargs:
            raise invalid_command()
        desired: dict[str, Any] = {"powerEnabled": True}
        if preset_mode is not None:
            desired["opMode"] = preset_mode
        await self.write(desired)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.write({"powerEnabled": False})

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self.write({"opMode": preset_mode})

    async def async_set_percentage(self, percentage: int) -> None:
        raise invalid_command()
