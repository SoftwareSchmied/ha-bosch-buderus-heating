"""Z1: the select of the active weekly program (A/B) of an EMS/iCom circuit."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .control import HeldCapabilities, async_set_control
from .control_presentation import control_name
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource
from .pointt import Resource
from .resource_catalog import resource_name
from .switch_program_selection import (
    ACTIVE_SWITCH_PROGRAM_POLICY,
    PROGRAMS,
    is_active_program_path,
    name_path,
    offered_programs,
    program_name,
    program_path,
)
from .writes import ControlContext, assess_control, context_of


@dataclass(frozen=True, kw_only=True)
class SwitchProgramSelectDescription(SelectEntityDescription):
    resource_path: str


def _offered(
    resource: Resource,
    readings: Mapping[str, Resource],
    context: ControlContext | None,
) -> tuple[str, ...]:
    assessment = assess_control(resource, context)
    if (
        not assessment.eligible
        or assessment.policy is not ACTIVE_SWITCH_PROGRAM_POLICY
        or resource.value not in assessment.options
    ):
        return ()
    return offered_programs(assessment.options, resource.path, readings)


def descriptions(
    resources: Mapping[str, Resource], context: ControlContext | None = None
) -> tuple[SwitchProgramSelectDescription, ...]:
    """A selection per circuit whose programs can all be read (Z1)."""
    return tuple(
        SwitchProgramSelectDescription(
            key=path.strip("/").replace("/", ":"),
            resource_path=path,
            translation_key="active_switch_program",
            entity_category=EntityCategory.CONFIG,
            icon="mdi:calendar-clock",
        )
        for path, resource in resources.items()
        if is_active_program_path(path) and _offered(resource, resources, context)
    )


def _option(program: str) -> str:
    return program.lower()


class SwitchProgramSelect(
    HeldCapabilities,
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator],
    SelectEntity,
):
    """Switch between the weekly programs A and B; one confirmed write."""

    _attr_has_entity_name = True
    entity_description: SwitchProgramSelectDescription
    # The names change rarely; the selected program is the state.
    _unrecorded_attributes = frozenset({"program_names"})

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: SwitchProgramSelectDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        path = description.resource_path
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:{description.key}:control"
        )
        self._attr_name = control_name(
            coordinator,
            path,
            resource_name(path, language=coordinator.hass.config.language),
        )
        # The programs and names this entity shows (G3/O51).
        self.extra_resource_paths = tuple(
            item
            for program in PROGRAMS
            for item in (program_path(path, program), name_path(path, program))
        )

    def _readings(self) -> dict[str, Resource]:
        return {
            path: snapshot.resource
            for path, snapshot in (self.coordinator.data or {}).items()
            if snapshot.available and snapshot.freshness is Freshness.FRESH
        }

    def _programs(self) -> tuple[str, ...]:
        readings = self._readings()
        current = readings.get(self.entity_description.resource_path)
        if current is None:
            return ()
        return _offered(current, readings, context_of(self.coordinator))

    @property
    def available(self) -> bool:
        return super().available and bool(self._programs())

    @property
    def options(self) -> list[str]:
        return [_option(program) for program in self._programs()]

    @property
    def current_option(self) -> str | None:
        snapshot = (self.coordinator.data or {}).get(
            self.entity_description.resource_path
        )
        value = snapshot.resource.value if snapshot is not None else None
        programs = self._programs()
        return _option(value) if isinstance(value, str) and value in programs else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The program names the gateway reports, next to the stable options;
        the options stay ``a``/``b`` when a program is renamed."""
        readings = self._readings()
        path = self.entity_description.resource_path
        names = {
            _option(program): name
            for program in self._programs()
            if (name := program_name(readings.get(name_path(path, program))))
        }
        current = self.current_option
        return {
            "program_names": names,
            "program_name": names.get(current) if current is not None else None,
        }

    async def async_select_option(self, option: str) -> None:
        program = next(
            (item for item in self._programs() if _option(item) == option), None
        )
        snapshots = self.coordinator.data or {}
        path = self.entity_description.resource_path
        current = snapshots.get(path)
        target = snapshots.get(program_path(path, program)) if program else None
        if program is None or current is None or target is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="write_validation_failed"
            )
        # The selection and the program it names are read again with the
        # write; a change on the gateway meanwhile sends nothing.
        await async_set_control(
            self.coordinator,
            path,
            program,
            ACTIVE_SWITCH_PROGRAM_POLICY,
            expected=(current.resource, target.resource),
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(
            self.coordinator, self.entity_description.resource_path
        )
