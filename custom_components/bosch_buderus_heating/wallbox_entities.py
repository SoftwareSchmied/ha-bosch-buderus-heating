"""Wallbox controls and the additional wallbox readings.

Controls exist only when the wallbox reports what they need: the firmware
from which the vendor app offers the function, a single value that answered
and (for settings) reported itself writeable, and a usable hardware current
limit for the current controls. Once created, an entity stays; while a rule
of the vendor app does not allow it (state, role in a load-management
group, energy manager, missing meter) it is unavailable. Every control is
disabled by default. Commands have no single value to report writeability;
they follow the state rules and stay disabled until a user enables them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Literal

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import (
    EntityCategory,
    Platform,
    UnitOfElectricCurrent,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .control import cloud_down_error
from .platform_scope import PlatformScope
from .pointt.exceptions import (
    AuthenticationError,
    PointTError,
    RateLimited,
    WriteNotConfirmed,
    WriteValidationError,
)
from .wallbox import ENERGY_MANAGER_STATES, LIMITERS, WallboxCoordinator
from .wallbox_control import (
    FORCE_PATH,
    METER_MAXIMUM_PATH,
    METER_PATH,
    MINIMUM_CURRENT,
    OPTION_AUTHENTICATE,
    PHASE_RULE,
    PHASES_PATH,
    SETTINGS,
    STATIC_LEAVES,
    STRATEGY_PATH,
    WallboxCloudDown,
    WallboxRejected,
    WallboxView,
    async_pause,
    async_set_boost,
    async_set_current_limit,
    async_set_phase_limit,
    async_set_setting,
    async_set_strategy,
    async_start,
    boost_issue,
    brand_excluded,
    hardware_limit,
    limit_issue,
    number_bounds,
    pause_issue,
    setting_issue,
    start_issue,
    strategy_issue,
)
from .wallbox_leaves import (
    FW_BOOST,
    FW_OVERLOAD,
    FW_SOLAR,
    firmware_reaches,
)
from .wallbox_sensor import wallbox_device_info
from .writes import WriteBusy

OVERLOAD_PATHS = (
    "conf/overloadProtection/currentLimiter",
    "conf/overloadProtection/currentLimit",
    "conf/overloadProtection/powerLimiter",
    "conf/overloadProtection/powerLimit",
)


def cached_view(coordinator: WallboxCoordinator) -> WallboxView:
    """The rules' view from the last poll: current values only, the flags of
    single values however old."""

    def section(name: Literal["telemetry", "conf", "info"]) -> dict[str, Any]:
        snapshot = coordinator.data.get(name)
        if snapshot is None or not coordinator.section_fresh(name):
            return {}
        return dict(snapshot.values)

    leaves = {
        path: leaf
        if path in STATIC_LEAVES or coordinator.leaf_fresh(path)
        else replace(leaf, value=None)
        for path, leaf in coordinator.leaves.items()
    }
    return WallboxView(
        section("telemetry"),
        section("conf"),
        section("info"),
        leaves,
        coordinator.firmware(),
    )


async def async_wallbox_call(
    coordinator: WallboxCoordinator, action: Awaitable[None]
) -> None:
    """Run a wallbox action and expose only translated, value-free errors."""
    try:
        await action
    except WallboxRejected as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key=err.translation_key
        ) from err
    except WallboxCloudDown as err:
        down = cloud_down_error(coordinator)
        raise (
            down
            if down is not None
            else HomeAssistantError(
                translation_domain=DOMAIN, translation_key="write_failed"
            )
        ) from err
    except WriteValidationError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="write_validation_failed"
        ) from err
    except WriteNotConfirmed as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_not_confirmed"
        ) from err
    except WriteBusy as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_busy"
        ) from err
    except AuthenticationError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_authentication_failed"
        ) from err
    except RateLimited as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_rate_limited"
        ) from err
    except PointTError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_failed"
        ) from err


def _writeable(view: WallboxView, path: str) -> bool:
    leaf = view.leaves.get(path)
    return leaf is not None and leaf.writeable


def _meter_seen(view: WallboxView) -> bool:
    return view.leaf_value(METER_PATH) in ("connected", "error")


def _standalone(view: WallboxView) -> bool:
    return view.conf.get("role") in ("standalone", None) and bool(view.conf)


def _setting_creatable(view: WallboxView, key: str) -> bool:
    rule = SETTINGS[key]
    if not (
        _writeable(view, rule.path)
        and firmware_reaches(view.firmware, rule.firmware)
        and (rule.meter == "none" or _meter_seen(view))
    ):
        return False
    if rule.brand_check and not _overload_creatable(view):
        return False
    return rule.meter == "none" or _standalone(view)


def _overload_creatable(view: WallboxView) -> bool:
    """Only with all four overload values writeable and a brand the app
    offers the function for."""
    return (
        firmware_reaches(view.firmware, FW_OVERLOAD)
        and all(_writeable(view, path) for path in OVERLOAD_PATHS)
        and brand_excluded(view) is False
        and _meter_seen(view)
        and _standalone(view)
    )


@dataclass(frozen=True, kw_only=True)
class WallboxEntitySpec:
    """One wallbox entity: platform, identity, when it exists."""

    key: str
    platform: Platform
    creatable: Callable[[WallboxView], bool]
    enabled: bool = False
    category: EntityCategory | None = None
    # Single values an enabled entity shows (polled in the conf interval).
    shows: tuple[str, ...] = ()


def _seen(
    section: Literal["telemetry", "conf", "info"], key: str
) -> Callable[[WallboxView], bool]:
    def check(view: WallboxView) -> bool:
        return getattr(view, section).get(key) is not None

    return check


def _leaf_known(path: str) -> Callable[[WallboxView], bool]:
    return lambda view: view.leaves.get(path) is not None


SPECS: tuple[WallboxEntitySpec, ...] = (
    # Commands.
    WallboxEntitySpec(
        key="start_charging",
        platform=Platform.BUTTON,
        creatable=lambda view: view.firmware is not None,
    ),
    WallboxEntitySpec(
        key="pause_charging",
        platform=Platform.BUTTON,
        creatable=lambda view: view.firmware is not None,
    ),
    WallboxEntitySpec(
        key="charging_current_limit",
        platform=Platform.NUMBER,
        creatable=lambda view: hardware_limit(view) is not None,
    ),
    WallboxEntitySpec(
        key="boost",
        platform=Platform.SWITCH,
        creatable=lambda view: (
            firmware_reaches(view.firmware, FW_BOOST)
            and hardware_limit(view) is not None
            and view.leaves.get(FORCE_PATH) is not None
        ),
        shows=(FORCE_PATH,),
    ),
    # Settings.
    WallboxEntitySpec(
        key="charging_strategy",
        platform=Platform.SELECT,
        creatable=lambda view: (
            firmware_reaches(view.firmware, FW_SOLAR)
            and _writeable(view, STRATEGY_PATH)
            and _meter_seen(view)
            and _standalone(view)
        ),
        shows=(STRATEGY_PATH, METER_PATH),
    ),
    WallboxEntitySpec(
        key="solar_supplement",
        platform=Platform.SWITCH,
        creatable=lambda view: _setting_creatable(view, "solar_supplement"),
        category=EntityCategory.CONFIG,
        shows=(SETTINGS["solar_supplement"].path, STRATEGY_PATH, METER_PATH),
    ),
    WallboxEntitySpec(
        key="supplement_limit",
        platform=Platform.NUMBER,
        creatable=lambda view: _setting_creatable(view, "supplement_limit"),
        category=EntityCategory.CONFIG,
        shows=(SETTINGS["supplement_limit"].path, STRATEGY_PATH, METER_PATH),
    ),
    WallboxEntitySpec(
        key="supplement_threshold",
        platform=Platform.NUMBER,
        creatable=lambda view: _setting_creatable(view, "supplement_threshold"),
        category=EntityCategory.CONFIG,
        shows=(
            SETTINGS["supplement_threshold"].path,
            STRATEGY_PATH,
            METER_PATH,
        ),
    ),
    WallboxEntitySpec(
        key="home_battery_priority",
        platform=Platform.SWITCH,
        creatable=lambda view: _setting_creatable(view, "home_battery_priority"),
        category=EntityCategory.CONFIG,
        shows=(SETTINGS["home_battery_priority"].path, METER_PATH),
    ),
    WallboxEntitySpec(
        key="filter_time",
        platform=Platform.NUMBER,
        creatable=lambda view: _setting_creatable(view, "filter_time"),
        category=EntityCategory.CONFIG,
        shows=(SETTINGS["filter_time"].path, METER_PATH),
    ),
    WallboxEntitySpec(
        key="current_limiter",
        platform=Platform.SWITCH,
        creatable=lambda view: _setting_creatable(view, "current_limiter"),
        category=EntityCategory.CONFIG,
        shows=(SETTINGS["current_limiter"].path, METER_PATH),
    ),
    WallboxEntitySpec(
        key="power_limiter",
        platform=Platform.SWITCH,
        creatable=lambda view: _setting_creatable(view, "power_limiter"),
        category=EntityCategory.CONFIG,
        shows=(SETTINGS["power_limiter"].path, METER_PATH),
    ),
    WallboxEntitySpec(
        key="power_limit",
        platform=Platform.NUMBER,
        creatable=lambda view: _setting_creatable(view, "power_limit"),
        category=EntityCategory.CONFIG,
        shows=(SETTINGS["power_limit"].path, METER_PATH),
    ),
    *(
        WallboxEntitySpec(
            key=f"current_limit_l{phase}",
            platform=Platform.NUMBER,
            creatable=lambda view: (
                _overload_creatable(view)
                and isinstance(view.leaf_value(PHASES_PATH), tuple)
            ),
            category=EntityCategory.CONFIG,
            shows=(PHASES_PATH, METER_PATH),
        )
        for phase in (1, 2, 3)
    ),
    WallboxEntitySpec(
        key="locked",
        platform=Platform.SWITCH,
        creatable=lambda view: _setting_creatable(view, "locked"),
        category=EntityCategory.CONFIG,
    ),
    WallboxEntitySpec(
        key="label",
        platform=Platform.TEXT,
        creatable=lambda view: _setting_creatable(view, "label"),
        category=EntityCategory.CONFIG,
    ),
    WallboxEntitySpec(
        key="electricity_price",
        platform=Platform.NUMBER,
        creatable=lambda view: (
            _setting_creatable(view, "electricity_price") and _standalone(view)
        ),
        category=EntityCategory.CONFIG,
        shows=(SETTINGS["electricity_price"].path,),
    ),
    WallboxEntitySpec(
        key="feed_in_price",
        platform=Platform.NUMBER,
        creatable=lambda view: _setting_creatable(view, "feed_in_price"),
        category=EntityCategory.CONFIG,
        shows=(SETTINGS["feed_in_price"].path, METER_PATH),
    ),
    # Additional readings.
    WallboxEntitySpec(
        key="external_meter",
        platform=Platform.SENSOR,
        creatable=_leaf_known(METER_PATH),
        enabled=True,
        category=EntityCategory.DIAGNOSTIC,
        shows=(METER_PATH,),
    ),
    WallboxEntitySpec(
        key="external_meter_maximum_current",
        platform=Platform.SENSOR,
        creatable=_leaf_known(METER_MAXIMUM_PATH),
        category=EntityCategory.DIAGNOSTIC,
        shows=(METER_MAXIMUM_PATH,),
    ),
    WallboxEntitySpec(
        key="currency",
        platform=Platform.SENSOR,
        creatable=_leaf_known("conf/currency"),
        category=EntityCategory.DIAGNOSTIC,
        shows=("conf/currency",),
    ),
    WallboxEntitySpec(
        key="limiter",
        platform=Platform.SENSOR,
        creatable=_seen("telemetry", "limiter_token"),
        enabled=True,
        category=EntityCategory.DIAGNOSTIC,
    ),
    WallboxEntitySpec(
        key="energy_manager",
        platform=Platform.SENSOR,
        creatable=_seen("telemetry", "energy_manager"),
        enabled=True,
        category=EntityCategory.DIAGNOSTIC,
    ),
    WallboxEntitySpec(
        key="boost_active",
        platform=Platform.BINARY_SENSOR,
        creatable=_leaf_known(FORCE_PATH),
        category=EntityCategory.DIAGNOSTIC,
        shows=(FORCE_PATH,),
    ),
    WallboxEntitySpec(
        key="grid_friendly_input",
        platform=Platform.BINARY_SENSOR,
        creatable=_leaf_known("telemetry/extInputState"),
        category=EntityCategory.DIAGNOSTIC,
        shows=("telemetry/extInputState",),
    ),
    WallboxEntitySpec(
        key="grid_friendly_configured",
        platform=Platform.BINARY_SENSOR,
        creatable=_seen("info", "grid_friendly_configured"),
        enabled=True,
        category=EntityCategory.DIAGNOSTIC,
    ),
    WallboxEntitySpec(
        key="grid_friendly_active",
        platform=Platform.BINARY_SENSOR,
        creatable=_seen("info", "grid_friendly_active"),
        enabled=True,
        category=EntityCategory.DIAGNOSTIC,
    ),
    WallboxEntitySpec(
        key="relays_available",
        platform=Platform.BINARY_SENSOR,
        creatable=_seen("info", "relays_available"),
        enabled=True,
        category=EntityCategory.DIAGNOSTIC,
    ),
    WallboxEntitySpec(
        key="authentication_required",
        platform=Platform.BINARY_SENSOR,
        creatable=_seen("conf", "authentication"),
        enabled=True,
        category=EntityCategory.DIAGNOSTIC,
    ),
)
SPEC_BY_KEY = {spec.key: spec for spec in SPECS}
# Read-only additions show values; every other entity changes the wallbox.
READ_ONLY_PLATFORMS = frozenset({Platform.SENSOR, Platform.BINARY_SENSOR})


class WallboxEntity(CoordinatorEntity[WallboxCoordinator], Entity):
    """Identity, device and availability shared by the wallbox entities."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: WallboxCoordinator, spec: WallboxEntitySpec
    ) -> None:
        super().__init__(coordinator)
        self.spec = spec
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:wallbox:cp0:{spec.key}"
        )
        self._attr_translation_key = f"wallbox_{spec.key}"
        self._attr_entity_registry_enabled_default = spec.enabled
        self._attr_entity_category = spec.category

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        for path in self.spec.shows:
            self.async_on_remove(self.coordinator.demand_leaf(path))

    @property
    def device_info(self) -> DeviceInfo:
        return wallbox_device_info(self.coordinator)

    @property
    def view(self) -> WallboxView:
        return cached_view(self.coordinator)

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.section_fresh("telemetry")
            and self._usable(self.view)
        )

    def _usable(self, view: WallboxView) -> bool:
        return True

    async def _run(self, action: Awaitable[None]) -> None:
        await async_wallbox_call(self.coordinator, action)


def _on_off(value: object) -> bool | None:
    return {"on": True, "off": False}.get(value) if isinstance(value, str) else None


def _authentication_allowed(coordinator: WallboxCoordinator, view: WallboxView) -> bool:
    """A start with the authentication on: allowed by the option, with a
    known firmware (``authenticate`` first from 5.1.0, ``start`` alone
    below it, as the app)."""
    entry = coordinator.config_entry
    return (
        entry is not None
        and entry.options.get(OPTION_AUTHENTICATE) is True
        and view.firmware is not None
    )


class WallboxButton(WallboxEntity, ButtonEntity):
    """Start or pause charging."""

    def _usable(self, view: WallboxView) -> bool:
        if self.spec.key == "pause_charging":
            return pause_issue(view) is None
        if start_issue(view) is not None:
            return False
        return view.conf.get("authentication") != "on" or _authentication_allowed(
            self.coordinator, view
        )

    async def async_press(self) -> None:
        if self.spec.key == "pause_charging":
            await self._run(async_pause(self.coordinator))
        else:
            await self._run(async_start(self.coordinator))


class WallboxNumber(WallboxEntity, NumberEntity):
    """The charging current, a solar, overload or price setting."""

    def __init__(
        self, coordinator: WallboxCoordinator, spec: WallboxEntitySpec
    ) -> None:
        super().__init__(coordinator, spec)
        key = spec.key
        self._phase = int(key[-1]) - 1 if key.startswith("current_limit_l") else None
        self._attr_mode = NumberMode.BOX
        if key == "charging_current_limit" or self._phase is not None:
            self._attr_device_class = NumberDeviceClass.CURRENT
            self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
            self._attr_mode = NumberMode.SLIDER
        elif key in ("supplement_limit", "supplement_threshold", "power_limit"):
            self._attr_device_class = NumberDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
        elif key == "filter_time":
            self._attr_device_class = NumberDeviceClass.DURATION
            self._attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def _bounds(self) -> tuple[float, float, float]:
        view = self.view
        if self.spec.key == "charging_current_limit":
            maximum = hardware_limit(view)
            return MINIMUM_CURRENT, float(maximum or MINIMUM_CURRENT), 1
        rule = PHASE_RULE if self._phase is not None else SETTINGS[self.spec.key]
        bounds = number_bounds(view, rule, view.leaves.get(rule.path))
        if bounds is not None:
            return bounds
        minimum = rule.minimum or 0.0
        return minimum, rule.maximum or minimum, rule.step or 1.0

    @property
    def native_min_value(self) -> float:
        return self._bounds()[0]

    @property
    def native_max_value(self) -> float:
        return self._bounds()[1]

    @property
    def native_step(self) -> float:
        return self._bounds()[2]

    @property
    def native_value(self) -> float | None:
        view = self.view
        value: object
        if self.spec.key == "charging_current_limit":
            value = view.telemetry.get("current_limit")
        elif self._phase is not None:
            phases = view.leaf_value(PHASES_PATH)
            value = phases[self._phase] if isinstance(phases, tuple) else None
        else:
            value = view.leaf_value(SETTINGS[self.spec.key].path)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    def _usable(self, view: WallboxView) -> bool:
        if self.spec.key == "charging_current_limit":
            return limit_issue(view) is None
        if self._phase is not None:
            return setting_issue(view, PHASE_RULE) is None and isinstance(
                view.leaf_value(PHASES_PATH), tuple
            )
        return setting_issue(view, SETTINGS[self.spec.key]) is None

    async def async_set_native_value(self, value: float) -> None:
        if self.spec.key == "charging_current_limit":
            await self._run(async_set_current_limit(self.coordinator, value))
        elif self._phase is not None:
            await self._run(async_set_phase_limit(self.coordinator, self._phase, value))
        else:
            await self._run(async_set_setting(self.coordinator, self.spec.key, value))


class WallboxSwitch(WallboxEntity, SwitchEntity):
    """Boost, lock or an on/off setting."""

    @property
    def is_on(self) -> bool | None:
        view = self.view
        key = self.spec.key
        if key == "boost":
            return _on_off(view.leaf_value(FORCE_PATH))
        if key == "locked":
            return _on_off(view.conf.get("locked"))
        value = view.leaf_value(SETTINGS[key].path)
        if key == "home_battery_priority":
            # -100 prefers the home battery, 0 does not; any other value is a
            # custom offset the switch cannot show.
            if isinstance(value, float) and value in (-100.0, 0.0):
                return value == -100.0
            return None
        return _on_off(value)

    def _usable(self, view: WallboxView) -> bool:
        if self.spec.key == "boost":
            return boost_issue(view) is None
        return setting_issue(view, SETTINGS[self.spec.key]) is None

    async def _set(self, on: bool) -> None:
        if self.spec.key == "boost":
            await self._run(async_set_boost(self.coordinator, on))
        else:
            await self._run(async_set_setting(self.coordinator, self.spec.key, on))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class WallboxSelect(WallboxEntity, SelectEntity):
    """Normal or solar charging."""

    def __init__(
        self, coordinator: WallboxCoordinator, spec: WallboxEntitySpec
    ) -> None:
        super().__init__(coordinator, spec)
        self._attr_options = ["manual", "solar"]

    @property
    def current_option(self) -> str | None:
        strategy = self.view.conf.get("charging_strategy")
        return {"default": "manual", "solar-eco": "solar"}.get(
            strategy if isinstance(strategy, str) else ""
        )

    def _usable(self, view: WallboxView) -> bool:
        return strategy_issue(view) is None

    async def async_select_option(self, option: str) -> None:
        await self._run(async_set_strategy(self.coordinator, option))


class WallboxText(WallboxEntity, TextEntity):
    """The display name of the wallbox."""

    _attr_mode = TextMode.TEXT
    _attr_native_min = 1

    @property
    def native_max(self) -> int:
        """32 characters as in the app; a longer name the wallbox reports
        (up to 128) is still shown, while a write keeps the 32."""
        return max(SETTINGS["label"].max_length, len(self.native_value or ""))

    @property
    def native_value(self) -> str | None:
        label = self.view.conf.get("label")
        return label if isinstance(label, str) else None

    def _usable(self, view: WallboxView) -> bool:
        return setting_issue(view, SETTINGS["label"]) is None

    async def async_set_value(self, value: str) -> None:
        await self._run(async_set_setting(self.coordinator, "label", value))


class WallboxReading(WallboxEntity, SensorEntity):
    """External meter, limiting source, energy manager, currency."""

    def __init__(
        self, coordinator: WallboxCoordinator, spec: WallboxEntitySpec
    ) -> None:
        super().__init__(coordinator, spec)
        if spec.key == "external_meter":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["disconnected", "connected", "error"]
        elif spec.key == "limiter":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(LIMITERS.values())
        elif spec.key == "energy_manager":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(ENERGY_MANAGER_STATES.values())
        elif spec.key == "external_meter_maximum_current":
            self._attr_device_class = SensorDeviceClass.CURRENT
            self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    @property
    def native_value(self) -> str | float | None:
        view = self.view
        key = self.spec.key
        if key in ("limiter", "energy_manager"):
            reading = view.telemetry.get(key)
            return reading if isinstance(reading, str) else None
        value = view.leaf_value(self.spec.shows[0])
        if key == "external_meter_maximum_current":
            return value if isinstance(value, float) and value >= 0 else None
        return value if isinstance(value, str) else None


class WallboxFlag(WallboxEntity, BinarySensorEntity):
    """An on/off fact the wallbox reports."""

    _SOURCES: ClassVar[dict[str, tuple[Literal["telemetry", "conf", "info"], str]]] = {
        "grid_friendly_configured": ("info", "grid_friendly_configured"),
        "grid_friendly_active": ("info", "grid_friendly_active"),
        "relays_available": ("info", "relays_available"),
        "authentication_required": ("conf", "authentication"),
    }

    @property
    def is_on(self) -> bool | None:
        view = self.view
        source = self._SOURCES.get(self.spec.key)
        if source is not None:
            section, key = source
            return _on_off(getattr(view, section).get(key))
        return _on_off(view.leaf_value(self.spec.shows[0]))


ENTITY_TYPES: dict[Platform, type[WallboxEntity]] = {
    Platform.BUTTON: WallboxButton,
    Platform.NUMBER: WallboxNumber,
    Platform.SWITCH: WallboxSwitch,
    Platform.SELECT: WallboxSelect,
    Platform.TEXT: WallboxText,
    Platform.SENSOR: WallboxReading,
    Platform.BINARY_SENSOR: WallboxFlag,
}


def track_wallbox_entities(
    platform: Platform,
    scope: PlatformScope,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add each wallbox entity of ``platform`` once the wallbox reports what
    it needs; it is never removed again."""
    entity_type = ENTITY_TYPES[platform]
    for coordinator in scope.wallboxes:
        coordinator.want_leaves()
        seen: set[str] = set()

        @callback
        def discover(
            coordinator: WallboxCoordinator = coordinator, seen: set[str] = seen
        ) -> None:
            view = cached_view(coordinator)
            entities = []
            for spec in SPECS:
                if (
                    spec.platform is platform
                    and spec.key not in seen
                    and spec.creatable(view)
                ):
                    seen.add(spec.key)
                    entities.append(entity_type(coordinator, spec))
            if entities:
                async_add_entities(entities)

        scope.async_on_unload(coordinator.async_add_listener(discover))
        discover()
