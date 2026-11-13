"""RRC 2.0 entities: away mode, events calendar, boost, fireplace.

Owned by work package W2. ``rrc_platforms.track`` calls the tracker of each
platform listed in ``TRACKERS`` once per coordinator; see
``docs/dev/rrc-packages.md``.

Away mode, its temperature and the fireplace function are generic switches
and numbers of the contract (``rrc_contract_absence``). This module adds the
events calendar and the boost entities: a switch and two numbers over the
one structure ``boostShortcut`` (``rrc_boost``). The numbers send nothing
themselves (AWAY-06): while the boost is off they keep a preset that the
switch sends with the next start; while it runs they refuse a change. All
of it is expected, untested on hardware.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from types import MappingProxyType
from typing import Any, Final
from weakref import WeakKeyDictionary

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory, Platform, UnitOfTemperature, UnitOfTime
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import rrc, rrc_boost, rrc_events
from .const import DOMAIN
from .control import track_rrc_entities
from .control_presentation import control_name
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource
from .i18n import localize
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource, WriteValidationError
from .rrc_boost import BOOST_PATH, Boost, Preset, Setting
from .rrc_event_calendar import RrcEventCalendar, async_run_rrc_write
from .rrc_platforms import Tracker
from .write_policies import context_of
from .writes import ContractRejected, ControlContext, ControlLocked

_CALENDAR_KEY: Final = "rrc_events_calendar"
_SWITCH_KEY: Final = "rrc_boost"
_LIMIT_PATHS: Final[Mapping[Setting, str]] = MappingProxyType(
    {
        "temperature": rrc_boost.BOOST_TEMPERATURE_PATH,
        "duration": rrc_boost.BOOST_DURATION_PATH,
    }
)
# AWAY-06: the preset of the next start per coordinator, set by the numbers
# and sent by the switch.
_PRESETS: Final[
    WeakKeyDictionary[BoschBuderusDataUpdateCoordinator, dict[Setting, float]]
] = WeakKeyDictionary()


def _presets(coordinator: BoschBuderusDataUpdateCoordinator) -> dict[Setting, float]:
    return _PRESETS.setdefault(coordinator, {})


def _refusal(issue: str) -> Exception:
    """The error of a boost structure that may not be written now."""
    if issue == "locked":
        return ControlLocked("The controller currently locks the boost")
    if issue == rrc_boost.UNSUPPORTED_SHAPE:
        return ContractRejected(issue)
    return WriteValidationError(f"Boost not writable: {issue}")


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
    if not context.rrc_plant:
        return ()  # S11, also without the tracker's own check
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
    if not context.rrc_plant:
        return ()  # S11, also without the tracker's own check
    if _boost_writable(resources):
        return (EntityDescription(key=_SWITCH_KEY, translation_key="rrc_boost"),)
    return ()


def build_numbers(
    resources: Mapping[str, Resource], context: ControlContext
) -> tuple[BoostNumberDescription, ...]:
    """S3: a number only with limits from its leaf's metadata."""
    if not context.rrc_plant:
        return ()  # S11, also without the tracker's own check
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

    def _limits_of(self, setting: Setting) -> rrc_boost.Limits | None:
        return rrc_boost.limits(self._fresh(_LIMIT_PATHS[setting]), setting)


class RrcBoostSwitch(_BoostEntity, SwitchEntity):
    """Boost on or off; a start sends the preset of the numbers and the set
    zones that still exist and that the boost may heat."""

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
        used = dict(_presets(self.coordinator))

        def start(boost: Boost) -> Boost:
            if boost.on:
                # Already running: nothing is sent, the preset waits.
                return boost
            return rrc_boost.started(boost, self._zone_ids(), self._preset(used))

        if await self._async_change(start):
            # The controller holds the preset now.
            presets = _presets(self.coordinator)
            for setting, value in used.items():
                if presets.get(setting) == value:
                    del presets[setting]
            self.coordinator.async_update_listeners()

    async def async_turn_off(self, **kwargs: Any) -> None:
        del kwargs
        await self._async_change(rrc_boost.stopped)

    def _preset(self, values: Mapping[Setting, float]) -> Preset:
        """S3 once more at the start: a preset outside the current limits is
        not sent."""
        for setting, value in values.items():
            limits = self._limits_of(setting)
            if limits is None:
                raise WriteValidationError("The boost limits are unknown")
            limits.check(value)
        duration = values.get("duration")
        return Preset(
            values.get("temperature"), int(duration) if duration is not None else None
        )

    def _zone_ids(self) -> tuple[int, ...] | None:
        """The zones of the last valid zone list (AWAY-08: a deleted zone is
        not sent)."""
        snapshot = (self.coordinator.data or {}).get(rrc.ZONE_LIST)
        resource = (
            snapshot.resource
            if snapshot is not None
            else self.coordinator.resources.get(rrc.ZONE_LIST)
        )
        return rrc.listed_ids(resource) if resource is not None else None

    async def _async_change(self, change: Callable[[Boost], Boost]) -> bool:
        """Fresh preflight (S5), one PUT (S6), read-back (S7); whether a
        change was sent and confirmed.

        S5 (AWAY-09): only the mode and the zones must still be as shown;
        the other fields are sent as the fresh reading has them.
        """
        coordinator = self.coordinator
        gateway_id = coordinator.gateway.gateway_id
        shown = self._boost
        targets: list[Boost] = []

        async def operation() -> tuple[Resource, ...]:
            current = await coordinator.client.get_resource(gateway_id, BOOST_PATH)
            issue = rrc_boost.write_issue(current)
            if issue is not None:
                raise _refusal(issue)
            boost = rrc_boost.parse(current)
            assert boost is not None  # checked by write_issue
            target = change(boost)
            if boost.shows(target):
                # Already so; nothing is sent.
                return (current,)
            if shown is None or shown.mode != boost.mode or shown.zones != boost.zones:
                raise ContractRejected("rrc_boost_changed")
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
        return bool(targets)


@dataclass(frozen=True, slots=True)
class _PresetData(ExtraStoredData):
    """The preset of one number across restarts."""

    value: float | None

    def as_dict(self) -> dict[str, Any]:
        return {"next_start": self.value}


class RrcBoostNumber(_BoostEntity, RestoreEntity, NumberEntity):
    """The temperature or the duration of the next boost start (AWAY-06).

    Nothing is sent from here: while the boost is off a new value is kept
    and shown until the switch sends it with the next start; while it runs
    a change is refused. The kept value survives a restart.
    """

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
        return self._limits_of(self.entity_description.setting)

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

    def _controller_value(self, boost: Boost) -> float:
        if self.entity_description.setting == "temperature":
            return boost.temperature
        return float(boost.duration)

    @property
    def native_value(self) -> float | None:
        """The kept value while the boost is off, else the controller's."""
        boost = self._boost
        if boost is None:
            return None
        preset = _presets(self.coordinator).get(self.entity_description.setting)
        if preset is not None and not boost.on:
            return preset
        return self._controller_value(boost)

    async def async_set_native_value(self, value: float) -> None:
        """Keep the value for the next start; nothing is sent (AWAY-06)."""
        setting = self.entity_description.setting
        limits = self._limits
        resource = self._fresh(BOOST_PATH)
        boost = rrc_boost.parse(resource)
        try:
            if limits is None or resource is None or boost is None:
                # S3: no limits from the metadata, no value is kept.
                raise WriteValidationError("The boost or its limits are unknown")
            if (issue := rrc_boost.write_issue(resource)) is not None:
                raise _refusal(issue)
            if boost.on:
                raise ContractRejected(rrc_boost.RUNNING)
            checked = rrc_boost.preset_value(setting, value, limits)
        except ContractRejected as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key=err.translation_key
            ) from err
        except ControlLocked as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="rrc_boost_locked"
            ) from err
        except WriteValidationError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="write_validation_failed"
            ) from err
        presets = _presets(self.coordinator)
        if math.isclose(checked, self._controller_value(boost), abs_tol=1e-6):
            presets.pop(setting, None)
        else:
            presets[setting] = checked
        self.async_write_ha_state()

    @property
    def extra_restore_state_data(self) -> _PresetData:
        return _PresetData(
            _presets(self.coordinator).get(self.entity_description.setting)
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        stored = await self.async_get_last_extra_data()
        value = stored.as_dict().get("next_start") if stored is not None else None
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
            _presets(self.coordinator).setdefault(
                self.entity_description.setting, float(value)
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
    _watch_boost_limits(entry, coordinator)


def _watch_boost_limits(
    entry: DiscoveryLifecycle, coordinator: BoschBuderusDataUpdateCoordinator
) -> None:
    """Note after every update why a boost setting has no limits, from
    the fresh readings only; the diagnostics show the last reason."""
    watch = rrc_boost.limit_watch(coordinator, create=True)
    assert watch is not None

    @callback
    def observe() -> None:
        if not context_of(coordinator).rrc_plant:
            return
        data = coordinator.data or {}
        if BOOST_PATH not in data:
            return
        fresh = {
            path: snapshot.resource
            for path in (
                rrc_boost.BOOST_TEMPERATURE_PATH,
                rrc_boost.BOOST_DURATION_PATH,
            )
            if (snapshot := data.get(path)) is not None
            and snapshot.available
            and snapshot.freshness is Freshness.FRESH
        }
        watch.observe(rrc_boost.setting_leaves(fresh), monotonic())

    entry.async_on_unload(coordinator.async_add_listener(observe))
    observe()


TRACKERS: Mapping[Platform, Tracker] = MappingProxyType(
    {
        Platform.CALENDAR: _track_calendar,
        Platform.SWITCH: _track_switch,
        Platform.NUMBER: _track_numbers,
    }
)
