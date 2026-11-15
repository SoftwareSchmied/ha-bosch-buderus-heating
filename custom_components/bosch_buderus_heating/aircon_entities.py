"""Additional air-conditioner controls (PointT ``airConditioning``).

Extra functions as switches, the spot air direction and the active schedule
as selects, the switch-on/off timers as numbers, ending a temporary change of
the schedule as a button and the solar operation as a binary sensor. All of
them are disabled by default; each exists only when the unit reports its
path as writable (and names the function, where it lists them).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .aircon import AirconCoordinator
from .aircon_entity import (
    AirconEntity,
    invalid_command,
    options,
    track_aircon_entities,
)
from .aircon_policies import (
    ACTIVE_PROGRAM,
    ACTIVE_PROGRAM_POLICY,
    ADVANCED_FUNCTION_POLICIES,
    AIR_PURIFICATION_MODE,
    ECO_MODE,
    FULL_POWER_MODE,
    PV_ENABLE,
    QUICK_AIR_FLOWS,
    QUICK_AIR_FLOWS_POLICY,
    SCHEDULE_ENABLED,
    SCHEDULE_ENABLED_POLICY,
    SETBACK_MODE,
    SLEEP_MODE,
    TEMPORARY_SETTING,
    TEMPORARY_SETTING_POLICY,
    TIMER_OFF,
    TIMER_ON,
    TIMER_POLICY,
    TIMER_STEP_MIN,
)
from .platform_scope import PlatformScope
from .unit_contracts import DURATION_UNITS
from .writes import EnumWritePolicy, assess_control

type Factory = tuple[str, Callable[[], Entity]]

QUICK_ORDER = (
    "off",
    "on",
    "topLeft",
    "topCenter",
    "topRight",
    "bottomLeft",
    "bottomCenter",
    "bottomRight",
)


def _key(path: str) -> str:
    return path.strip("/").replace("/", ":")


def _switch_translation(coordinator: AirconCoordinator, path: str) -> str:
    if path == SETBACK_MODE:
        # Sharp units heat to 10 °C, Midea units to 8 °C.
        return (
            "aircon_setback_10" if coordinator.system == "sharp" else "aircon_setback_8"
        )
    return {
        FULL_POWER_MODE: "aircon_full_power",
        SLEEP_MODE: "aircon_sleep",
        AIR_PURIFICATION_MODE: "aircon_air_purification",
        SCHEDULE_ENABLED: "aircon_schedule_enabled",
    }[path]


class AirconControl(AirconEntity):
    """A disabled-by-default setting of the unit."""

    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: AirconCoordinator, path: str) -> None:
        super().__init__(coordinator, _key(path))
        self._path = path

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.control_ready(self._path)


class AirconSwitch(AirconControl, SwitchEntity):
    """An on/off function of the unit."""

    def __init__(
        self, coordinator: AirconCoordinator, path: str, policy: EnumWritePolicy
    ) -> None:
        super().__init__(coordinator, path)
        self._policy = policy
        self._attr_translation_key = _switch_translation(coordinator, path)

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.value(self._path)
        return {"on": True, "off": False}.get(str(value)) if value else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(self._path, "on", self._policy)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(self._path, "off", self._policy)


class AirconSelect(AirconControl, SelectEntity):
    """A choice from the reported options."""

    def __init__(
        self,
        coordinator: AirconCoordinator,
        path: str,
        policy: EnumWritePolicy,
        translation_key: str,
        order: Iterable[str] | None,
    ) -> None:
        super().__init__(coordinator, path)
        self._policy = policy
        self._order = tuple(order) if order is not None else None
        self._attr_translation_key = translation_key

    def _offered(self) -> dict[str, str]:
        return options(self.coordinator, self._path, self._order)

    @property
    def options(self) -> list[str]:
        return list(self._offered())

    @property
    def current_option(self) -> str | None:
        value = self.coordinator.value(self._path)
        return next(
            (key for key, token in self._offered().items() if token == value), None
        )

    async def async_select_option(self, option: str) -> None:
        token = self._offered().get(option)
        if token is None:
            raise invalid_command()
        await self._write(self._path, token, self._policy)


def timer_valid(coordinator: AirconCoordinator, path: str) -> bool:
    """A usable timer: minimum above 0 and room for at least one step."""
    resource = coordinator.resource(path)
    if resource is None or not coordinator.control_ready(path):
        return False
    minimum, maximum = resource.metadata.minimum, resource.metadata.maximum
    return (
        DURATION_UNITS.get(resource.metadata.unit or "") == "min"
        and minimum is not None
        and maximum is not None
        and minimum > 0
        and maximum - minimum >= TIMER_STEP_MIN
    )


class AirconTimer(AirconControl, NumberEntity):
    """Minutes until the unit switches on or off; 0 stops the timer."""

    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 0.0
    _attr_native_step = TIMER_STEP_MIN
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: AirconCoordinator, path: str) -> None:
        super().__init__(coordinator, path)
        self._attr_translation_key = (
            "aircon_timer_on" if path == TIMER_ON else "aircon_timer_off"
        )

    @property
    def available(self) -> bool:
        return super().available and timer_valid(self.coordinator, self._path)

    @property
    def native_max_value(self) -> float:
        resource = self.coordinator.resource(self._path)
        maximum = resource.metadata.maximum if resource is not None else None
        return maximum if maximum is not None else TIMER_STEP_MIN

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.value(self._path)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    async def async_set_native_value(self, value: float) -> None:
        if not timer_valid(self.coordinator, self._path):
            raise invalid_command()
        await self._write(self._path, value, TIMER_POLICY)


class AirconEndTemporary(AirconControl, ButtonEntity):
    """End a manual change made while the schedule runs (sends ``off``)."""

    _attr_translation_key = "aircon_end_temporary"

    def __init__(self, coordinator: AirconCoordinator) -> None:
        super().__init__(coordinator, TEMPORARY_SETTING)

    async def async_press(self) -> None:
        await self._write(TEMPORARY_SETTING, "off", TEMPORARY_SETTING_POLICY)


class AirconSolarOperation(AirconEntity, BinarySensorEntity):
    """Whether the unit runs in solar operation (read only)."""

    _attr_translation_key = "aircon_solar_operation"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: AirconCoordinator) -> None:
        super().__init__(coordinator, _key(PV_ENABLE))

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.resource(PV_ENABLE) is not None

    @property
    def is_on(self) -> bool | None:
        return {"on": True, "off": False}.get(
            str(self.coordinator.value(PV_ENABLE))
        )


def _switches(coordinator: AirconCoordinator) -> Iterable[Factory]:
    policies = {
        **{
            path: policy
            for path, policy in ADVANCED_FUNCTION_POLICIES.items()
            # Eco is the preset of the climate entity.
            if path != ECO_MODE
        },
        SCHEDULE_ENABLED: SCHEDULE_ENABLED_POLICY,
    }
    for path, policy in policies.items():
        resource = coordinator.resource(path)
        if (
            resource is not None
            and coordinator.control_ready(path)
            and assess_control(resource).supports_switch
        ):
            yield _key(path), lambda path=path, policy=policy: AirconSwitch(
                coordinator, path, policy
            )


def _selects(coordinator: AirconCoordinator) -> Iterable[Factory]:
    for path, policy, key, order in (
        (QUICK_AIR_FLOWS, QUICK_AIR_FLOWS_POLICY, "aircon_quick_air_flow", QUICK_ORDER),
        (ACTIVE_PROGRAM, ACTIVE_PROGRAM_POLICY, "aircon_active_schedule", None),
    ):
        if len(options(coordinator, path, order)) >= 2:
            yield _key(path), lambda path=path, policy=policy, key=key, order=order: (
                AirconSelect(coordinator, path, policy, key, order)
            )


def _timers(coordinator: AirconCoordinator) -> Iterable[Factory]:
    for path in (TIMER_ON, TIMER_OFF):
        if timer_valid(coordinator, path):
            yield _key(path), lambda path=path: AirconTimer(coordinator, path)


def _buttons(coordinator: AirconCoordinator) -> Iterable[Factory]:
    if coordinator.control_ready(TEMPORARY_SETTING):
        yield _key(TEMPORARY_SETTING), lambda: AirconEndTemporary(coordinator)


def _binary_sensors(coordinator: AirconCoordinator) -> Iterable[Factory]:
    if coordinator.resource(PV_ENABLE) is not None:
        yield _key(PV_ENABLE), lambda: AirconSolarOperation(coordinator)


def _tracker(
    build: Callable[[AirconCoordinator], Iterable[Factory]],
) -> Callable[[PlatformScope, AddConfigEntryEntitiesCallback], None]:
    def track(
        scope: PlatformScope, async_add_entities: AddConfigEntryEntitiesCallback
    ) -> None:
        track_aircon_entities(scope, async_add_entities, build)

    return track


track_aircon_switches = _tracker(_switches)
track_aircon_selects = _tracker(_selects)
track_aircon_numbers = _tracker(_timers)
track_aircon_buttons = _tracker(_buttons)
track_aircon_binary_sensors = _tracker(_binary_sensors)
