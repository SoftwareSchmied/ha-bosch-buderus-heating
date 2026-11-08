"""Program selection for numeric RRC program references."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import rrc
from .const import DOMAIN
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource, grouped_entity_name
from .pointt import (
    AuthenticationError,
    PointTError,
    RateLimited,
    Resource,
    WriteNotConfirmed,
    WriteValidationError,
)
from .rrc_programs import selection_options


@dataclass(frozen=True, kw_only=True)
class RrcSelectDescription(SelectEntityDescription):
    resource_path: str


def descriptions(resources: Mapping[str, Resource]) -> tuple[RrcSelectDescription, ...]:
    return tuple(
        RrcSelectDescription(key=path.strip("/").replace("/", ":"), resource_path=path)
        for path, resource in resources.items()
        if selection_options(resource, resources.get(rrc.PROGRAM_LIST))
    )


class RrcProgramSelect(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], SelectEntity
):
    _attr_has_entity_name = True
    entity_description: RrcSelectDescription
    # The options come from the program list (G3/O51: read for this entity).
    extra_resource_paths = (rrc.PROGRAM_LIST,)

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: RrcSelectDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:{description.key}:control"
        )
        self._attr_name = grouped_entity_name(
            coordinator,
            description.resource_path,
            "Zeitprogramm wählen"
            if coordinator.hass.config.language.startswith("de")
            else "Select heating schedule",
        )

    @property
    def options(self) -> list[str]:
        snapshots = self.coordinator.data or {}
        current = snapshots.get(self.entity_description.resource_path)
        programs = snapshots.get(rrc.PROGRAM_LIST)
        if any(
            item is None or not item.available or item.freshness is not Freshness.FRESH
            for item in (current, programs)
        ):
            return []
        assert current is not None and programs is not None
        return [
            str(value)
            for value in selection_options(current.resource, programs.resource)
        ]

    @property
    def available(self) -> bool:
        return super().available and bool(self.options)

    @property
    def current_option(self) -> str | None:
        snapshot = (self.coordinator.data or {}).get(
            self.entity_description.resource_path
        )
        value = rrc.scalar_value(snapshot.resource) if snapshot else None
        option = str(value)
        return option if option in self.options else None

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="write_validation_failed"
            )
        try:
            await self.coordinator.async_select_rrc_program(
                self.entity_description.resource_path, int(option)
            )
        except PointTError as err:
            key = (
                "write_validation_failed"
                if isinstance(err, WriteValidationError)
                else "write_not_confirmed"
                if isinstance(err, WriteNotConfirmed)
                else "write_rate_limited"
                if isinstance(err, RateLimited)
                else "write_authentication_failed"
                if isinstance(err, AuthenticationError)
                else "write_failed"
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key=key
            ) from err

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(
            self.coordinator, self.entity_description.resource_path
        )
