"""RRC 2.0 entities: away mode, events calendar, boost, fireplace.

Owned by work package W2. ``rrc_platforms.track`` calls the tracker of each
platform listed in ``TRACKERS`` once per coordinator; see
``docs/dev/rrc-packages.md``.

Away mode, its temperature and the fireplace function are generic switches
and numbers of the contract (``rrc_contract_absence``). This module adds the
events calendar and the boost entities: a switch and two numbers over the
one structure ``boostShortcut`` (``rrc_boost``). All of it is expected,
untested on hardware.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory, Platform, UnitOfTemperature, UnitOfTime
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import rrc_boost, rrc_events
from .const import DOMAIN
from .control import track_rrc_entities
from .control_presentation import control_name
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource
from .i18n import localize
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource, WriteValidationError
from .rrc_boost import BOOST_PATH, Boost, Setting
from .rrc_event_calendar import RrcEventCalendar, async_run_rrc_write
from .rrc_platforms import Tracker
from .writes import ContractRejected, ControlContext, ControlLocked

_CALENDAR_KEY: Final = "rrc_events_calendar"
_SWITCH_KEY: Final = "rrc_boost"
_LIMIT_PATHS: Final[Mapping[Setting, str]] = MappingProxyType(
    {
        "temperature": rrc_boost.BOOST_TEMPERATURE_PATH,
        "duration": rrc_boost.BOOST_DURATION_PATH,
    }
)


@dataclass(frozen=True, kw_only=True)
class BoostNumberDescription(NumberEntityDescription):
    """One setting of the boost structure."""

    setting: Setting


_NUMBERS: Final = (
    BoostNumberDescription(
        key="rrc_boost_temperature",
        translation_key="rrc_boost_temperature",
        setting="temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.CONFIG,
        mode=NumberMode.BOX,
        icon="mdi:thermometer-chevron-up",
    ),
    BoostNumberDescription(
        key="rrc_boost_duration",
        translation_key="rrc_boost_duration",
        setting="duration",
        native_unit_of_measurement=UnitOfTime.HOURS,
        entity_category=EntityCategory.CONFIG,
        mode=NumberMode.BOX,
        icon="mdi:timer-outline",
    ),
)


def _boost_writable(resources: Mapping[str, Resource]) -> bool:
    """S1/P41: a writable structure this integration can read."""
    resource = resources.get(BOOST_PATH)
    return (
        resource is not None
        and resource.metadata.writable
        and rrc_boost.parse(resource) is not None
    )


def build_calendar(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[EntityDescription, ...]:
    """The calendar once a list of the expected type is known."""
    del context
    if any(
        (resource := resources.get(path)) is not None
        and resource.metadata.resource_type == rrc_events.LIST_TYPE
        for path in rrc_events.LIST_PATHS
    ):
        return (EntityDescription(key=_CALENDAR_KEY),)
    return ()


def build_switch(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[EntityDescription, ...]:
    del context
    if _boost_writable(resources):
        return (EntityDescription(key=_SWITCH_KEY, translation_key="rrc_boost"),)
    return ()


def build_numbers(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[BoostNumberDescription, ...]:
    """S3: a number only with limits from its leaf's metadata."""
    del context
    if not _boost_writable(resources):
        return ()
    return tuple(
        description
        for description in _NUMBERS
        if rrc_boost.limits(
            resources.get(_LIMIT_PATHS[description.setting]), description.setting
        )
        is not None
    )


class _BoostEntity(CoordinatorEntity[BoschBuderusDataUpdateCoordinator]):
    """Reads and writes the boost structure as a whole."""

    _attr_has_entity_name = True
    _role: str

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, name: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:heatingCircuits:hc1:boostShortcut:"
            f"{self._role}"
        )
        self._attr_name = control_name(coordinator, BOOST_PATH, name)

    def _fresh(self, path: str) -> Resource | None:
        snapshot = (self.coordinator.data or {}).get(path)
        if (
            snapshot is None
            or not snapshot.available
            or snapshot.freshness is not Freshness.FRESH
        ):
            return None
        return snapshot.resource

    @property
    def _boost(self) -> Boost | None:
        return rrc_boost.parse(self._fresh(BOOST_PATH))

    @property
    def available(self) -> bool:
        resource = self._fresh(BOOST_PATH)
        return (
            super().available
            and resource is not None
            and resource.metadata.writable
            and rrc_boost.parse(resource) is not None
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, BOOST_PATH)

    async def _async_change(self, change: Callable[[Boost], Boost]) -> None:
        """Fresh preflight (S5), one PUT (S6), read-back (S7)."""
        coordinator = self.coordinator
        gateway_id = coordinator.gateway.gateway_id
        shown = self._boost
        targets: list[Boost] = []

        async def operation() -> tuple[Resource, ...]:
            current = await coordinator.client.get_resource(gateway_id, BOOST_PATH)
            issue = rrc_boost.write_issue(current)
            if issue == "locked":
                raise ControlLocked("The controller currently locks the boost")
            if issue is not None:
                raise (
                    ContractRejected(issue)
                    if issue == rrc_boost.UNSUPPORTED_SHAPE
                    else WriteValidationError(f"Boost not writable: {issue}")
                )
            boost = rrc_boost.parse(current)
            assert boost is not None  # checked by write_issue
            target = change(boost)
            if boost.shows(target):
                # Already so; nothing is sent.
                return (current,)
            if shown is None or not boost.shows(shown):
                raise WriteValidationError("The boost changed; read it again")
            targets.append(target)

            async def preflight() -> Boost:
                return target

            async def send(prepared: Boost) -> None:
                await coordinator.client.put_resource_json(
                    gateway_id, BOOST_PATH, prepared.body()
                )

            async def confirm(prepared: Boost) -> tuple[Resource, ...] | None:
                read = await coordinator.client.get_resource(gateway_id, BOOST_PATH)
                parsed = rrc_boost.parse(read)
                return (
                    (read,) if parsed is not None and parsed.shows(prepared) else None
                )

            return await coordinator.rrc_struct_writes.async_write(
                preflight=preflight,
                send=send,
                confirm=confirm,
                on_sent=lambda: coordinator.note_rrc_write_sent(BOOST_PATH),
            )

        def late(resource: Resource) -> bool:
            parsed = rrc_boost.parse(resource)
            return bool(targets) and parsed is not None and parsed.shows(targets[0])

        await async_run_rrc_write(coordinator, BOOST_PATH, operation, late_check=late)


class RrcBoostSwitch(_BoostEntity, SwitchEntity):
    """Boost on or off; the zones stay as set on the controller."""

    _role = "switch"
    _attr_translation_key = "rrc_boost"
    _attr_icon = "mdi:rocket-launch-outline"
    _unrecorded_attributes = frozenset({"zones"})

    def __init__(
        self, coordinator: BoschBuderusDataUpdateCoordinator, description: Any
    ) -> None:
        del description
        super().__init__(
            coordinator, localize(coordinator.hass.config.language, "Boost", "Boost")
        )

    @property
    def is_on(self) -> bool | None:
        boost = self._boost
        return boost.on if boost is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        boost = self._boost
        return {"zones": list(boost.zones)} if boost is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        del kwargs
        await self._async_change(lambda boost: rrc_boost.switched(boost, True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        del kwargs
        await self._async_change(lambda boost: rrc_boost.switched(boost, False))


class RrcBoostNumber(_BoostEntity, NumberEntity):
    """The temperature or the duration of the boost; the mode stays."""

    entity_description: BoostNumberDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: BoostNumberDescription,
    ) -> None:
        self.entity_description = description
        self._role = description.setting
        language = coordinator.hass.config.language
        name = (
            localize(language, "Boost-Temperatur", "Boost temperature")
            if description.setting == "temperature"
            else localize(language, "Boost-Dauer", "Boost duration")
        )
        super().__init__(coordinator, name)

    @property
    def _limits(self) -> rrc_boost.Limits | None:
        setting = self.entity_description.setting
        return rrc_boost.limits(self._fresh(_LIMIT_PATHS[setting]), setting)

    @property
    def available(self) -> bool:
        return super().available and self._limits is not None

    @property
    def native_min_value(self) -> float:
        limits = self._limits
        return limits.minimum if limits is not None else super().native_min_value

    @property
    def native_max_value(self) -> float:
        limits = self._limits
        return limits.maximum if limits is not None else super().native_max_value

    @property
    def native_step(self) -> float | None:
        limits = self._limits
        return limits.step if limits is not None else super().native_step

    @property
    def native_value(self) -> float | None:
        boost = self._boost
        if boost is None:
            return None
        if self.entity_description.setting == "temperature":
            return boost.temperature
        return float(boost.duration)

    async def async_set_native_value(self, value: float) -> None:
        limits = self._limits
        if limits is None:
            # S3: no limits from the metadata, no value is sent.
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="write_validation_failed"
            )
        setting = self.entity_description.setting
        await self._async_change(
            lambda boost: rrc_boost.adjusted(boost, setting, value, limits)
        )


def _track_calendar(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(
        entry,
        coordinator,
        async_add_entities,
        build_calendar,
        lambda owner, _description: RrcEventCalendar(owner),
    )


def _track_switch(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(
        entry, coordinator, async_add_entities, build_switch, RrcBoostSwitch
    )


def _track_numbers(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    track_rrc_entities(
        entry, coordinator, async_add_entities, build_numbers, RrcBoostNumber
    )


TRACKERS: Mapping[Platform, Tracker] = MappingProxyType(
    {
        Platform.CALENDAR: _track_calendar,
        Platform.SWITCH: _track_switch,
        Platform.NUMBER: _track_numbers,
    }
)
